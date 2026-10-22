"""Capability cards separate implemented contracts from verified model readiness."""
from __future__ import annotations
from .adapters import ADAPTERS
from .templates import template_catalog


def cards(settings):
    result = []
    for model_id, adapter in ADAPTERS.items():
        card = adapter.describe()
        identity = settings.model_identities.get(model_id, {})
        card.update(code_identity={"operator_record": identity.get("code"), "verification": "operator_record_only_not_runtime_verified"},
                    weights_identity={"operator_record": identity.get("weights"), "verification": "operator_record_only_not_runtime_verified"},
                    availability={"adapter_implemented": True, "backend": settings.backend,
                                  "real_compute_enabled": settings.real_compute_enabled,
                                  "real_model_acceptance": "not_verified_for_current_release",
                                  "status": "demo_only" if settings.backend == "demo" else "disabled_by_operator" if not settings.real_compute_enabled else "requires_environment_and_generation_acceptance"})
        if model_id == "mattergen":
            card.update(required_inputs=["supported conditions or explicit unconditional"],
                        condition_combinations=list(card["checkpoints"].values()),
                        outputs=["CIF archive"], checkers=["element equality", "space group", "reduced stoichiometry after explicit search", "structural sanity", "structure matching"])
        else:
            card["templates"] = template_catalog(settings)
            card["configured_bridge"] = all(k in settings.diffcsp for k in ("python", "upstream_root", "model_root", "model_revision", "weight_manifest"))
        result.append(card)
    return result
