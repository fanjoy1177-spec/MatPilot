"""Finite reviewable alternatives, never auto-applied changes to an accepted plan."""
from __future__ import annotations
from .adapters.mattergen import canonical_hash


def repair_options(plan):
    if plan["state"] == "ready":
        return []
    options = []
    requirements = plan.get("requirements", [])
    def add(kind, title, changes, required, action):
        value = {"kind": kind, "title": title, "requirement_changes": changes,
                 "additional_information_required": required, "proposed_action": action,
                 "sampling_cap_change": 0, "auto_apply": False, "requires_reviewed_new_plan": True,
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
    if "incompatible_conditions" in codes:
        structural = [r for r in requirements if r["kind"] in {"symmetry", "element_set"}]
        if structural:
            add("move_structural_condition_to_check", "将不支持的结构引导改为生成后检查", [], ["explicit search strategy approval"], "Choose a supported checkpoint subset; move listed structural requirements to post-checks with search_mode=generate_then_filter. Do not remove the requirements or numerical targets; revalidation may still block execution.")
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
