"""Versioned, material-agnostic recommendation routing; no computation is run."""
from __future__ import annotations

from .mixed_valence import analyze_mixed_valence_candidate, describe_ionic_system
from .recommendation_pipeline import recommend_pipeline

POLICIES = {"triage_v2", "structural_v2", "ionic_mixed_valence_v2", "evidence_v1"}
MATERIAL_CLASSES = {"auto", "ionic", "metallic", "covalent", "other", "unknown"}


def policy_cards():
    return {
        "triage_v2": {"method": "Inspect every candidate before choosing representatives; route declared chemistry, then coverage.",
                      "scope": "Periodic ordered crystals; chemical checks only within the declared ionic rule scope. No Fe-Ni-O restriction."},
        "structural_v2": {"method": "Per-candidate structural readiness before representative selection.",
                          "scope": "No ionic, stability or property claim for metals, alloys, covalent or unclassified materials."},
        "ionic_mixed_valence_v2": {"method": "The v1 formal charge solver applied before representative choice.",
                                  "scope": "Same bounded ionic rule scope as v1; feasibility is not a physical prediction."},
        "evidence_v1": {"method": "Explicit identity-bound comparable evidence first, diversity only breaks quality ties.",
                        "scope": "No prediction is run. Unknown evidence remains review; cross-composition raw energies cannot rank."},
    }


def _readiness(record, expected_elements, **kwargs):
    # The pipeline has already checked identity, occupancy, exact elements and
    # geometry. This tag is deliberately not a chemical-feasibility assertion.
    return {"policy_id": "structural_readiness_v2", "tier": "priority_relaxation",
            "priority": "结构准备度通过，待性质验证", "score": None,
            "eligible_for_relaxation_shortlist": True, "issues": [], "metrics": {},
            "evidence_scope": "structural_readiness_only; chemical_feasibility_not_assessed"}


def recommend_v2(records, expected_elements, limit=12, selection_policy="triage_v2", *,
                 material_class="auto", evidence=None, **priority):
    if not isinstance(selection_policy, str) or selection_policy not in POLICIES:
        raise ValueError("Unknown v2 recommendation policy.")
    if not isinstance(material_class, str) or material_class not in MATERIAL_CLASSES:
        raise ValueError("material_class must be auto, ionic, metallic, covalent, other or unknown.")
    support = describe_ionic_system(expected_elements)
    ionic = support["supported"] and material_class in {"auto", "ionic"}
    if selection_policy == "ionic_mixed_valence_v2" and not ionic:
        raise ValueError("Declared material class or elements are outside the ionic rule scope.")
    if selection_policy == "evidence_v1" and evidence is None:
        raise ValueError("evidence_v1 needs an objective and explicit metric selections.")
    if evidence is not None and selection_policy not in {"triage_v2", "evidence_v1"}:
        raise ValueError("Use triage_v2 or evidence_v1 when requesting evidence ranking.")
    effective = ("evidence_v1" if evidence is not None else
                 "ionic_mixed_valence_v2" if ionic and selection_policy != "structural_v2" else "structural_v2")
    callback = analyze_mixed_valence_candidate if effective == "ionic_mixed_valence_v2" else _readiness
    policy = {"id": effective, "scope": policy_cards()[effective]["scope"],
              "evidence_scope": "comparable_selected_metric_evidence" if evidence is not None else
                                "bounded_formal_charge_heuristic" if ionic and effective != "structural_v2" else
                                "structural_readiness_only; chemical_feasibility_not_assessed",
              "score_order": "none"}
    ranking = {} if evidence is None else {
        "rank_keys": evidence["rank_keys"], "ranking_metadata": evidence["ranking_metadata"]}
    result = recommend_pipeline(records, expected_elements, limit, triage_callback=callback,
                                policy=policy, **ranking, **priority)
    fallback = selection_policy == "triage_v2" and evidence is None and not ionic
    result.update(requested_selection_policy=selection_policy, effective_selection_policy=effective,
                  fallback_reason="ionic_rule_not_applicable; structural_readiness_only" if fallback else None,
                  ionic_system_support=support,
                  selection_policy_resolution={"declared_material_class": material_class,
                      "ionic_policy_applicable": ionic, "fallback_applied": fallback,
                      "chemical_feasibility_policy_invoked": effective == "ionic_mixed_valence_v2",
                      "property_prediction_performed": False,
                      "evidence_ranking_performed": evidence is not None,
                      "element_based_routing_does_not_identify_bonding": material_class == "auto"})
    if evidence is not None:
        result.update(evidence_ranking={k: v for k, v in evidence.items() if k != "rank_keys"})
        result["limitations"].append("Ranking is conditional on the selected method, structure version and declared property; model applicability and scientific accuracy require independent validation. Chemical heuristics do not override compatible property evidence.")
    return result
