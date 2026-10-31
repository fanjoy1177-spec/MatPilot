"""Explicit Fe-Ni-O review priorities, followed by representative selection.

This policy ports MG-Flow's published review thresholds without fitting them to
the current candidate collection.  A priority is a proposed next workflow step,
not a prediction of stability, successful relaxation, or material performance.
"""
from __future__ import annotations

from collections import Counter
import itertools
import math

from .recommendations import _inspect, recommend_candidates


POLICY_ID = "fe_ni_o_relaxation_v1"
EXPECTED_ELEMENTS = frozenset({"Fe", "Ni", "O"})
COVALENT_RADII = {"O": 0.66, "Fe": 1.32, "Ni": 1.24}
ATOMIC_WEIGHTS = {"O": 15.999, "Fe": 55.845, "Ni": 58.6934}
LIMITATIONS = [
    "This Fe-Ni-O heuristic assigns a next-step review priority, not material properties or relaxation success.",
    "Uniform Fe and Ni oxidation states of +2 or +3 with O -2 are a deliberately limited check; a failed check does not exclude mixed valence or other oxidation states.",
    "Covalent radii, volume and density thresholds are review heuristics and are not universal criteria for acceptable crystals.",
    "No relaxation, DFT, force, energy, stability or catalytic-property calculation is performed.",
]


def policy_description() -> dict:
    return {"id": POLICY_ID, "scope": "exact_Fe_Ni_O_workflow_review_only",
            "origin": "MG-Flow/scripts/structure_screen_candidates.py:analyze_cif",
            "frozen_before_current_collection_analysis": True,
            "initial_score": 100,
            "penalties": {
                "minimum_distance_below_0_8_angstrom": 50,
                "otherwise_minimum_covalent_ratio_below_0_65": 25,
                "volume_per_atom_outside_inclusive_5_to_40_angstrom3": 20,
                "density_outside_inclusive_1_to_12_g_cm3": 15,
                "uniform_oxidation_state_balance_not_found": 15},
            "priority_rule": "score >= 85 and uniform oxidation-state balance found",
            "manual_review_rule": "known score >= 60 but not priority, or unknown analysis",
            "defer_rule": "known score < 60",
            "covalent_radii_angstrom": dict(COVALENT_RADII),
            "uniform_oxidation_states": {"Fe": [2, 3], "Ni": [2, 3], "O": [-2]},
            "distance_method": "pymatgen nearest periodic pair distances including shortest nonzero lattice self-image",
            "occupancy_method": "frozen structural checker requires explicit full CIF occupancy",
            "limitations": list(LIMITATIONS)}


def _require_supported_elements(expected_elements) -> None:
    if (not isinstance(expected_elements, (list, tuple, set, frozenset))
            or any(not isinstance(x, str) for x in expected_elements)
            or set(expected_elements) != EXPECTED_ELEMENTS):
        raise ValueError(f"{POLICY_ID} supports exactly Fe, Ni and O; use structural recommendation for other element sets.")


def _unknown(reason: str, error: str | None = None) -> dict:
    result = {"policy_id": POLICY_ID, "tier": "manual_review", "priority": "人工复核",
              "score": None, "eligible_for_relaxation_shortlist": False,
              "issues": [reason], "metrics": {}, "limitations": list(LIMITATIONS)}
    if error is not None:
        result["analysis_error"] = error
    return result


def uniform_oxidation_balance_possible(composition: dict) -> bool | None:
    """A negative result only asks for review; mixed valence is not tested."""
    if (not isinstance(composition, dict) or set(composition) != EXPECTED_ELEMENTS
            or any(isinstance(x, bool) or not isinstance(x, (int, float))
                   or not math.isfinite(x) or x <= 0 or abs(x - round(x)) > 1e-6
                   for x in composition.values())):
        return None
    return any(composition["Fe"] * iron + composition["Ni"] * nickel
               == 2 * composition["O"] for iron, nickel in itertools.product((2, 3), repeat=2))


def classify_relaxation_metrics(*, minimum_distance_angstrom, minimum_covalent_ratio,
                                volume_per_atom_angstrom3, density_g_cm3,
                                uniform_charge_balance_possible) -> dict:
    """Apply the frozen thresholds to fully known measurements; unknowns fail closed."""
    values = (minimum_distance_angstrom, minimum_covalent_ratio,
              volume_per_atom_angstrom3, density_g_cm3)
    if (any(isinstance(x, bool) or not isinstance(x, (int, float))
            or not math.isfinite(x) or x <= 0 for x in values)
            or type(uniform_charge_balance_possible) is not bool):
        return _unknown("triage_measurements_unknown_or_invalid")
    score, issues = 100, []
    if minimum_distance_angstrom < 0.8:
        score -= 50
        issues.append("severely_short_periodic_contact")
    elif minimum_covalent_ratio < 0.65:
        score -= 25
        issues.append("suspect_short_contact_relative_to_covalent_radii")
    if not 5 <= volume_per_atom_angstrom3 <= 40:
        score -= 20
        issues.append("volume_per_atom_outside_review_range")
    if not 1 <= density_g_cm3 <= 12:
        score -= 15
        issues.append("estimated_density_outside_review_range")
    if not uniform_charge_balance_possible:
        score -= 15
        issues.append("uniform_oxidation_state_balance_requires_review_mixed_valence_not_tested")
    score = max(score, 0)
    if score >= 85 and uniform_charge_balance_possible:
        tier, label = "priority_relaxation", "优先弛豫"
    elif score >= 60:
        tier, label = "manual_review", "人工复核"
    else:
        tier, label = "defer", "暂缓"
    return {"policy_id": POLICY_ID, "tier": tier, "priority": label, "score": score,
            "eligible_for_relaxation_shortlist": tier == "priority_relaxation",
            "issues": issues,
            "metrics": {"minimum_distance_angstrom": float(minimum_distance_angstrom),
                        "minimum_covalent_ratio": float(minimum_covalent_ratio),
                        "volume_per_atom_angstrom3": float(volume_per_atom_angstrom3),
                        "density_g_cm3": float(density_g_cm3),
                        "uniform_charge_balance_possible": uniform_charge_balance_possible},
            "limitations": list(LIMITATIONS)}


def _periodic_contact_metrics(crystal) -> tuple[float, float]:
    """Find nearest pair and radius-scaled pair, including i == j image contacts."""
    import numpy as np

    # One basis vector is an upper bound on the shortest nonzero lattice vector.
    # pymatgen enumerates the sphere, so skew cells are not limited to +/-1 images.
    radius = min(float(x) for x in crystal.lattice.abc) + 1e-8
    images = crystal.lattice.get_points_in_sphere([[0, 0, 0]], [0, 0, 0], radius)
    self_distances = [float(point[1]) for point in images if float(point[1]) > 1e-8]
    if not self_distances:
        raise ValueError("No nonzero periodic self-image distance was available.")
    distances = np.asarray(crystal.distance_matrix, dtype=float).copy()
    np.fill_diagonal(distances, min(self_distances))
    radii = np.asarray([COVALENT_RADII[site.specie.symbol] for site in crystal], dtype=float)
    ratios = distances / (radii[:, None] + radii[None, :])
    return float(distances.min()), float(ratios.min())


def analyze_relaxation_candidate(record: dict, expected_elements, *,
                                 symprec: float = 0.1, angle_tolerance: float = 5.0) -> dict:
    """Recheck SHA, explicit occupancy and geometry before evaluating this policy."""
    _require_supported_elements(expected_elements)
    row, crystal = _inspect(record, set(EXPECTED_ELEMENTS), symprec, angle_tolerance)
    if crystal is None:
        return {"policy_id": POLICY_ID, "tier": "not_eligible", "priority": "未通过结构预检",
                "score": None, "eligible_for_relaxation_shortlist": False,
                "issues": list(row["exclusion_reasons"]), "metrics": {},
                "limitations": list(LIMITATIONS)}
    try:
        composition = dict(Counter(site.specie.symbol for site in crystal))
        distance, ratio = _periodic_contact_metrics(crystal)
        volume = float(crystal.volume)
        density = 1.66053906660 * sum(ATOMIC_WEIGHTS[x] * count for x, count in composition.items()) / volume
        result = classify_relaxation_metrics(minimum_distance_angstrom=distance,
            minimum_covalent_ratio=ratio, volume_per_atom_angstrom3=volume / len(crystal),
            density_g_cm3=density,
            uniform_charge_balance_possible=uniform_oxidation_balance_possible(composition))
        result["metrics"].update(composition=composition, cell_volume_angstrom3=volume,
                                 input_site_count=len(crystal))
        return result
    except Exception as exc:
        return _unknown("relaxation_triage_analysis_unavailable", f"{type(exc).__name__}: {exc}")


def recommend_for_relaxation(records: list[dict], expected_elements, limit: int = 12, *,
                             priority_candidate_ids: list[str] | None = None,
                             priority_selection_basis: str | None = None,
                             symprec: float = 0.1, angle_tolerance: float = 5.0) -> dict:
    """Precheck, deduplicate, triage, then apply frozen diversity ranking to priority representatives."""
    _require_supported_elements(expected_elements)
    # The original API validates all caller inputs, identities and bytes.  Its
    # temporary selection is discarded; no material is generated or modified.
    audit = recommend_candidates(records, list(EXPECTED_ELEMENTS), limit,
        priority_candidate_ids=priority_candidate_ids, priority_selection_basis=priority_selection_basis,
        symprec=symprec, angle_tolerance=angle_tolerance)
    by_id = {record["candidate_id"]: record for record in records}
    triage, priority_ids = {}, []
    for row in audit["records"]:
        if row["qualified"]:
            item = analyze_relaxation_candidate(by_id[row["candidate_id"]], expected_elements,
                                                symprec=symprec, angle_tolerance=angle_tolerance)
        else:
            item = {"policy_id": POLICY_ID, "tier": "not_eligible", "priority": "未通过代表结构资格检查",
                    "score": None, "eligible_for_relaxation_shortlist": False,
                    "issues": list(row["exclusion_reasons"]), "metrics": {},
                    "limitations": list(LIMITATIONS)}
        triage[row["candidate_id"]] = item
        if item["eligible_for_relaxation_shortlist"]:
            priority_ids.append(row["candidate_id"])
    result = recommend_candidates(records, list(EXPECTED_ELEMENTS), limit,
        priority_candidate_ids=priority_ids,
        priority_selection_basis=f"{POLICY_ID}: frozen review heuristics; priority is a workflow proposal, not property validation",
        symprec=symprec, angle_tolerance=angle_tolerance)
    first_rows = {row["candidate_id"]: row for row in audit["records"]}
    for row in result["records"]:
        original = first_rows[row["candidate_id"]]
        row["structural_representative_qualified"] = original["qualified"]
        row["structural_exclusion_reasons"] = list(original["exclusion_reasons"])
        row["relaxation_priority"] = triage[row["candidate_id"]]
        # Preserve the original representative relation even when an entire
        # equivalence class was excluded from the later priority-only pass.
        if original.get("representative_id") is not None:
            row["representative_id"] = original["representative_id"]
        if original["qualified"] and not triage[row["candidate_id"]]["eligible_for_relaxation_shortlist"]:
            row["exclusion_reasons"] = [x for x in row["exclusion_reasons"] if x != "outside_explicit_priority_selector"]
            row["exclusion_reasons"].append("not_in_heuristic_priority_relaxation_pool")
            row["not_selected_reason"] = "not_in_heuristic_priority_relaxation_pool"
    tiers = Counter(item["tier"] for item in triage.values())
    result.update(method="verified_cif_fe_ni_o_triage_then_category_coverage_v1",
        relaxation_triage_policy=policy_description(),
        caller_priority_selector=audit["priority_selector"],
        stage_counts={"input_count": len(records),
                      "structural_representative_count": audit["eligible_representative_count"],
                      "priority_relaxation_count": tiers["priority_relaxation"],
                      "manual_review_count": tiers["manual_review"],
                      "defer_count": tiers["defer"],
                      "not_eligible_count": tiers["not_eligible"],
                      "selected_count": result["selected_count"],
                      "requested_count": limit,
                      "shortfall_count": max(0, limit - result["selected_count"])})
    result["limitations"] += list(LIMITATIONS)
    return result
