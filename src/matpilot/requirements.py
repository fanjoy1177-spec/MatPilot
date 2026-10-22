"""Requirement proposals, source anchors and deterministic execution mappings.

The LLM proposes records; this module does not claim to understand arbitrary text.
Offsets are Unicode code-point offsets (Python slicing), not UTF-16 positions.
"""
from __future__ import annotations

import math
import re
from copy import deepcopy

from .adapters.mattergen import CONVERSIONS, ELEMENTS, UNITS


KINDS = {
    "element_set", "stoichiometry", "symmetry", "sample_budget",
    "numeric_target", "numeric_bound", "research_goal", "execution_parameter", "template",
}
NUMERIC = re.compile(r"(?<![\w.])[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
CHEMICAL_SET = re.compile(r"(?<![A-Za-z])(?:[A-Z][a-z]?-)+[A-Z][a-z]?(?![A-Za-z])")
FORMULA = re.compile(r"(?<![A-Za-z])(?:[A-Z][a-z]?\d*){2,}(?![A-Za-z])")
GOAL_TERMS = ("析氧", "耐氯", "耐腐蚀", "催化活性", "OER", "corrosion", "catalytic")
LOWER_BOUND_WORDS = r"至少|不低于|不小于|下限(?:为|是)?|>=|≥|at\s+least"
UPPER_BOUND_WORDS = r"至多|最多|不超过|不高于|不大于|上限(?:为|是)?|<=|≤|at\s+most"
BOUND_WORDS = LOWER_BOUND_WORDS + "|" + UPPER_BOUND_WORDS
BOUND_SIGNAL = re.compile(r"(?:" + BOUND_WORDS + r")\s*[+-]?\d+(?:\.\d+)?", re.IGNORECASE)
UNIT_TOKEN = re.compile(r"(?<![A-Za-z])(?:meV/atom|eV/atom|mu_B/angstrom\^3|MPa|GPa|meV|eV)(?![A-Za-z/])")


def contract() -> dict:
    return {
        "schema_version": 2,
        "source": "Prefer {quote: exact original substring}. The program resolves a unique exact match. If repeated, include correct start and end offsets; supplied offsets are never silently repaired.",
        "source_offsets": "Zero-based Unicode code points; end is exclusive; quote must match raw_request[start:end].",
        "kinds": sorted(KINDS),
        "required_fields": ["requirement_id", "source", "kind", "property", "value", "strength", "relation"],
        "optional_fields": ["unit"],
        "strength": ["hard", "goal"],
        "kind_rules": {
            "element_set": "property=chemical_system; value=hyphen-separated elements; relation=equal; no unit",
            "stoichiometry": "property=chemical_formula; value=original formula; relation=equal; no unit",
            "symmetry": "property=space_group; value=integer 1..230; relation=equal; no unit",
            "sample_budget": "property=max_samples with relation=at_most or equal; valid_candidates_target with relation=at_least or equal; positive integer, no unit",
            "execution_parameter": "ONLY property=batch_size or seed; integer value, no unit. batch_size accepts equal (exact size) or at_most (hard per-batch cap; actual size may be smaller). seed requires equal. Preserve explicit upper-bound language as at_most; never relabel an upper or lower bound as equal, and do not interpret a batch minimum as a supported cap.",
            "numeric_target": "property from model units; original numeric value and unit; relation=target; strength=goal",
            "numeric_bound": "property from model units; original value and unit; relation=at_most, at_least or range; preserve hard bounds",
            "template": "property=template_id; value=registered identifier; relation=equal; no unit",
            "research_goal": "property=descriptive name; value=qualitative scientific objective text; relation=goal (required); no unit. review_targets may be empty: records automatically populate them. If supplied, copy exactly the same value text."
        },
        "workflow_instructions": "Demo-only, wait for approval, plan-only and truthful reporting are workflow instructions retained in full raw_request and enforced through backend/host policies, not scientific research_goal or invented execution_parameter fields. Disclose actual backend and evidence limits; never claim the requirement ledger verifies arbitrary workflow prose.",
        "note": "Every natural-language request needs anchored requirement proposals. Mappings are computed by MatPilot, not supplied by the LLM. Lexical coverage is not semantic completeness.",
        "example": {
            "raw_request": "Fe-Ni-O，最多4个",
            "requirements": [
                {"requirement_id": "r1", "source": {"quote": "Fe-Ni-O"},
                 "kind": "element_set", "property": "chemical_system", "value": "Fe-Ni-O", "strength": "hard", "relation": "equal"},
                {"requirement_id": "r2", "source": {"quote": "最多4个"},
                 "kind": "sample_budget", "property": "max_samples", "value": 4, "strength": "hard", "relation": "at_most"},
            ],
        },
    }


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("A finite number is required.")
    return value


def _formula(value: str) -> str:
    from pymatgen.core import Composition
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A chemical formula is required.")
    composition = Composition(value, strict=True)
    if not composition or any(x <= 0 for x in composition.values()):
        raise ValueError("Invalid chemical stoichiometry.")
    return composition.reduced_formula


def _normalize(record: dict) -> dict:
    value = record["value"]
    kind, prop = record["kind"], record["property"]
    relation = record["relation"]
    unit = record.get("unit")
    if kind == "element_set":
        if prop != "chemical_system" or relation != "equal" or unit is not None:
            raise ValueError("Element sets use chemical_system, equal and no unit.")
        elements = value.split("-") if isinstance(value, str) else []
        if not elements or any(e not in ELEMENTS for e in elements) or len(set(elements)) != len(elements):
            raise ValueError("Use distinct hyphen-separated element symbols.")
        value = "-".join(sorted(elements))
    elif kind == "stoichiometry":
        if prop != "chemical_formula" or relation != "equal" or unit is not None:
            raise ValueError("Stoichiometry uses chemical_formula, equal and no unit.")
        value = _formula(value)
    elif kind in {"symmetry", "sample_budget", "execution_parameter"}:
        allowed = {"symmetry": {"space_group"}, "sample_budget": {"max_samples", "valid_candidates_target"},
                   "execution_parameter": {"batch_size", "seed"}}[kind]
        if prop not in allowed or unit is not None or relation not in {"equal", "at_most", "at_least"}:
            raise ValueError("Invalid property, unit or relation for this requirement.")
        if type(value) is not int or value < (0 if prop == "seed" else 1):
            raise ValueError("A valid nonnegative/positive integer is required.")
        if prop == "space_group" and (value > 230 or relation != "equal"):
            raise ValueError("Space group must be an exact number from 1 to 230.")
        if prop == "max_samples" and relation not in {"equal", "at_most"}:
            raise ValueError("A sampling budget cannot be a minimum.")
        if prop == "valid_candidates_target" and relation not in {"equal", "at_least"}:
            raise ValueError("A valid-candidate target is separate from the sampling cap.")
        if prop == "batch_size" and relation not in {"equal", "at_most"}:
            raise ValueError("Batch size supports an exact size or an upper bound, not a minimum.")
        if prop == "seed" and relation != "equal":
            raise ValueError("A seed must have an exact value, not a bound or range.")
    elif kind in {"numeric_target", "numeric_bound"}:
        if prop not in UNITS or prop == "space_group":
            raise ValueError("Unknown quantitative property; an explicit property contract is needed.")
        if kind == "numeric_target" and relation != "target":
            raise ValueError("A target must not be substituted for a bound.")
        if kind == "numeric_bound" and relation not in {"at_most", "at_least", "range"}:
            raise ValueError("Use an explicit bound or range.")
        values = value if relation == "range" else [value]
        if not isinstance(values, list) or len(values) != (2 if relation == "range" else 1):
            raise ValueError("A range needs exactly two endpoints.")
        factor = 1 if unit == UNITS[prop] else CONVERSIONS.get((unit, UNITS[prop]))
        if factor is None:
            raise ValueError(f"Supply a supported unit for {prop}.")
        values = [_number(x) * factor for x in values]
        if min(values) < 0 or (len(values) == 2 and values[0] > values[1]):
            raise ValueError("Invalid target or bound endpoints.")
        value, unit = (values if relation == "range" else values[0]), UNITS[prop]
    elif kind == "template":
        if prop != "template_id" or not isinstance(value, str) or not value or relation != "equal" or unit is not None:
            raise ValueError("Template requirements use template_id, a registered identifier and equal.")
    elif kind == "research_goal":
        if not isinstance(prop, str) or not prop or not isinstance(value, str) or not value.strip():
            raise ValueError("A research goal needs an explicit name and text.")
        if relation != "goal" or unit is not None:
            raise ValueError("Qualitative research goals have no invented numerical unit or relation.")
    return {**deepcopy(record), "normalized_value": value, "normalized_unit": unit}


def _source_check(raw: str, record: dict):
    source = record["source"]
    if not isinstance(source, dict) or set(source) not in ({"quote"}, {"start", "end", "quote"}):
        raise ValueError("source must contain quote only, or start, end and quote.")
    quote = source["quote"]
    if not isinstance(quote, str) or not quote:
        raise ValueError("Source quote must be a nonempty exact substring.")
    if set(source) == {"quote"}:
        start = raw.find(quote)
        if start < 0:
            raise ValueError("Source quote is absent from the complete original request.")
        if raw.find(quote, start + 1) >= 0:
            raise ValueError("Source quote is repeated; use a longer unique quote or exact start/end offsets.")
        source = {"start": start, "end": start + len(quote), "quote": quote}
        record["source"] = source
        record["source_resolution"] = "unique_exact_quote"
    else:
        record["source_resolution"] = "explicit_offsets"
    start, end = source["start"], source["end"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw):
        raise ValueError("Source span is outside the complete original request.")
    if source["quote"] != raw[start:end]:
        raise ValueError("Source quote does not match the original request. Supply quote only if it occurs exactly once; do not guess offsets.")
    kind, value, quote = record["kind"], record["value"], source["quote"]
    if kind == "numeric_target" and re.search(BOUND_WORDS, quote, re.IGNORECASE):
        raise ValueError("The source contains a numerical bound; it cannot become a target.")
    if kind == "execution_parameter":
        for bound in BOUND_SIGNAL.finditer(quote):
            direction = "at_most" if re.match(UPPER_BOUND_WORDS, bound.group(), re.IGNORECASE) else "at_least"
            if record["property"] != "batch_size" or direction != "at_most" or record["relation"] != direction:
                raise ValueError("Preserve execution-parameter bound semantics: only batch_size at_most is supported; a bound cannot become equal or a seed value.")
    if kind in {"numeric_target", "numeric_bound"}:
        quoted_units = {m.group() for m in UNIT_TOKEN.finditer(quote)}
        if quoted_units and quoted_units != {record.get("unit")}:
            raise ValueError("Proposed units differ from the source; preserve the original value and unit.")
    if record["strength"] == "goal" and re.search(r"必须|严格|精确|不得|不允许|must\b|exact\b", quote, re.IGNORECASE):
        raise ValueError("An explicit hard requirement cannot be relabeled as a soft goal.")
    # These checks catch obvious fabricated values; they are not a semantic parser.
    if kind in {"symmetry", "sample_budget", "execution_parameter", "numeric_target", "numeric_bound"}:
        quoted = [float(m.group()) for m in re.finditer(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", quote)]
        values = value if isinstance(value, list) else [value]
        if any(float(v) not in quoted for v in values):
            raise ValueError("Numeric proposal is not present in its source span; retain original units and values.")
    if kind == "element_set" and any(e not in quote for e in value.split("-")):
        raise ValueError("Proposed elements are not present in the quoted span.")
    if kind == "template" and value not in quote:
        raise ValueError("Template identifier is not present in its source span.")
    if kind == "stoichiometry":
        formulae = [m.group() for m in FORMULA.finditer(quote)]
        if not any(_formula(f) == record["normalized_value"] for f in formulae):
            raise ValueError("Proposed stoichiometry is not present in the source span.")


def _signals(raw: str) -> list[dict]:
    signals, chemical_spans = [], []
    for pattern, kind in [(CHEMICAL_SET, "elements"), (FORMULA, "formula")]:
        for match in pattern.finditer(raw):
            value = match.group()
            if kind == "elements" and any(e not in ELEMENTS for e in value.split("-")):
                continue
            if kind == "formula":
                if not any(c.isdigit() for c in value):
                    continue
                try:
                    _formula(value)
                except ValueError:
                    continue
            signals.append({"start": match.start(), "end": match.end(), "text": value, "kind": kind})
            chemical_spans.append((match.start(), match.end()))
    for match in re.finditer(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", raw):
        if not any(a <= match.start() and match.end() <= b for a, b in chemical_spans):
            signals.append({"start": match.start(), "end": match.end(), "text": match.group(), "kind": "number"})
    for term in GOAL_TERMS:
        for match in re.finditer(re.escape(term), raw, re.IGNORECASE):
            signals.append({"start": match.start(), "end": match.end(), "text": match.group(), "kind": "research_goal"})
    for match in BOUND_SIGNAL.finditer(raw):
        signals.append({"start": match.start(), "end": match.end(), "text": match.group(), "kind": "bound"})
    return signals


def _structured_records(request: dict) -> list[dict]:
    """No original prose exists on the structured CLI path; never invent quotes."""
    records = []
    def add(kind, prop, value, relation="equal", strength="hard", unit=None):
        records.append({"requirement_id": f"s{len(records)+1}", "source": None, "origin": "structured_input",
                        "kind": kind, "property": prop, "value": value, "relation": relation, "strength": strength, "unit": unit})
    conditions = request.get("conditions", {})
    if isinstance(conditions, dict):
        for prop, value in conditions.items():
            if prop == "chemical_system":
                add("element_set", prop, value)
            elif prop == "chemical_formula":
                add("stoichiometry", prop, value)
            elif prop == "space_group":
                add("symmetry", prop, value.get("value") if isinstance(value, dict) else value)
            elif prop in UNITS:
                number = value.get("value") if isinstance(value, dict) else value
                unit = value.get("unit", UNITS[prop]) if isinstance(value, dict) else UNITS[prop]
                relation = value.get("relation", "target") if isinstance(value, dict) else "target"
                add("numeric_target" if relation == "target" else "numeric_bound", prop, number, relation, "goal" if relation == "target" else "hard", unit)
    if request.get("template_id") is not None:
        add("template", "template_id", request["template_id"])
    add("sample_budget", "max_samples", request.get("num_samples", 16), "at_most")
    if request.get("valid_candidates_target") is not None:
        add("sample_budget", "valid_candidates_target", request["valid_candidates_target"], "at_least")
    for value in request.get("review_targets", []) if isinstance(request.get("review_targets", []), list) else []:
        add("research_goal", "research_goal", value, "goal", "goal")
    return records


def audit_requirements(request: dict, plan: dict) -> dict:
    """Validate proposals and compute all allowed mappings without dropping failures."""
    raw = request.get("raw_request", "")
    raw = raw if isinstance(raw, str) else ""
    incoming = request.get("requirements")
    issues, normalized, mappings = [], [], []
    ids = set()
    if not raw and incoming is None:
        incoming = _structured_records(request)
        origin = "structured_input"
    else:
        origin = "anchored_proposal"
    if not isinstance(incoming, list) or not incoming or len(incoming) > 80:
        incoming = []
        issues.append({"code": "clarification_required", "detail": "requirements_missing", "message": "Provide 1-80 anchored requirements for the complete original request."})
    required = {"requirement_id", "source", "kind", "property", "value", "strength", "relation"}
    for item in incoming:
        try:
            if not isinstance(item, dict) or not required <= set(item) or set(item) - required - {"unit", "origin"}:
                raise ValueError("Requirement fields do not match the declared schema.")
            rid = item["requirement_id"]
            if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid) or rid in ids:
                raise ValueError("Requirement IDs must be unique nonempty identifiers.")
            ids.add(rid)
            if item["kind"] not in KINDS or item["strength"] not in {"hard", "goal"}:
                raise ValueError("Unknown requirement kind or strength.")
            record = _normalize(item)
            if origin == "anchored_proposal":
                _source_check(raw, record)
            record["origin"] = origin
            normalized.append(record)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append({"code": "invalid_requirement", "requirement_id": item.get("requirement_id") if isinstance(item, dict) else None, "message": str(exc)})
    covered_conditions, covered_reviews = set(), set()
    for r in normalized:
        rid, kind, prop, value = r["requirement_id"], r["kind"], r["property"], r["normalized_value"]
        routes = []
        def issue(message, code="requirement_mismatch"):
            issues.append({"code": code, "requirement_id": rid, "message": message})
        if kind in {"element_set", "symmetry", "numeric_target"}:
            if prop in plan["conditions"] and plan["conditions"][prop] == value:
                routes.append({"stage": "generation", "parameter": f"conditions.{prop}", "semantics": "guidance_not_guarantee"})
                covered_conditions.add(prop)
            elif kind != "numeric_target" and request.get("search_mode") == "generate_then_filter":
                routes.append({"stage": "search", "semantics": "explicit_generate_then_filter"})
            else:
                issue("Requirement has no matching generation parameter. Explicitly choose a supported condition or generate-then-filter search.")
            if kind in {"element_set", "symmetry"}:
                routes.append({"stage": "post_check", "checker": "element_equality" if kind == "element_set" else "space_group", "method": "pymatgen"})
            else:
                routes.append({"stage": "post_check", "checker": None, "status": "unavailable", "reason": "No property evaluator is configured."})
                if r["strength"] == "hard":
                    issue("A numerical generation target cannot establish a hard result guarantee.", "unverified_hard_requirement")
        elif kind == "stoichiometry":
            routes.append({"stage": "post_check", "checker": "reduced_stoichiometry", "method": "pymatgen.Composition"})
            if plan["model_id"] == "diffcsp" and plan.get("template") and plan["conditions"].get(prop) == value and plan["template"][prop] == value:
                routes.append({"stage": "generation", "parameter": "template.wyckoff_letters+atom_types", "semantics": "composition_from_verified_template; outputs_checked"})
                covered_conditions.add(prop)
            elif request.get("search_mode") != "generate_then_filter":
                issue("This model cannot fix stoichiometry. Explicit generate_then_filter search or a supported model/template is needed.", "clarification_required")
            else:
                routes.append({"stage": "search", "semantics": "explicit_generate_then_filter; success within budget not guaranteed"})
        elif kind == "numeric_bound":
            routes.append({"stage": "post_check", "checker": None, "status": "unavailable", "reason": "No method-verified property evaluator is configured."})
            issue("A numerical bound requires an applicable evaluator; it cannot be replaced by a generation target.", "unverified_hard_requirement")
        elif kind == "sample_budget":
            field = "num_samples" if prop == "max_samples" else "valid_candidates_target"
            actual = plan.get(field)
            matches = actual == value if prop != "max_samples" or r["relation"] == "equal" else isinstance(actual, int) and actual <= value
            if not matches:
                issue("Requested sampling cap/valid-candidate target does not match the plan; budgets must not be expanded.")
            routes.append({"stage": "execution" if prop == "max_samples" else "post_check", "parameter": field, "checker": "sampling_cap" if prop == "max_samples" else "valid_candidate_count"})
        elif kind == "template":
            if plan.get("template_id") != value or not plan.get("template"):
                issue("The registered template requirement is missing or invalid.")
            routes.append({"stage": "execution", "parameter": "template_id", "checker": "registered_CIF_hash_and_symmetry"})
        elif kind == "execution_parameter":
            actual = plan.get(prop)
            matches = (type(actual) is int and actual <= value) if prop == "batch_size" and r["relation"] == "at_most" else actual == value
            if not matches:
                issue("Execution parameter differs from its original requirement.")
            routes.append({"stage": "execution", "parameter": prop, "relation": r["relation"], "value": value})
        else:
            routes.append({"stage": "review", "status": "unavailable", "checker": None, "goal": value, "reason": "Research property has not been evaluated."})
            covered_reviews.add(value)
            if r["strength"] == "hard":
                issue("A hard scientific performance requirement has no verified evaluator.", "unverified_hard_requirement")
        mappings.append({"requirement_id": rid, "routes": routes})
    if plan.get("template_id") and not any(r["kind"] == "template" and r["normalized_value"] == plan["template_id"] for r in normalized):
        issues.append({"code": "unmapped_template", "message": "Template selection must be included in the reviewed requirements."})
    for field in set(plan["conditions"]) - covered_conditions:
        issues.append({"code": "unmapped_condition", "field": field, "message": "This generation condition has no matching requirement record."})
    for goal in plan.get("review_targets", []):
        if goal not in covered_reviews:
            issues.append({"code": "unmapped_research_goal", "message": "Each review target must have a requirement record with the same canonical text.", "goal": goal})
    # Research requirements are authoritative, even if the LLM omitted review_targets.
    review_targets = list(dict.fromkeys([*plan.get("review_targets", []), *[r["normalized_value"] for r in normalized if r["kind"] == "research_goal"]]))
    compatible_kinds = {"elements": {"element_set"}, "formula": {"stoichiometry"},
                        "number": {"symmetry", "sample_budget", "execution_parameter", "numeric_target", "numeric_bound"}, "research_goal": {"research_goal", "numeric_bound", "numeric_target"},
                        "bound": {"sample_budget", "numeric_bound", "execution_parameter"}}
    def covers_signal(record, signal):
        source = record["source"]
        if (record["kind"] not in compatible_kinds[signal["kind"]] or not source
                or not source["start"] <= signal["start"] or not signal["end"] <= source["end"]):
            return False
        if signal["kind"] != "bound":
            return True
        direction = "at_most" if re.match(UPPER_BOUND_WORDS, signal["text"], re.IGNORECASE) else "at_least"
        if record["kind"] == "execution_parameter":
            return record["property"] == "batch_size" and record["relation"] == direction == "at_most"
        return record["relation"] == direction or record["kind"] == "numeric_bound" and record["relation"] == "range"
    uncovered = [signal for signal in _signals(raw) if not any(covers_signal(record, signal) for record in normalized)]
    if uncovered:
        issues.append({"code": "clarification_required", "detail": "uncovered_source_signals", "message": "Some explicit source tokens have no corresponding requirement record; review them before execution."})
    return {"requirements": normalized, "requirement_mappings": mappings, "review_targets": review_targets, "issues": issues,
            "coverage": {"scope": origin, "proposed_count": len(incoming), "valid_count": len(normalized),
                         "mapped_count": len(mappings), "uncovered_signals": uncovered,
                         "program_mapping_coverage": len(mappings) / len(incoming) if incoming else None,
                         "semantic_completeness": "not_verified_requires_independent_review",
                         "limitations": "Lexical checks cover explicit Arabic numerals, element sets, formulae and a limited goal vocabulary. They do not prove absence of all semantic omissions."}}
