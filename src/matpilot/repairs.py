"""Finite reviewable alternatives, never auto-applied changes to an accepted plan."""
from __future__ import annotations
from copy import deepcopy
from .adapters.mattergen import CHECKPOINTS, canonical_hash


def repair_options(plan):
    if plan["state"] == "ready":
        return []
    options = []
    requirements = plan.get("requirements", [])
    def add(kind, title, changes, required, action, execution_changes=None):
        value = {"kind": kind, "title": title, "requirement_changes": changes,
                 "additional_information_required": required, "proposed_action": action,
                 "sampling_cap_change": 0, "auto_apply": False, "requires_reviewed_new_plan": True,
                 "preserved_requirement_ids": [r["requirement_id"] for r in requirements],
                 "execution_changes": execution_changes or {},
                 "scope": "finite catalog alternative; not a global minimum repair or a readiness guarantee"}
        value["option_id"] = canonical_hash(value)[:16]
        options.append(value)
    stoich = [r for r in requirements if r["kind"] == "stoichiometry"]
    if plan["model_id"] == "diffcsp" and not plan.get("template"):
        add("provide_template", "补充并核对结构模板", [], ["operator-registered CIF template matching composition and symmetry"], "Keep the requested formula and symmetry; prepare a new DiffCSP plan using a registered template.")
    if stoich and plan["model_id"] == "mattergen":
        add("switch_to_template_csp", "保留固定化学式，选择模板预测", [], ["reviewed template", "verified space group"], "Select DiffCSP with matching template; all existing requirements must be remapped and checked again.")
        add("explicit_search", "保留化学计量检查，接受有限预算生成后筛选", [], ["explicit acceptance of generate-then-filter search"], "Set search_mode=generate_then_filter; preserve stoichiometry as a hard post-check and keep the existing sampling cap. Success is not guaranteed.")
    codes = {i["code"] for i in plan["issues"]}
    if "incompatible_conditions" in codes and plan["model_id"] == "mattergen":
        structural = [r for r in requirements if r["kind"] in {"symmetry", "element_set"}
                      and r["property"] in plan["conditions"]]
        movable = {r["property"] for r in structural}
        conditions = plan["conditions"]
        fixed = set(conditions) - movable  # Numerical targets are never removed.
        candidates, seen = [], set()
        for checkpoint, fields in CHECKPOINTS.items():
            fields = set(fields)
            if not fixed <= fields or not fields <= set(conditions) or fields == set(conditions):
                continue
            if not set(conditions) - fields <= movable or tuple(sorted(fields)) in seen:
                continue
            seen.add(tuple(sorted(fields)))
            candidates.append((checkpoint, fields))
        # Keep the most conditions among this finite published catalog. This is
        # not a scientific feasibility claim or a global minimum repair search.
        retained = max((len(fields) for _, fields in candidates), default=-1)
        for checkpoint, fields in candidates:
            if len(fields) != retained:
                continue
            changes = []
            for r in structural:
                if r["property"] in fields:
                    continue
                unchanged = {key: deepcopy(r.get(key)) for key in
                             ("kind", "property", "value", "unit", "strength", "relation")}
                changes.append({"requirement_id": r["requirement_id"],
                                "before": {**unchanged, "stages": ["generation", "post_check"]},
                                "after": {**unchanged, "stages": ["search", "post_check"],
                                          "checker": "element_equality" if r["kind"] == "element_set" else "space_group"},
                                "effect": "Remove this generation guide only; retain the original hard/goal result requirement and explicit post-check. Success within the unchanged sampling budget is not guaranteed."})
            before_execution = {"model_id": plan["model_id"], "checkpoint": plan.get("checkpoint"),
                                "conditions": deepcopy(conditions), "search_mode": plan.get("search_mode", "direct"),
                                "num_samples": plan["num_samples"], "batch_size": plan["batch_size"],
                                "seed": plan["seed"], "valid_candidates_target": plan.get("valid_candidates_target")}
            after_execution = {**before_execution, "checkpoint": checkpoint,
                               "conditions": {key: deepcopy(value) for key, value in conditions.items() if key in fields},
                               "search_mode": "generate_then_filter", "unconditional": not fields}
            add("move_structural_condition_to_check", "保留结果要求，使用 " + checkpoint + " 引导后筛选", changes,
                ["explicit acceptance of each listed generation-guide removal; new plan review"],
                "Use the explicit execution_changes with every existing requirement retained. Requirements that explicitly demand a generation condition need a user-approved task revision; a post-check is not equivalent to that demand. Revalidation may still block execution.",
                {"before": before_execution, "after": after_execution})
    unknown_hard = [r for r in requirements if r["strength"] == "hard" and r["kind"] in {"research_goal", "numeric_bound", "numeric_target"}]
    if unknown_hard:
        add("provide_evaluator", "保留科学硬要求，先补齐评价方法", [], ["validated evaluator, method identity, applicable units/reference set"], "Keep execution blocked until an applicable evaluator is configured and verified. This plugin version has no general scientific evaluator.")
        changes = [{"requirement_id": r["requirement_id"], "before": {"kind": r["kind"], "strength": r["strength"], "value": r["value"], "relation": r["relation"], "unit": r.get("unit")},
                    "after": {"kind": "research_goal", "strength": "goal", "verification_status": "unmeasured"},
                    "effect": "Removes the guarantee/bound from acceptance; retains original value and units as an explicitly unverified research target."} for r in unknown_hard]
        add("weaken_guarantee", "仅在用户同意后，将性能保证改为未评价目标", changes, ["explicit user revision of the original request"], "Record the revised request alongside the prior plan; never edit old source quotes or quietly relabel a hard constraint.")
    if not options:
        add("clarify_exact_issue", "按具体错误补充或修正输入", [], [i.get("message", "Review the issue") for i in plan["issues"]], "Retain the full original request and prepare a corrected plan; no automatic parameter changes.")
    return options[:5]
