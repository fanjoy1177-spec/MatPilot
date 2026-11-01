"""Explicit reduced-composition heuristic inside the unchanged ionic gate.

The rule was chosen using earlier development outcomes, but has no fitted model
and reads no property labels at runtime. A larger reduced formula is a ranking
heuristic, not a predicted energy, stability probability or performance score.
"""
from __future__ import annotations

import copy
import math
import re
from functools import reduce

from .mixed_valence import recommend_with_mixed_valence


POLICY_ID = "ionic_complexity_v3"
MAX_RECORDS = 256
MAX_ATOMS = 1000
LIMITATIONS = [
    "Reduced-formula atom count is an empirical screening heuristic, not a physical-property prediction or a calibrated probability.",
    "The policy choice was informed by development labels from earlier validation cohorts; those cohorts are not independent evidence for this policy.",
    "The primary count is invariant to integer repetition of a cell; the CIF SHA256 tie-break is artifact-dependent and is not a physical invariant.",
    "Larger reduced formulas are not guaranteed to be more stable; transfer to unseen chemical systems requires independent validation.",
    "No formula, symmetry or input-cell-size diversity bonus is applied; a fixed shortlist can lose structural coverage.",
]
_LEGACY_ORDERING_TEXT = {
    "Candidate ordering after the feasibility gate maximizes fixed categorical coverage; it does not rank physical performance or guarantee a globally optimal shortlist.",
    "Greedy fixed-weight category coverage is not a proof of globally optimal selection.",
}


def policy_description() -> dict:
    return {
        "id": POLICY_ID,
        "gate": "unchanged ionic_mixed_valence_v1 priority representatives",
        "primary_key": "descending sum(integer_atom_counts)/gcd(integer_atom_counts)",
        "tie_break": "CIF_sha256_then_candidate_id",
        "fitted_model": False,
        "policy_choice_informed_by_development_labels": True,
        "runtime_property_labels_used": False,
        "score_interpretation": "composition_heuristic_not_predicted_property_or_probability",
        "fallback": "whole_priority_pool_keeps_legacy_selection_on_any_invalid_count_or_sha256",
        "resource_bound": "at most 256 representatives; at most 1000 input atoms per structure; O(n log n) sorting",
        "limitations": list(LIMITATIONS),
    }


def reduced_formula_atom_count(row: dict) -> int:
    """Derive the integer ratio from the gate's verified composition only."""
    try:
        charge = row["relaxation_priority"]["metrics"]["charge_feasibility"]
        if charge["status"] != "feasible" or charge["mixed_possible"] is not True:
            raise ValueError("priority_charge_not_feasible")
        composition = charge["composition"]
        if not isinstance(composition, dict) or not 2 <= len(composition) <= 118:
            raise ValueError("invalid_composition_mapping")
        counts = []
        for symbol, count in composition.items():
            if (not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][a-z]?", symbol)
                    or isinstance(count, bool) or not isinstance(count, int)
                    or not 1 <= count <= MAX_ATOMS):
                raise ValueError("invalid_element_or_atom_count")
            counts.append(count)
        total = sum(counts)
        if total > MAX_ATOMS:
            raise ValueError("atom_budget_exceeded")
        return total // reduce(math.gcd, counts)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("required_verified_composition_missing_or_malformed") from exc


def _is_priority(row: dict) -> bool:
    priority = row.get("relaxation_priority", {})
    return (row.get("qualified") is True and isinstance(priority, dict)
            and priority.get("eligible_for_relaxation_shortlist") is True)


def rerank_by_complexity(base_result: dict) -> dict:
    """Copy and reorder the complete mixed-valence pool; preserve it on failure."""
    if not isinstance(base_result, dict) or not isinstance(base_result.get("records"), list):
        raise ValueError("A complete mixed-valence recommendation result is required.")
    if len(base_result["records"]) > MAX_RECORDS:
        raise ValueError("Ranking accepts at most 256 inspected records.")
    if not all(isinstance(row, dict) for row in base_result["records"]):
        raise ValueError("Recommendation rows must be mappings.")
    result = copy.deepcopy(base_result)
    rows = result["records"]
    pool = [row for row in rows if _is_priority(row)]
    metadata = {
        "policy_id": POLICY_ID, "status": "pending", "fitted_model": False,
        "policy_choice_informed_by_development_labels": True,
        "runtime_property_labels_used": False, "priority_pool_count": len(pool),
        "gate_unchanged": True,
        "legacy_selected_candidate_ids": copy.deepcopy(base_result.get("selected_candidate_ids", [])),
        "fallback_reasons": [], "warnings": [], "limitations": list(LIMITATIONS),
    }
    result["priority_ranking"] = metadata
    result["priority_ranking_policy"] = policy_description()
    for row in rows:
        row["ranking_evidence"] = {
            "status": "pending" if _is_priority(row) else "outside_priority_pool",
            "reduced_formula_atom_count": None, "heuristic_key": None,
            "uncalibrated_score": None,
        }

    def fallback(reasons):
        metadata.update(status="fallback_legacy", fallback_reasons=reasons,
                        warnings=["whole_priority_pool_uses_legacy_category_coverage"])
        for row in pool:
            row["ranking_evidence"].update(
                status="fallback_legacy", reduced_formula_atom_count=None,
                heuristic_key=None, uncalibrated_score=None,
                fallback_reasons=copy.deepcopy(reasons))
        return result

    limit = result.get("requested_count")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECORDS:
        return fallback([{"reason": "invalid_requested_count"}])
    ids = [row.get("candidate_id") for row in rows]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        return fallback([{"reason": "invalid_or_ambiguous_candidate_ids"}])
    old_ids = result.get("selected_candidate_ids")
    if (not isinstance(old_ids, list)
            or any(not isinstance(value, str) for value in old_ids)
            or len(old_ids) != len(set(old_ids))
            or set(old_ids) - {row["candidate_id"] for row in pool}
            or len(old_ids) != min(limit, len(pool))):
        return fallback([{"reason": "legacy_selection_inconsistent_with_priority_pool_or_quota"}])
    if not pool:
        metadata["status"] = "no_priority_candidates"
        return result
    reasons = []
    for row in pool:
        try:
            count = reduced_formula_atom_count(row)
            if not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
                raise ValueError("missing_or_invalid_verified_cif_sha256")
            row["ranking_evidence"].update(
                status="ranked", reduced_formula_atom_count=count,
                heuristic_key=[-count, row["sha256"], row["candidate_id"]],
                score_interpretation="composition_heuristic_not_predicted_property_or_probability")
        except (ValueError, KeyError, TypeError, OverflowError, AttributeError) as exc:
            reasons.append({"candidate_id": row["candidate_id"],
                            "reason": "ranking_inputs_unavailable", "detail": str(exc)})
    if reasons:
        return fallback(reasons)
    ordered = sorted(pool, key=lambda row: tuple(row["ranking_evidence"]["heuristic_key"]))
    selected = ordered[:limit]
    # Resolve coverage before changing any selection fields, so malformed audit
    # fields cannot leave a partially modified selection on fallback.
    try:
        coverage = copy.deepcopy(result.get("coverage", {}))
        for key, item in coverage.items():
            values = {row[key] for row in selected}
            item.update(selected_values=sorted(values), selected_count=len(values),
                        pool_count=len({row[key] for row in pool}))
    except (KeyError, TypeError, AttributeError) as exc:
        return fallback([{"reason": "coverage_audit_unavailable", "detail": str(exc)}])
    for rank, row in enumerate(ordered, 1):
        row["selected"] = rank <= limit
        row.pop("priority_rank_score", None)
        row.pop("selection_rank", None)
        row.pop("selection_reason", None)
        row["ranking_evidence"]["priority_pool_rank"] = rank
        if row["selected"]:
            row.update(selection_rank=rank, selection_reason={
                "policy_id": POLICY_ID,
                "reduced_formula_atom_count": row["ranking_evidence"]["reduced_formula_atom_count"],
                "heuristic_key": copy.deepcopy(row["ranking_evidence"]["heuristic_key"]),
                "uncalibrated_score": None, "tie_break": "CIF_sha256_then_candidate_id",
            })
            row.pop("not_selected_reason", None)
        else:
            row["not_selected_reason"] = "requested_shortlist_limit_reached_under_reduced_composition_heuristic"
    result.update(
        method="verified_cif_ionic_mixed_valence_then_reduced_composition_heuristic_v3",
        selected_candidate_ids=[row["candidate_id"] for row in selected],
        selected_count=len(selected), target_met=len(selected) == limit, coverage=coverage,
        active_ranking_policy=POLICY_ID)
    if "stage_counts" in result:
        result["stage_counts"].update(selected_count=len(selected), shortfall_count=max(0, limit - len(selected)))
    metadata["legacy_ordering_limitations"] = [
        text for text in result.get("limitations", []) if text in _LEGACY_ORDERING_TEXT]
    result["limitations"] = list(dict.fromkeys([
        text for text in result.get("limitations", []) if text not in _LEGACY_ORDERING_TEXT] + LIMITATIONS))
    policy = result.setdefault("policy", {})
    legacy_policy = {key: policy.pop(key) for key in ("category_weights", "size_bins_sites") if key in policy}
    if legacy_policy:
        policy["legacy_precheck_provenance"] = legacy_policy
    policy["active_ranking_policy"] = POLICY_ID
    metadata.update(status="applied", selected_count=len(selected), requested_count=limit)
    return result


def recommend_complexity(records: list[dict], expected_elements, limit: int = 12, *,
                         priority_candidate_ids: list[str] | None = None,
                         priority_selection_basis: str | None = None,
                         symprec: float = 0.1, angle_tolerance: float = 5.0) -> dict:
    """Run the unchanged ionic gate, then apply the explicit composition rule."""
    base = recommend_with_mixed_valence(
        records, expected_elements, limit, priority_candidate_ids=priority_candidate_ids,
        priority_selection_basis=priority_selection_basis, symprec=symprec,
        angle_tolerance=angle_tolerance)
    return rerank_by_complexity(base)
