"""Rank explicit, version-bound scalar evidence without running a predictor.

This is an evidence contract, not a calibrated materials-quality model.  It does
not infer measurements from generation conditions, filenames, or LLM prose.
"""
from __future__ import annotations

import math
import re
from copy import deepcopy

from .adapters.mattergen import canonical_hash
from .evidence import _known_identity, compare_metrics, metric_usable, validate_metric, verify_artifact


POLICY_ID = "registered_property_evidence_v1"
# A finite registry prevents an arbitrary energy alias from bypassing the
# composition rule. Add new scalar semantics deliberately with their own tests.
PROPERTY_UNITS = {
    "energy_above_hull": "eV/atom",
    "energy_above_reference_hull": "eV/atom",
    "energy": "eV",
    "total_energy": "eV",
    "energy_per_atom": "eV/atom",
    "formation_energy": "eV/atom",
    "dft_band_gap": "eV",
    "band_gap": "eV",
    "ml_bulk_modulus": "GPa",
    "bulk_modulus": "GPa",
    "dft_mag_density": "mu_B/angstrom^3",
    "hhi_score": "dimensionless",
    "max_force": "eV/angstrom",
    "max_stress": "GPa",
    "max_abs_stress": "GPa",
}
COMPOSITION_RESTRICTED = {"energy", "total_energy", "energy_per_atom", "formation_energy"}
CELL_RESTRICTED = {"energy", "total_energy"}
READINESS_PROPERTIES = {"max_force", "max_stress", "max_abs_stress"}
REFERENCE_PROPERTIES = {"energy_above_hull", "energy_above_reference_hull", "formation_energy"}
NONNEGATIVE_PROPERTIES = {"energy_above_hull", "dft_band_gap", "band_gap", *READINESS_PROPERTIES}


def _number(value, field: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number.")
    return value


def validate_objective(objective: dict) -> dict:
    """Normalize a scalar objective; no implicit unit conversion is performed."""
    required = {"property", "unit", "direction"}
    optional = {"target_min", "target_max"}
    if not isinstance(objective, dict) or not required <= set(objective) or set(objective) - required - optional:
        raise ValueError("Objective needs property, unit, direction and optional target_min/target_max only.")
    prop = objective["property"]
    if not isinstance(prop, str) or prop not in PROPERTY_UNITS:
        raise ValueError("Unknown scalar objective property; register its semantics before ranking.")
    if objective["unit"] != PROPERTY_UNITS[prop]:
        raise ValueError(f"Objective {prop} requires the explicit canonical unit {PROPERTY_UNITS[prop]}.")
    directions = {"minimize": "minimize", "maximize": "maximize", "min": "minimize", "max": "maximize"}
    if not isinstance(objective["direction"], str) or objective["direction"] not in directions:
        raise ValueError("Objective direction must be minimize or maximize.")
    normalized = {**objective, "direction": directions[objective["direction"]]}
    for name in optional & set(objective):
        _number(objective[name], name)
    if "target_min" in objective and "target_max" in objective and objective["target_min"] > objective["target_max"]:
        raise ValueError("target_min must not exceed target_max.")
    return normalized


def _rank_key(value: float, objective: dict) -> tuple:
    low, high = objective.get("target_min"), objective.get("target_max")
    distance = low - value if low is not None and value < low else value - high if high is not None and value > high else 0
    direction_value = value if objective["direction"] == "minimize" else -value
    result = (int(distance > 0), distance, direction_value)
    # Finite inputs can still overflow when subtracting opposite extreme values.
    if not all(math.isfinite(x) for x in result):
        raise ValueError("Objective distance overflows; use a representable target range.")
    return result


def _composition_scope(records: list[dict], prop: str) -> dict:
    if prop not in COMPOSITION_RESTRICTED:
        return {"restriction": "comparable_method_and_reference", "composition_comparison": "not_required_for_this_scalar"}
    from pymatgen.core import Structure

    compositions = []
    for record in records:
        # Do not trust an editable formula field to authorize energy comparisons.
        crystal = Structure.from_file(record["path"])
        comp = crystal.composition.element_composition
        compositions.append(comp if prop in CELL_RESTRICTED else comp.fractional_composition)
    if compositions and any(not compositions[0].almost_equals(x, rtol=1e-8, atol=1e-10) for x in compositions[1:]):
        detail = "identical cell stoichiometry and atom count" if prop in CELL_RESTRICTED else "the same composition"
        raise ValueError(f"{prop} cannot form a global ranking across compositions; it requires {detail}. Use compatible energy_above_hull evidence for cross-composition stability screening.")
    return {
        "restriction": "same_cell_stoichiometry" if prop in CELL_RESTRICTED else "same_fractional_composition",
        "composition_comparison": "parsed_verified_CIF_not_record_formula",
        "composition": dict(sorted(compositions[0].get_el_amt_dict().items())) if compositions else None,
    }


def build_evidence_rank_keys(
    records: list[dict],
    metrics_by_version: dict[str, list[dict]],
    objective: dict,
    metric_selections: list[dict] | None = None,
) -> dict:
    """Return ascending tuple keys and a complete selection audit.

    ``records`` must be the authoritative original versions. A selection has
    candidate_id, structure_version_id and metric_id; an optional job_id is
    checked against the corresponding record. The service may instead resolve
    job identity itself and pass three-field selections. No metric is selected
    automatically, including when only one is available. Unselected raw records
    are preserved without being presented as validated ranking evidence.

    Bad request/identity bindings or incompatible methods raise ValueError.
    Missing, stale, incomplete or unconverged selected evidence has no rank key
    and receives an explicit unresolved record for downstream review. Ranking
    only covers the usable selected subset; missing candidates never receive a
    favorable default value. Target bounds are ranking preferences, not proof of
    fulfilling any separate hard requirement.
    """
    objective = validate_objective(objective)
    if not isinstance(records, list) or not isinstance(metrics_by_version, dict):
        raise ValueError("Records must be a list and metrics_by_version an evidence mapping.")
    if any(not isinstance(k, str) or not isinstance(v, list) or any(not isinstance(m, dict) for m in v)
           for k, v in metrics_by_version.items()):
        raise ValueError("Each registered structure version must map to a list of metric objects.")

    by_candidate, by_version = {}, {}
    for record in records:
        if not isinstance(record, dict) or any(not isinstance(record.get(k), str) or not record[k]
                                              for k in ("candidate_id", "structure_version_id", "sha256", "path")):
            raise ValueError("Every record needs explicit candidate, original version, SHA256 and artifact path.")
        if not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
            raise ValueError("Record SHA256 must be a lowercase 64-character digest.")
        if record.get("parent_version_id") is not None or record.get("transformation", "generated") != "generated":
            raise ValueError("Evidence ranking accepts original generated structure versions only.")
        cid, vid = record["candidate_id"], record["structure_version_id"]
        if cid in by_candidate or vid in by_version:
            raise ValueError("Candidate and original structure version identities must be unique.")
        by_candidate[cid], by_version[vid] = record, record

    if metric_selections is None:
        metric_selections = []
    if not isinstance(metric_selections, list):
        raise ValueError("metric_selections must be an explicit list.")
    selected = {}
    fields = {"candidate_id", "structure_version_id", "metric_id"}
    for selection in metric_selections:
        if (not isinstance(selection, dict) or set(selection) not in (fields, fields | {"job_id"})
                or any(not isinstance(v, str) or not v for v in selection.values())):
            raise ValueError("Each metric selection needs candidate_id, structure_version_id, metric_id and optional job_id.")
        cid, vid = selection["candidate_id"], selection["structure_version_id"]
        record = by_candidate.get(cid)
        if record is None or record["structure_version_id"] != vid:
            raise ValueError("Selected candidate and original structure version do not match the registered records.")
        if "job_id" in selection and record.get("job_id") != selection["job_id"]:
            raise ValueError("Selected job does not match the registered original structure version.")
        if cid in selected:
            raise ValueError("Choose exactly one metric per candidate for this objective; duplicate selections are forbidden.")
        available = metrics_by_version.get(vid, [])
        matches = [m for m in available if m.get("metric_id") == selection["metric_id"]]
        if len(matches) != 1:
            raise ValueError("Selected metric must exist exactly once in the registered structure-version evidence.")
        metric = matches[0]
        if metric.get("candidate_id") != cid or metric.get("structure_version_id") != vid:
            raise ValueError("Selected metric belongs to another candidate or structure version.")
        if metric.get("property") != objective["property"] or metric.get("unit") != objective["unit"]:
            raise ValueError("Selected metric must match the objective property and unit exactly.")
        selected[cid] = metric

    usable, unresolved, statuses = [], [], []
    for record in records:
        cid, vid = record["candidate_id"], record["structure_version_id"]
        metric = selected.get(cid)
        reasons = []
        if metric is None:
            reasons.append("No explicit metric selection; available evidence was not automatically chosen.")
        else:
            try:
                verify_artifact(record)
                body = {k: v for k, v in metric.items() if k != "metric_id"}
                checked = validate_metric(body, record)
                if checked["metric_id"] != metric["metric_id"]:
                    raise ValueError("Selected metric integrity digest does not match its registered content.")
                refs = checked.get("reference_set")
                reference_missing = objective["property"] in REFERENCE_PROPERTIES and (
                    not isinstance(refs, dict) or not _known_identity(refs.get("id")) or not isinstance(refs.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", refs["sha256"]))
                if not metric_usable(checked) or reference_missing:
                    reasons.append("Selected evidence has unknown value, method/version/weights/parameters, unit, reference or convergence.")
                elif objective["property"] in NONNEGATIVE_PROPERTIES and checked["value"] < 0:
                    reasons.append("Selected scalar must be nonnegative under this property's registered semantics.")
                else:
                    key = _rank_key(checked["value"], objective)
                    usable.append((record, checked, key))
            except (ValueError, OSError, TypeError, KeyError, RuntimeError) as exc:
                reasons.append(f"Selected evidence could not be revalidated: {exc}")
        status = {"candidate_id": cid, "structure_version_id": vid,
                  "selected_metric_id": metric.get("metric_id") if metric else None,
                  "status": "review" if reasons else "usable_selected_evidence", "reasons": reasons}
        statuses.append(status)
        if reasons:
            unresolved.append(deepcopy(status))

    usable_metrics = [m for _, m, _ in usable]
    comparability = compare_metrics(usable_metrics)
    if len(usable_metrics) >= 2 and not comparability["comparable"]:
        raise ValueError("Selected evidence cannot form a global ranking: " + " ".join(comparability["reasons"]))
    scope = _composition_scope([r for r, _, _ in usable], objective["property"])
    rank_keys = {r["candidate_id"]: key for r, _, key in usable}
    normalized_selections = sorted((deepcopy(x) for x in metric_selections),
                                   key=lambda x: (x.get("job_id", ""), x["candidate_id"], x["structure_version_id"], x["metric_id"]))
    selected_bindings = {(x["structure_version_id"], x["metric_id"]) for x in metric_selections}
    unselected = [deepcopy(m) for vid, values in metrics_by_version.items() for m in values
                  if (vid, m.get("metric_id")) not in selected_bindings]
    readiness = objective["property"] in READINESS_PROPERTIES
    meaning = ("calculation_readiness_only_not_stability" if readiness else
               "signed_distance_to_fixed_reference_hull; negative_means_below_reference_not_universal_stability" if objective["property"] == "energy_above_reference_hull" else
               "method_specific_hull_distance_not_synthesis_success" if objective["property"] == "energy_above_hull" else
               "selected_scalar_objective_only_not_universal_material_quality")
    metadata = {
        "id": POLICY_ID,
        "description": "Explicit version-bound evidence; target-range distance then the requested scalar direction, with method/reference comparability checked.",
        "evidence_scope": "original_generated_versions_only; usable_explicit_selections_only; no new computation",
        "objective": deepcopy(objective), "interpretation": meaning,
        "rank_key_fields": ["outside_target_range", "distance_to_target_range", "direction_adjusted_value"],
        "target_semantics": "Ranking preference only; separate hard requirement checks are not replaced.",
        "composition_scope": scope, "comparability": comparability,
        "eligible_metric_count": len(usable), "unresolved_count": len(unresolved),
        "predictor_applicability": "not_assessed_by_this_evidence_contract",
        "scientific_accuracy": "not_validated", "uncertainty": "unknown",
        "limitations": "Verified artifact identity and matching declared metadata do not independently validate a predictor, reference data, scientific values, stability probability, synthesis, or application performance. Force/stress magnitudes describe readiness only; max_stress means a nonnegative magnitude, not a signed tensor component.",
    }
    audit = {
        "scope": "original_generated_versions_only", "selections": normalized_selections,
        "selection_sha256": canonical_hash(normalized_selections),
        "selected_metric_ids": sorted(m["metric_id"] for m in selected.values()),
        "selected_metrics": [deepcopy(selected[cid]) for cid in sorted(selected)],
        "unselected_metric_ids": sorted(m["metric_id"] for m in unselected if isinstance(m.get("metric_id"), str)),
        "unselected_metrics": unselected,
        "unselected_validation": "Preserved as registered; not revalidated or used to choose a more favorable ranking value.",
        "candidate_statuses": statuses,
    }
    return {"rank_keys": rank_keys, "ranking_metadata": metadata, "metric_selection_audit": audit,
            "unresolved_candidates": unresolved, "all_metrics_by_structure_version": deepcopy(metrics_by_version)}
