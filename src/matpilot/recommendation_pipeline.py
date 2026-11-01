"""Generic, evidence-aware candidate planning with triage before deduplication.

This module supplies no chemistry model and performs no property prediction.
An explicitly supplied deterministic callback owns chemical applicability. The
service supplying rank_keys must verify metric identity and comparability before
calling this function; finite numeric keys alone do not establish evidence.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
import hashlib
from pathlib import Path

from .recommendations import MAX_CANDIDATES, MAX_CIF_BYTES, MATCHER_PARAMETERS, _inspect

PIPELINE_ID = "triage_before_representative_v1"
_CATEGORIES = ("reduced_formula", "space_group_number", "size_bin")
_TIERS = {"priority_relaxation": 0, "manual_review": 1, "defer": 2, "not_eligible": 3}


def _nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def _finite_number(value):
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _review(reason, error=None):
    result = {"tier": "manual_review", "priority": "人工复核", "score": None,
              "eligible_for_relaxation_shortlist": False, "issues": [reason], "metrics": {}}
    if error is not None:
        result["analysis_error"] = error
    return result


def _not_eligible(reasons):
    return {"tier": "not_eligible", "priority": "未通过结构或身份检查", "score": None,
            "eligible_for_relaxation_shortlist": False, "issues": list(reasons), "metrics": {}}


def _evaluate(callback, record, expected, symprec, angle_tolerance):
    try:
        item = callback(deepcopy(record), list(expected), symprec=symprec, angle_tolerance=angle_tolerance)
        if not isinstance(item, dict):
            return _review("triage_callback_returned_invalid_schema")
        item = deepcopy(item)
        tier = item.get("tier")
        if tier not in _TIERS:
            return _review("triage_callback_returned_unknown_tier")
        if (type(item.get("eligible_for_relaxation_shortlist")) is not bool
                or item["eligible_for_relaxation_shortlist"] != (tier == "priority_relaxation")):
            return _review("triage_callback_returned_inconsistent_eligibility")
        if item.get("score") is not None and not _finite_number(item["score"]):
            return _review("triage_callback_returned_invalid_score")
        issues = item.get("issues", [])
        if not isinstance(issues, list) or any(not isinstance(value, str) for value in issues):
            return _review("triage_callback_returned_invalid_issues")
        item.setdefault("issues", [])
        item.setdefault("score", None)
        item.setdefault("priority", {"priority_relaxation": "优先计算", "manual_review": "人工复核",
                                     "defer": "暂缓", "not_eligible": "未通过策略检查"}[tier])
        return item
    except Exception as exc:
        return _review("triage_callback_unavailable", f"{type(exc).__name__}: {exc}")


def _validate_inputs(records, expected_elements, limit, triage_callback, policy,
                     rank_keys, ranking_metadata, missing_rank_policy,
                     priority_candidate_ids, priority_selection_basis, symprec, angle_tolerance):
    from pymatgen.core import Element
    if (not isinstance(records, list) or not 1 <= len(records) <= MAX_CANDIDATES
            or not all(isinstance(record, dict) for record in records)):
        raise ValueError("Provide 1 to 256 candidate records.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError("limit must be an integer from 1 to 256.")
    if (not isinstance(expected_elements, (list, tuple, set, frozenset)) or not expected_elements
            or any(not isinstance(value, str) for value in expected_elements)):
        raise ValueError("An explicit nonempty set of element symbols is required.")
    try:
        expected = sorted({Element(value).symbol for value in expected_elements})
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Expected elements must be valid element symbols.") from exc
    if any(not _finite_number(value) or value <= 0 for value in (symprec, angle_tolerance)):
        raise ValueError("Symmetry tolerances must be finite positive numbers.")
    ids = [record.get("candidate_id") for record in records]
    if not all(_nonempty_text(cid) for cid in ids):
        raise ValueError("Every record requires a nonempty candidate_id.")
    counts = Counter(ids)
    if not callable(triage_callback):
        raise ValueError("An explicit deterministic triage callback is required.")
    if (not isinstance(policy, dict) or not all(_nonempty_text(policy.get(k)) for k in ("id", "scope", "evidence_scope"))
            or not isinstance(policy.get("score_order"), str) or policy["score_order"] not in {"higher_is_better", "none"}):
        raise ValueError("Declare policy id, scope, evidence_scope and score_order (higher_is_better or none).")
    allowed = None
    if priority_candidate_ids is not None:
        if (not isinstance(priority_candidate_ids, list) or not all(isinstance(cid, str) for cid in priority_candidate_ids)
                or len(set(priority_candidate_ids)) != len(priority_candidate_ids)
                or set(priority_candidate_ids) - set(ids)):
            raise ValueError("Priority selector must contain distinct known candidate IDs.")
        if not _nonempty_text(priority_selection_basis):
            raise ValueError("An explicit priority selection basis is required.")
        allowed = set(priority_candidate_ids)
    elif priority_selection_basis is not None:
        raise ValueError("A selection basis requires an explicit priority selector.")
    if not isinstance(missing_rank_policy, str) or missing_rank_policy not in {"review", "error"}:
        raise ValueError("missing_rank_policy must be review or error; missing evidence never receives a best value.")
    normalized_ranks = None
    if rank_keys is not None:
        if not isinstance(rank_keys, dict):
            raise ValueError("rank_keys must be a candidate-ID keyed dictionary or None.")
        if (not isinstance(ranking_metadata, dict)
                or not all(_nonempty_text(ranking_metadata.get(k)) for k in ("id", "description", "evidence_scope"))):
            raise ValueError("Ranking requires explicit id, description and evidence_scope metadata.")
        normalized_ranks, arities = {}, set()
        for cid, key in rank_keys.items():
            if not isinstance(cid, str) or cid not in counts or counts[cid] != 1:
                raise ValueError("Ranking keys must refer to known unambiguous candidate IDs.")
            if not isinstance(key, (tuple, list)) or not key or not all(_finite_number(value) for value in key):
                raise ValueError("Each rank key must be a nonempty tuple/list of finite non-boolean numbers.")
            normalized_ranks[cid] = tuple(key)
            arities.add(len(key))
        if len(arities) > 1:
            raise ValueError("All rank keys must have the same length.")
    elif ranking_metadata is not None:
        raise ValueError("Ranking metadata requires rank_keys; use None for no evidence ranking.")
    return expected, counts, allowed, normalized_ranks


def recommend_pipeline(records: list[dict], expected_elements, limit: int = 12, *,
                       triage_callback, policy: dict, rank_keys: dict | None = None,
                       ranking_metadata: dict | None = None, missing_rank_policy: str = "review",
                       priority_candidate_ids: list[str] | None = None,
                       priority_selection_basis: str | None = None,
                       symprec: float = 0.1, angle_tolerance: float = 5.0) -> dict:
    """Inspect and triage all permitted candidates before selecting representatives.

    rank_keys use ascending lexicographic order: lower keys are preferred. The
    caller must bind comparable scientific evidence to the exact structure and
    normalize desired directions; this function verifies only key shape/identity.
    None retains coverage-first cross-representative selection. An empty dict
    requests evidence ranking with no available evidence, so eligible candidates
    go to review (or raise when missing_rank_policy='error'). No best-value fill.

    Callback signature: callback(record, expected_elements, *, symprec,
    angle_tolerance), returning tier, eligible_for_relaxation_shortlist, optional
    finite score, issues and descriptive fields. Unknown/unsupported systems must
    return manual_review; exceptions and invalid callback results become review.
    Policy-local scores select representatives; they are never probabilities.
    """
    from pymatgen.analysis.structure_matcher import StructureMatcher
    expected, counts, allowed, ranks = _validate_inputs(
        records, expected_elements, limit, triage_callback, policy, rank_keys, ranking_metadata,
        missing_rank_policy, priority_candidate_ids, priority_selection_basis, symprec, angle_tolerance)
    items = []
    missing = []
    for record in sorted(records, key=lambda item: item["candidate_id"]):
        row, crystal = _inspect(record, set(expected), symprec, angle_tolerance)
        cid = row["candidate_id"]
        if counts[cid] != 1:
            row["exclusion_reasons"].append("ambiguous_duplicate_candidate_id")
            crystal = None
        structural_reasons = list(row["exclusion_reasons"])
        row["structural_precheck_qualified"] = crystal is not None
        caller_allowed = allowed is None or cid in allowed
        row["caller_allowed"] = caller_allowed
        if not caller_allowed:
            row["exclusion_reasons"].append("outside_explicit_priority_selector")
        if crystal is None or not caller_allowed:
            item = _not_eligible(row["exclusion_reasons"])
        else:
            item = _evaluate(triage_callback, record, expected, symprec, angle_tolerance)
        row["ranking_status"] = ("not_requested" if ranks is None else
                                 "provided_key_requires_caller_verified_evidence" if cid in ranks else "missing_comparable_evidence")
        row["evidence_rank_key"] = list(ranks[cid]) if ranks is not None and cid in ranks else None
        if ranks is not None and item["eligible_for_relaxation_shortlist"] and cid not in ranks:
            missing.append(cid)
            if missing_rank_policy == "review":
                row["upstream_triage"] = item
                item = _review("missing_comparable_ranking_evidence")
        row["relaxation_priority"] = item
        row["structural_exclusion_reasons"] = structural_reasons
        row["structural_representative_qualified"] = False
        row["representative_id"] = None
        items.append({"row": row, "crystal": crystal, "allowed": caller_allowed, "record": record})
    # A trusted callback may still have a bug, and another process may edit an
    # earlier candidate. Recheck all bound bytes after callback evaluation.
    for entry in items:
        if entry["crystal"] is None:
            continue
        row, record = entry["row"], entry["record"]
        try:
            with Path(record["path"]).open("rb") as handle:
                content = handle.read(MAX_CIF_BYTES + 1)
            unchanged = len(content) <= MAX_CIF_BYTES and hashlib.sha256(content).hexdigest() == record["sha256"]
        except (OSError, ValueError, TypeError, KeyError):
            unchanged = False
        if not unchanged:
            reason = "artifact_sha256_mismatch_after_triage"
            row["exclusion_reasons"].append(reason)
            row["structural_exclusion_reasons"].append(reason)
            row["upstream_triage"] = row.get("upstream_triage", row["relaxation_priority"])
            row["relaxation_priority"] = _not_eligible(row["exclusion_reasons"])
            row["structural_precheck_qualified"] = False
            row["artifact_verified"] = False
            entry["crystal"] = None
    still_valid = {entry["row"]["candidate_id"] for entry in items if entry["crystal"] is not None and entry["allowed"]}
    missing = [cid for cid in missing if cid in still_valid]
    if missing and missing_rank_policy == "error":
        raise ValueError("Eligible candidates lack comparable ranking evidence: " + ", ".join(missing))

    def representative_key(entry):
        row = entry["row"]
        item = row["relaxation_priority"]
        if ranks is not None:
            evidence = (row["candidate_id"] not in ranks, ranks.get(row["candidate_id"], ()))
        else:
            evidence = ()
        score = item.get("score") if policy["score_order"] == "higher_is_better" else None
        heuristic = (score is None, -score if score is not None else 0) if policy["score_order"] == "higher_is_better" else ()
        return (_TIERS[item["tier"]], evidence, heuristic, len(item["issues"]), row["site_count"], row["candidate_id"])

    matcher = StructureMatcher(**MATCHER_PARAMETERS)
    groups, unknown = [], []
    for entry in sorted((entry for entry in items if entry["crystal"] is not None and entry["allowed"]), key=representative_key):
        row = entry["row"]
        try:
            prior = next((group for group in groups
                if row["reduced_formula"] == group["row"]["reduced_formula"]
                and matcher.fit(entry["crystal"], group["crystal"])), None)
        except Exception as exc:
            row["exclusion_reasons"].append("structural_equivalence_unknown")
            row["structural_exclusion_reasons"].append("structural_equivalence_unknown")
            row["analysis_error"] = f"{type(exc).__name__}: {exc}"
            unknown.append(row["candidate_id"])
            continue
        if prior is None:
            groups.append(entry)
            row["representative_id"] = row["candidate_id"]
            row["structural_representative_qualified"] = True
        else:
            row["representative_id"] = prior["row"]["candidate_id"]
            row["exclusion_reasons"].append("equivalent_to_qualified_representative")
            row["structural_exclusion_reasons"].append("equivalent_to_qualified_representative")
    representatives = []
    for entry in items:
        row = entry["row"]
        eligible = row["relaxation_priority"]["eligible_for_relaxation_shortlist"]
        if entry["crystal"] is not None and entry["allowed"] and not eligible:
            row["exclusion_reasons"].append("not_in_declared_priority_pool")
        row["qualified"] = bool(row["structural_representative_qualified"] and eligible and not row["exclusion_reasons"])
        if row["qualified"]:
            representatives.append(row)
    covered = {key: set() for key in _CATEGORIES}
    remaining, selected = list(representatives), []
    while remaining and len(selected) < limit:
        def selection_key(row):
            gain = sum(row[key] not in covered[key] for key in _CATEGORIES)
            tie = (-gain, row["site_count"], row["candidate_id"])
            return (ranks[row["candidate_id"]], *tie) if ranks is not None else tie
        chosen = min(remaining, key=selection_key)
        added = [key for key in _CATEGORIES if chosen[key] not in covered[key]]
        chosen.update(selected=True, selection_rank=len(selected) + 1,
                      selection_reason={"new_category_coverage": added, "coverage_gain": len(added),
                          "ranking_mode": "evidence_key_then_coverage" if ranks is not None else "coverage_only",
                          "evidence_rank_key": list(ranks[chosen["candidate_id"]]) if ranks is not None else None,
                          "tie_break": "equal_evidence_keys_only_then_coverage_sites_id" if ranks is not None else "fewer_input_sites_then_candidate_id"})
        for key in _CATEGORIES:
            covered[key].add(chosen[key])
        selected.append(chosen)
        remaining.remove(chosen)
    for row in remaining:
        row["not_selected_reason"] = "requested_shortlist_limit_reached_under_declared_ranking_policy"
    for entry in items:
        row = entry["row"]
        if row["exclusion_reasons"]:
            row["not_selected_reason"] = "; ".join(row["exclusion_reasons"])
    caller_selector = ({"candidate_ids": sorted(allowed), "basis": priority_selection_basis,
                        "evidence_scope": "explicit_caller_preference_not_property_validation"}
                       if allowed is not None else None)
    tiers = Counter(entry["row"]["relaxation_priority"]["tier"] for entry in items)
    return {"schema_version": 1, "method": PIPELINE_ID, "expected_elements": expected,
            "input_count": len(records), "requested_count": limit, "selected_count": len(selected),
            "target_met": len(selected) == limit, "eligible_representative_count": len(representatives),
            "selected_candidate_ids": [row["candidate_id"] for row in selected],
            "records": [entry["row"] for entry in items],
            "coverage": {key: {"selected_values": sorted(values), "selected_count": len(values),
                               "pool_count": len({row[key] for row in representatives})} for key, values in covered.items()},
            "priority_selector": deepcopy(caller_selector), "caller_priority_selector": deepcopy(caller_selector),
            "relaxation_triage_policy": deepcopy(policy), "triage_policy": deepcopy(policy),
            "policy": {"category_weights": {key: 1 for key in _CATEGORIES},
                       "size_bins_sites": ["1-10", "11-20", "21+"], "symprec_angstrom": symprec,
                       "angle_tolerance_degrees": angle_tolerance, "structure_matcher": dict(MATCHER_PARAMETERS),
                       "minimum_contact_angstrom": .5, "occupancy_tolerance": 1e-6,
                       "requires_explicit_cif_occupancies": True},
            "ranking_audit": {"enabled": ranks is not None, "key_order": "ascending_lexicographic" if ranks is not None else None,
                              "metadata": deepcopy(ranking_metadata), "missing_rank_policy": missing_rank_policy,
                              "missing_priority_candidate_ids": sorted(missing),
                              "provided_candidate_ids": sorted(ranks) if ranks is not None else [],
                              "evidence_comparability_responsibility": "caller_must_verify_exact_structure_version_and_comparable_metrics",
                              "cross_representative_order": "evidence_first_diversity_only_on_equal_keys" if ranks is not None else "fixed_category_coverage"},
            "representative_selection": {"order": "eligible_tier_then_evidence_key_then_policy_local_score_issues_sites_id",
                                         "structural_representatives": [entry["row"]["candidate_id"] for entry in groups],
                                         "priority_representatives": [row["candidate_id"] for row in representatives],
                                         "equivalence_unknown_candidate_ids": unknown},
            "stage_counts": {"input_count": len(records),
                             "structural_precheck_count": sum(entry["row"]["structural_precheck_qualified"] for entry in items),
                             "caller_allowed_precheck_count": sum(entry["row"]["structural_precheck_qualified"] and entry["allowed"] for entry in items),
                             "structural_representative_count": len(groups),
                             "priority_relaxation_count": tiers["priority_relaxation"],
                             "manual_review_count": tiers["manual_review"], "defer_count": tiers["defer"],
                             "not_eligible_count": tiers["not_eligible"],
                             "priority_representative_count": len(representatives),
                             "selected_count": len(selected), "requested_count": limit,
                             "shortfall_count": max(0, limit-len(selected))},
            "limitations": ["The pipeline provides no default chemical system or property predictor; callback scope must be declared.",
                            "Priority labels describe next-step planning, not validated physical performance or successful relaxation.",
                            "Policy-local heuristic scores are not calibrated probabilities and are not compared across policies.",
                            "Numeric rank keys are accepted only as caller-validated comparable evidence; this layer does not verify scientific provenance.",
                            "Structural equivalence and symmetry depend on declared tolerances; unknown equivalence never receives a fabricated representative.",
                            "Site count is an input-cell scale proxy, not measured compute time or material quality.",
                            "No generation, ML property prediction, relaxation, DFT or API request is performed by this pipeline."]}
