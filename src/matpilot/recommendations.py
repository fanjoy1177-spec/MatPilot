"""Deterministic, evidence-bound shortlists of existing CIF candidates.

This module never generates or relaxes structures and never evaluates material
properties. Category coverage is a shortlist design objective, not quality.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import io
import math
from pathlib import Path
import re
import warnings


MAX_CANDIDATES = 256
MAX_CIF_BYTES = 4 * 1024 * 1024
MATCHER_PARAMETERS = {"ltol": 0.2, "stol": 0.3, "angle_tol": 5,
                      "primitive_cell": True, "scale": True, "attempt_supercell": False}


def _size_bin(count: int) -> str:
    return "1-10" if count <= 10 else "11-20" if count <= 20 else "21+"


def _occupancy(value) -> float:
    return float(re.sub(r"\([0-9]+\)$", "", str(value).strip()))


def _inspect(record: dict, expected: set[str], symprec: float, angle_tolerance: float) -> tuple[dict, object | None]:
    from pymatgen.io.cif import CifParser
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    row = {"candidate_id": record.get("candidate_id"), "structure_version_id": record.get("structure_version_id"),
           "sha256": record.get("sha256"), "qualified": False, "selected": False,
           "exclusion_reasons": [], "warnings": [], "structural_status": "unknown"}
    reasons = row["exclusion_reasons"]
    path = record.get("path")
    digest = record.get("sha256")
    if not isinstance(path, str) or not Path(path).is_absolute():
        reasons.append("missing_or_nonabsolute_artifact_path")
        return row, None
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        reasons.append("missing_or_invalid_sha256")
        return row, None
    try:
        with Path(path).open("rb") as handle:
            content = handle.read(MAX_CIF_BYTES + 1)
        if len(content) > MAX_CIF_BYTES:
            reasons.append("cif_exceeds_4_mib")
            return row, None
        if hashlib.sha256(content).hexdigest() != digest:
            reasons.append("artifact_sha256_mismatch")
            return row, None
        row["artifact_verified"] = True
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            parser = CifParser(io.StringIO(content.decode("utf-8")), occupancy_tolerance=1.0)
            blocks = parser.as_dict()
            if len(blocks) != 1:
                reasons.append("expected_exactly_one_cif_data_block")
                return row, None
            structures = parser.parse_structures(primitive=False, check_occu=True, on_error="raise")
        row["warnings"] = [str(item.message) for item in caught]
        if len(structures) != 1:
            reasons.append("expected_exactly_one_structure")
            return row, None
        crystal = structures[0]
        if not 1 <= len(crystal) <= 1000:
            reasons.append("site_count_outside_1_to_1000")
            return row, None
        fields = next(iter(blocks.values()))
        occupations = fields.get("_atom_site_occupancy")
        if not isinstance(occupations, list) or not occupations:
            row["full_occupancy"] = None
            reasons.append("explicit_full_occupancy_not_available")
        else:
            values = [_occupancy(x) for x in occupations]
            full = all(math.isfinite(x) and abs(x - 1.0) <= 1e-6 for x in values)
            row["full_occupancy"] = bool(full and crystal.is_ordered
                and all(abs(float(site.species.num_atoms) - 1.0) <= 1e-6 for site in crystal))
            if not row["full_occupancy"]:
                reasons.append("partial_occupancy_or_disordered_sites")
        row["ordered"] = bool(crystal.is_ordered)
        actual = {element.symbol for element in crystal.composition.elements}
        row.update(elements=sorted(actual), reduced_formula=crystal.composition.reduced_formula,
                   site_count=len(crystal), size_bin=_size_bin(len(crystal)),
                   composition_complete=actual == expected, volume_angstrom3=float(crystal.volume))
        if actual != expected:
            reasons.append("element_set_mismatch")
            row.update(missing_elements=sorted(expected - actual), unexpected_elements=sorted(actual - expected))
        coordinates = [float(x) for site in crystal for x in site.frac_coords]
        coordinates += [float(x) for vector in crystal.lattice.matrix for x in vector]
        if not all(math.isfinite(x) for x in coordinates) or not math.isfinite(crystal.volume) or crystal.volume <= 0:
            reasons.append("invalid_geometry")
        else:
            # Includes periodic self-images, which distance_matrix alone omits.
            too_close = [float(neighbor.nn_distance) for neighbors in crystal.get_all_neighbors(0.5)
                         for neighbor in neighbors if float(neighbor.nn_distance) < 0.5 - 1e-8]
            row["minimum_distance_threshold_angstrom"] = 0.5
            row["basic_geometry_pass"] = not too_close
            if too_close:
                row["short_contact_angstrom"] = min(too_close)
                reasons.append("periodic_contact_below_0_5_angstrom")
        if "all_hard_requirements_pass" in record and record["all_hard_requirements_pass"] is not True:
            reasons.append("existing_hard_requirement_checks_not_passed")
        if reasons:
            return row, None
        try:
            analyzer = SpacegroupAnalyzer(crystal, symprec=symprec, angle_tolerance=angle_tolerance)
            group = int(analyzer.get_space_group_number())
            if not 1 <= group <= 230:
                raise ValueError("Invalid space-group number.")
            row.update(space_group_number=group, space_group_symbol=analyzer.get_space_group_symbol())
        except Exception as exc:
            reasons.append("space_group_unavailable")
            row["analysis_error"] = f"{type(exc).__name__}: {exc}"
            return row, None
        row["structural_status"] = "structural_precheck_passed_pending_properties"
        return row, crystal
    except Exception as exc:
        reasons.append("cif_analysis_failed")
        row["analysis_error"] = f"{type(exc).__name__}: {exc}"
        return row, None


def recommend_candidates(records: list[dict], expected_elements, limit: int = 12, *,
                         priority_candidate_ids: list[str] | None = None,
                         priority_selection_basis: str | None = None,
                         symprec: float = 0.1, angle_tolerance: float = 5.0) -> dict:
    """Recheck immutable CIF bytes, deduplicate, then maximize category coverage.

    An optional priority list is an explicit caller preference, never an inferred
    historical label. Its basis is recorded verbatim and conveys no scientific
    validation. Unknown or duplicate IDs in that selector are rejected.
    """
    from pymatgen.core import Element
    from pymatgen.analysis.structure_matcher import StructureMatcher

    if not isinstance(records, list) or not 1 <= len(records) <= MAX_CANDIDATES or not all(isinstance(r, dict) for r in records):
        raise ValueError("Provide 1 to 256 candidate records.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError("limit must be an integer from 1 to 256.")
    if not isinstance(expected_elements, (list, tuple, set)) or not expected_elements:
        raise ValueError("An explicit nonempty expected element set is required.")
    try:
        expected = {Element(value).symbol for value in expected_elements}
    except (ValueError, TypeError) as exc:
        raise ValueError("Expected elements must be valid element symbols.") from exc
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0
           for x in (symprec, angle_tolerance)):
        raise ValueError("Symmetry tolerances must be finite positive numbers.")
    ids = [r.get("candidate_id") for r in records]
    if any(not isinstance(value, str) or not value.strip() for value in ids):
        raise ValueError("Every record requires a nonempty candidate_id.")
    counts = Counter(ids)
    priority = None
    if priority_candidate_ids is not None:
        if (not isinstance(priority_candidate_ids, list)
                or any(not isinstance(x, str) for x in priority_candidate_ids)
                or len(set(priority_candidate_ids)) != len(priority_candidate_ids)
                or set(priority_candidate_ids) - set(ids)):
            raise ValueError("Priority selector must contain distinct known candidate IDs.")
        if not isinstance(priority_selection_basis, str) or not priority_selection_basis.strip():
            raise ValueError("An explicit priority selection basis is required.")
        priority = set(priority_candidate_ids)
    elif priority_selection_basis is not None:
        raise ValueError("A selection basis requires an explicit priority selector.")

    inspected, crystals = [], {}
    for record in sorted(records, key=lambda r: r["candidate_id"]):
        row, crystal = _inspect(record, expected, symprec, angle_tolerance)
        if counts[record["candidate_id"]] != 1:
            row["exclusion_reasons"].append("ambiguous_duplicate_candidate_id")
            crystal = None
        if priority is not None and record["candidate_id"] not in priority:
            row["exclusion_reasons"].append("outside_explicit_priority_selector")
            crystal = None
        inspected.append(row)
        if crystal is not None:
            crystals[row["candidate_id"]] = crystal

    matcher = StructureMatcher(**MATCHER_PARAMETERS)
    representatives = []
    for row in sorted((r for r in inspected if r["candidate_id"] in crystals),
                      key=lambda r: (r["site_count"], r["candidate_id"])):
        candidate_id = row["candidate_id"]
        equivalent = None
        try:
            for prior in representatives:
                if prior["reduced_formula"] == row["reduced_formula"] and matcher.fit(crystals[candidate_id], crystals[prior["candidate_id"]]):
                    equivalent = prior["candidate_id"]
                    break
        except Exception as exc:
            row["exclusion_reasons"].append("structural_equivalence_unknown")
            row["analysis_error"] = f"{type(exc).__name__}: {exc}"
            continue
        row["representative_id"] = equivalent or candidate_id
        if equivalent is not None:
            row["exclusion_reasons"].append("equivalent_to_qualified_representative")
            continue
        row["qualified"] = True
        representatives.append(row)

    covered = {"reduced_formula": set(), "space_group_number": set(), "size_bin": set()}
    remaining = list(representatives)
    selected = []
    while remaining and len(selected) < limit:
        def gain(row):
            return sum(row[key] not in covered[key] for key in covered)
        chosen = min(remaining, key=lambda r: (-gain(r), r["site_count"], r["candidate_id"]))
        added = [key for key in covered if chosen[key] not in covered[key]]
        chosen.update(selected=True, selection_rank=len(selected) + 1,
                      selection_reason={"new_category_coverage": added, "coverage_gain": len(added),
                                        "tie_break": "fewer_input_sites_then_candidate_id"})
        for key in covered:
            covered[key].add(chosen[key])
        selected.append(chosen)
        remaining.remove(chosen)
    for row in remaining:
        row["not_selected_reason"] = "requested_shortlist_limit_reached_under_fixed_coverage_policy"
    for row in inspected:
        if row["exclusion_reasons"]:
            row["not_selected_reason"] = "; ".join(row["exclusion_reasons"])

    return {"schema_version": 1, "method": "verified_cif_category_coverage_v1",
            "expected_elements": sorted(expected), "input_count": len(records),
            "eligible_representative_count": len(representatives), "requested_count": limit,
            "selected_count": len(selected), "target_met": len(selected) == limit,
            "selected_candidate_ids": [r["candidate_id"] for r in selected], "records": inspected,
            "coverage": {key: {"selected_values": sorted(values), "selected_count": len(values),
                                "pool_count": len({row[key] for row in representatives})}
                         for key, values in covered.items()},
            "priority_selector": {"candidate_ids": sorted(priority), "basis": priority_selection_basis,
                                  "evidence_scope": "explicit_caller_preference_not_property_validation"}
                                 if priority is not None else None,
            "policy": {"category_weights": {key: 1 for key in covered},
                       "size_bins_sites": ["1-10", "11-20", "21+"],
                       "symprec_angstrom": symprec, "angle_tolerance_degrees": angle_tolerance,
                       "structure_matcher": dict(MATCHER_PARAMETERS), "minimum_contact_angstrom": 0.5,
                       "occupancy_tolerance": 1e-6, "requires_explicit_cif_occupancies": True},
            "limitations": [
                "Ranking selects a representative shortlist; it does not improve or validate material properties.",
                "Full occupancy and structural sanity do not prove stability, synthesizability or catalytic performance.",
                "Space groups and equivalence depend on the stated tolerances; unknown analysis is excluded, never replaced by P1.",
                "Site count is an input-cell scale proxy, not measured runtime, compute cost or physical quality.",
                "Greedy fixed-weight category coverage is not a proof of globally optimal selection.",
                "No generation, relaxation, DFT, external novelty search or API request is performed."]}
