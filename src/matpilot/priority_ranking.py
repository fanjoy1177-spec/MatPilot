"""Frozen, development-trained ranking inside the unchanged ionic priority pool.

No property labels enter inference. Sigmoid scores are uncalibrated relative
scores, not hull energies, success probabilities or material-property claims.
"""
from __future__ import annotations

import copy
import math
import re
from functools import reduce

from .mixed_valence import recommend_with_mixed_valence
from .priority_ranking_model import (
    MODEL_SHA256, TRAINING_FEATURE_MIN, TRAINING_FEATURE_MAX, load_model,
)


POLICY_ID = "ionic_ranked_v2"
FEATURE_NAMES = (
    "log_reduced_atoms", "anion_fraction", "cation_average_valence", "charge_position",
    "requires_mixed", "log_volume_atom", "log_density", "composition_entropy",
)
MAX_RECORDS = 256
MAX_ATOMS = 1000


def policy_description() -> dict:
    model = load_model()
    return {"id": POLICY_ID, "model_id": model["model_id"], "model_sha256": MODEL_SHA256,
            "gate": "unchanged ionic_mixed_valence_v1 priority representatives",
            "features": list(FEATURE_NAMES), "training": copy.deepcopy(model["training"]),
            "performance_labels_used_for_training": True, "runtime_property_labels_used": False,
            "score_interpretation": "uncalibrated_relative_screening_score_not_probability_or_hull",
            "coverage_bonus": model["coverage_bonus"],
            "coverage_categories": list(model["coverage_categories"]),
            "tie_break": "CIF_sha256_then_candidate_id",
            "fallback": "whole_priority_pool_keeps_legacy_selection_on_any_invalid_feature_or_untrained_anion",
            "resource_bound": "at most 256 representatives; 8 features; O(k*n) greedy selection",
            "limitations": list(model["limitations"])}


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}_not_a_number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name}_not_finite") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{name}_not_finite_positive" if positive else f"{name}_not_finite")
    return number


def extract_features(row: dict) -> dict[str, float]:
    """Use verified composition/per-atom metrics only; no label or input-cell size feature."""
    try:
        priority = row["relaxation_priority"]
        metrics = priority["metrics"]
        charge = metrics["charge_feasibility"]
        if charge["status"] != "feasible" or charge["mixed_possible"] is not True:
            raise ValueError("priority_charge_not_feasible")
        composition = charge["composition"]
        if not isinstance(composition, dict) or not 2 <= len(composition) <= 118:
            raise ValueError("invalid_composition_mapping")
        counts, total = {}, 0
        for element, count in composition.items():
            if (not isinstance(element, str) or not re.fullmatch(r"[A-Z][a-z]?", element)
                    or isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ATOMS):
                raise ValueError("invalid_element_or_atom_count")
            counts[element] = count
            total += count
        if total > MAX_ATOMS:
            raise ValueError("atom_budget_exceeded")
        system = charge["system"]
        anion = system["anion"]["element"]
        if anion not in counts:
            raise ValueError("anion_not_in_composition")
        anion_state = _number(system["anion"]["oxidation_state"], "anion_state")
        target = _number(charge["target_cation_charge"], "target_cation_charge", positive=True)
        if anion_state >= 0 or not math.isclose(target, -counts[anion] * anion_state, rel_tol=1e-12):
            raise ValueError("inconsistent_formal_charge_target")
        catalog = system["cation_catalog"]
        if not isinstance(catalog, dict) or set(catalog) != set(counts) - {anion}:
            raise ValueError("inconsistent_cation_catalog")
        low, high = 0.0, 0.0
        for element, entry in catalog.items():
            states = entry["states"]
            if not isinstance(states, (list, tuple)) or not 1 <= len(states) <= 32:
                raise ValueError("invalid_cation_states")
            if any(isinstance(s, bool) or not isinstance(s, int) or not 1 <= s <= 20 for s in states):
                raise ValueError("invalid_cation_state")
            low += counts[element] * min(states)
            high += counts[element] * max(states)
        if not low <= target <= high:
            raise ValueError("target_outside_cation_catalog")
        requires_mixed = charge["requires_mixed_valence"]
        if not isinstance(requires_mixed, bool):
            raise ValueError("mixed_requirement_unknown")
        cation_atoms = total - counts[anion]
        average = target / cation_atoms
        minimum = low / cation_atoms
        maximum = high / cation_atoms
        volume = _number(metrics["volume_per_atom_angstrom3"], "volume_per_atom", positive=True)
        density = _number(metrics["density_g_cm3"], "density", positive=True)
        values = (
            math.log(total / reduce(math.gcd, counts.values())), counts[anion] / total, average,
            (average - minimum) / (maximum - minimum) if maximum > minimum else 0.5,
            float(requires_mixed), math.log(volume), math.log(density),
            -sum((count / total) * math.log(count / total) for count in counts.values()),
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("nonfinite_feature")
        return dict(zip(FEATURE_NAMES, values))
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("required_verified_ranking_metric_missing_or_malformed") from exc


def _validate_model(model):
    if tuple(model["features"]) != FEATURE_NAMES or model["feature_schema_version"] != 1:
        raise ValueError("unsupported_ranking_feature_schema")
    for key in ("mean", "scale", "weights"):
        if len(model[key]) != len(FEATURE_NAMES):
            raise ValueError("ranking_model_vector_length_mismatch")
        for value in model[key]:
            _number(value, "ranking_model_" + key, positive=key == "scale")
    _number(model["intercept"], "ranking_model_intercept")
    _number(model["coverage_bonus"], "ranking_model_coverage_bonus")
    if model["standardized_clip"] != [-5.0, 5.0] or model["coverage_categories"] != ["reduced_formula", "space_group_number"]:
        raise ValueError("unsupported_ranking_selection_specification")


def _score(row, model):
    features = extract_features(row)
    raw_z = [(features[name] - mean) / scale
             for name, mean, scale in zip(FEATURE_NAMES, model["mean"], model["scale"])]
    if not all(math.isfinite(z) for z in raw_z):
        raise ValueError("nonfinite_standardized_feature")
    lower, upper = model["standardized_clip"]
    clipped = [min(upper, max(lower, value)) for value in raw_z]
    terms = [value * weight for value, weight in zip(clipped, model["weights"])]
    linear = model["intercept"] + sum(terms)
    if not math.isfinite(linear):
        raise ValueError("nonfinite_linear_score")
    score = 1 / (1 + math.exp(-linear)) if linear >= 0 else math.exp(linear) / (1 + math.exp(linear))
    warnings = []
    outside = [name for index, name in enumerate(FEATURE_NAMES)
               if features[name] < TRAINING_FEATURE_MIN[index] - 1e-12
               or features[name] > TRAINING_FEATURE_MAX[index] + 1e-12]
    clips = [name for name, before, after in zip(FEATURE_NAMES, raw_z, clipped) if before != after]
    elements = set(row["relaxation_priority"]["metrics"]["charge_feasibility"]["composition"])
    if elements not in [set(system.split("-")) for system in model["training"]["systems"]]:
        warnings.append("chemical_system_not_seen_in_training_extrapolation")
    if outside:
        warnings.append("features_outside_observed_training_range")
    if clips:
        warnings.append("standardized_features_clipped_to_frozen_limits")
    return {"status": "scored", "model_id": model["model_id"], "model_sha256": MODEL_SHA256,
            "features": features, "standardized_features_before_clip": dict(zip(FEATURE_NAMES, raw_z)),
            "standardized_features": dict(zip(FEATURE_NAMES, clipped)),
            "intercept": model["intercept"], "logit_contributions": dict(zip(FEATURE_NAMES, terms)),
            "linear_score": linear, "uncalibrated_score": score,
            "score_interpretation": "relative_score_not_calibrated_probability_or_predicted_hull",
            "outside_training_range_features": outside, "clipped_features": clips, "warnings": warnings}


def _is_priority(row):
    priority = row.get("relaxation_priority", {})
    return row.get("qualified") is True and priority.get("eligible_for_relaxation_shortlist") is True


def rerank_priority_pool(base_result: dict) -> dict:
    """Return a copy; any unavailable feature keeps the entire old selection intact."""
    if not isinstance(base_result, dict) or not isinstance(base_result.get("records"), list):
        raise ValueError("A complete mixed-valence recommendation result is required.")
    if len(base_result["records"]) > MAX_RECORDS:
        raise ValueError("Ranking accepts at most 256 inspected records.")
    if not all(isinstance(row, dict) for row in base_result["records"]):
        raise ValueError("Recommendation rows must be mappings.")
    result = copy.deepcopy(base_result)
    rows = result["records"]
    pool = [row for row in rows if _is_priority(row)]
    metadata = {"policy_id": POLICY_ID, "status": "pending", "model_sha256": MODEL_SHA256,
                "performance_labels_used_for_training": True, "runtime_property_labels_used": False,
                "priority_pool_count": len(pool), "gate_unchanged": True,
                "legacy_selected_candidate_ids": list(base_result.get("selected_candidate_ids", [])),
                "fallback_reasons": [], "warnings": []}
    result["priority_ranking"] = metadata
    for row in rows:
        row["ranking_evidence"] = {"status": "pending" if _is_priority(row) else "outside_priority_pool",
                                   "uncalibrated_score": None}

    def fallback(reasons):
        metadata.update(status="fallback_legacy", fallback_reasons=reasons,
                        warnings=["whole_priority_pool_uses_legacy_category_coverage"])
        for row in pool:
            row["ranking_evidence"].update(status="fallback_legacy", uncalibrated_score=None)
            row["ranking_evidence"]["fallback_reasons"] = copy.deepcopy(reasons)
            row.pop("priority_rank_score", None)
        return result

    try:
        model = load_model()
        _validate_model(model)
        metadata.update(model_id=model["model_id"], training=copy.deepcopy(model["training"]),
                        coverage_bonus=model["coverage_bonus"],
                        coverage_categories=list(model["coverage_categories"]),
                        limitations=list(model["limitations"]))
        result["priority_ranking_policy"] = policy_description()
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return fallback([{"reason": "ranking_model_unavailable", "detail": str(exc)}])
    limit = result.get("requested_count")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECORDS:
        return fallback([{"reason": "invalid_requested_count"}])
    if not pool:
        metadata["status"] = "no_priority_candidates"
        return result
    reasons = []
    ids = [row.get("candidate_id") for row in rows]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        return fallback([{"reason": "invalid_or_ambiguous_candidate_ids"}])
    for row in pool:
        try:
            anion = row["relaxation_priority"]["metrics"]["charge_feasibility"]["system"]["anion"]["element"]
            if anion not in model["training"]["supported_anions"]:
                reasons.append({"candidate_id": row["candidate_id"], "reason": "anion_not_covered_in_training", "anion": anion})
                continue
            if not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
                raise ValueError("missing_or_invalid_verified_cif_sha256")
            if not isinstance(row.get("reduced_formula"), str) or not row["reduced_formula"]:
                raise ValueError("missing_reduced_formula_category")
            group = row.get("space_group_number")
            if isinstance(group, bool) or not isinstance(group, int) or not 1 <= group <= 230:
                raise ValueError("missing_or_invalid_space_group_category")
            row["ranking_evidence"] = _score(row, model)
        except (ValueError, KeyError, TypeError, OverflowError, AttributeError) as exc:
            reasons.append({"candidate_id": row["candidate_id"], "reason": "ranking_features_unavailable", "detail": str(exc)})
    if reasons:
        return fallback(reasons)
    if set(result.get("selected_candidate_ids", [])) - {row["candidate_id"] for row in pool}:
        return fallback([{"reason": "legacy_selection_not_contained_in_priority_pool"}])
    covered = {key: set() for key in model["coverage_categories"]}
    remaining, selected = list(pool), []
    for row in pool:
        row["selected"] = False
        row.pop("selection_rank", None)
        row.pop("selection_reason", None)
        row["priority_rank_score"] = row["ranking_evidence"]["uncalibrated_score"]
    while remaining and len(selected) < limit:
        def gain(row):
            return sum(row[key] not in covered[key] for key in covered) / len(covered)
        chosen = min(remaining, key=lambda row: (
            -row["priority_rank_score"] - model["coverage_bonus"] * gain(row),
            row["sha256"], row["candidate_id"]))
        added = [key for key in covered if chosen[key] not in covered[key]]
        bonus = model["coverage_bonus"] * gain(chosen)
        chosen.update(selected=True, selection_rank=len(selected) + 1,
                      selection_reason={"policy_id": POLICY_ID, "new_category_coverage": added,
                                        "coverage_bonus": bonus, "uncalibrated_score": chosen["priority_rank_score"],
                                        "greedy_selection_score": chosen["priority_rank_score"] + bonus,
                                        "tie_break": "CIF_sha256_then_candidate_id"})
        chosen["ranking_evidence"]["selection_adjustment"] = {
            "new_category_coverage": added, "coverage_bonus": bonus,
            "greedy_selection_score": chosen["priority_rank_score"] + bonus}
        chosen.pop("not_selected_reason", None)
        for key in covered:
            covered[key].add(chosen[key])
        selected.append(chosen)
        remaining.remove(chosen)
    for row in remaining:
        row["not_selected_reason"] = "requested_shortlist_limit_reached_under_frozen_trained_ranking"
    for key, coverage in result.get("coverage", {}).items():
        values = {row[key] for row in selected}
        coverage.update(selected_values=sorted(values), selected_count=len(values),
                        pool_count=len({row[key] for row in pool}))
    result.update(method="verified_cif_ionic_mixed_valence_then_frozen_relative_ranking_v2",
                  selected_candidate_ids=[row["candidate_id"] for row in selected],
                  selected_count=len(selected), target_met=len(selected) == limit)
    if "stage_counts" in result:
        result["stage_counts"].update(selected_count=len(selected), shortfall_count=max(0, limit - len(selected)))
    legacy_ordering_text = {
        "Candidate ordering after the feasibility gate maximizes fixed categorical coverage; it does not rank physical performance or guarantee a globally optimal shortlist.",
        "Greedy fixed-weight category coverage is not a proof of globally optimal selection.",
    }
    metadata["legacy_ordering_limitations"] = [text for text in result.get("limitations", [])
                                               if text in legacy_ordering_text]
    current_limitations = [text for text in result.get("limitations", []) if text not in legacy_ordering_text]
    current_limitations.append(
        "Within the unchanged priority pool, greedy selection combines a frozen development-trained relative score with a weak formula/space-group coverage bonus; it does not guarantee globally optimal selection.")
    result["limitations"] = list(dict.fromkeys(current_limitations + model["limitations"]))
    result["active_ranking_policy"] = POLICY_ID
    policy = result.setdefault("policy", {})
    legacy_policy = {key: policy.pop(key) for key in ("category_weights", "size_bins_sites") if key in policy}
    if legacy_policy:
        policy["legacy_precheck_provenance"] = legacy_policy
    policy["active_ranking_policy"] = POLICY_ID
    metadata.update(status="applied", selected_count=len(selected), requested_count=limit,
                    warnings=sorted({warning for row in pool for warning in row["ranking_evidence"]["warnings"]}))
    return result


def recommend_ranked(records: list[dict], expected_elements, limit: int = 12, *,
                     priority_candidate_ids: list[str] | None = None,
                     priority_selection_basis: str | None = None,
                     symprec: float = 0.1, angle_tolerance: float = 5.0) -> dict:
    """Run the unchanged verified ionic gate, then rank its complete priority pool."""
    base = recommend_with_mixed_valence(records, expected_elements, limit,
        priority_candidate_ids=priority_candidate_ids, priority_selection_basis=priority_selection_basis,
        symprec=symprec, angle_tolerance=angle_tolerance)
    return rerank_priority_pool(base)
