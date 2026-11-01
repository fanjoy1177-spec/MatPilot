"""Bounded formal-charge feasibility for a declared single-anion ionic scope.

Integer counts may use multiple oxidation states of the same element. The
returned witness is bookkeeping by element, never a site-resolved oxidation
state assignment, an energy estimate, or a prediction of material performance.
The historical Fe-Ni-O policy remains in relaxation_triage.py unchanged.
"""
from __future__ import annotations

from collections import Counter
import math

from .recommendations import _inspect, recommend_candidates


POLICY_ID = "ionic_mixed_valence_v1"
ANION_STATES = {"O": -2, "F": -1, "Cl": -1, "Br": -1, "I": -1,
                "S": -2, "Se": -2, "N": -3}
DEFAULT_LIMITS = {"max_atoms": 1000, "max_target_charge": 10000,
                  "max_transitions": 2_000_000, "max_memory_bytes": 4 * 1024 * 1024}
MAX_ELEMENT_COUNT = 118
LIMITATIONS = [
    "Formal integer charge balance is necessary only within this declared ionic model; it is not a prediction of energy, stability, synthesizability, relaxation success or catalytic performance.",
    "A witness gives oxidation-state counts per element, not assignments to crystallographic sites or a validated charge-ordering configuration.",
    "Exactly one registered anion is assumed, with a fixed negative state. Peroxides, superoxides, polyanions, mixed-anion compounds, metallic bonding and other oxidation states are outside this model.",
    "Positive cation states come from the installed pymatgen ICSD oxidation-state catalog, falling back to common states when no positive ICSD states are available; catalog completeness is not guaranteed.",
    "No solution means only no assignment within the declared states and atom counts. Unknown, unsupported and invalid inputs are distinguished and never treated as chemical impossibility.",
    "The full-occupancy and basic geometry checks are task constraints, not universal material validity criteria. No element-specific density or volume thresholds are imposed by this policy.",
    "Candidate ordering after the feasibility gate maximizes fixed categorical coverage; it does not rank physical performance or guarantee a globally optimal shortlist.",
]


def policy_description() -> dict:
    return {
        "id": POLICY_ID,
        "scope": "single_registered_anion_with_positive_state_metal_cations",
        "anion_states": dict(ANION_STATES),
        "cation_states": "positive integer Element.icsd_oxidation_states; if empty/unavailable, positive integer Element.common_oxidation_states",
        "cation_state_source": "pymatgen.core.periodic_table.Element",
        "cation_state_source_documentation": "https://pymatgen.org/pymatgen.core.html#pymatgen.core.periodic_table.Element.icsd_oxidation_states",
        "uniform_ablation": "uniform_possible uses exactly the same state catalog as mixed_possible; only per-element uniformity differs",
        "solver": "Exact integer bitset dynamic programming; bounded resources; deterministic backtracking yields per-element state counts",
        "complexity": "For U cation atoms and target charge Q, mixed solving uses U bitset layers of at most Q+1 bits; shifts scale with permitted states and bitset word length. Conservative reachable-state transition counting is also capped.",
        "resource_limits": {**DEFAULT_LIMITS, "max_element_entries": MAX_ELEMENT_COUNT,
                            "caller_may_only_reduce_limits": True},
        "priority_rule": "structural representative passes and formal mixed-allowed integer charge balance is feasible",
        "manual_review_rule": "formal model has no solution, or analysis is unknown/unsupported/invalid; no property rejection is inferred",
        "defer_rule": "none; obvious geometry/identity/task violations remain in the existing structural precheck",
        "structural_gate": "Existing structural_v1 CIF SHA256, exact declared elements, explicit full occupancy, periodic contact >= 0.5 angstrom, structure equivalence and symmetry checks",
        "performance_labels_used": False,
        "limitations": list(LIMITATIONS),
    }


def _positive_integer_states(values) -> tuple[int, ...]:
    try:
        return tuple(sorted({int(value) for value in values
                             if not isinstance(value, bool)
                             and isinstance(value, (int, float))
                             and math.isfinite(value) and value > 0
                             and float(value).is_integer()}))
    except (TypeError, ValueError, OverflowError):
        return ()


def describe_ionic_system(expected_elements) -> dict:
    """Explain support without reading a structure or any scientific label."""
    if (not isinstance(expected_elements, (list, tuple, set, frozenset))
            or not expected_elements):
        return {"supported": False, "status": "invalid_input", "reason": "invalid_expected_elements"}
    if len(expected_elements) > MAX_ELEMENT_COUNT:
        return {"supported": False, "status": "unknown", "reason": "element_entry_budget_exceeded"}
    if any(not isinstance(x, str) or not 1 <= len(x) <= 2 for x in expected_elements):
        return {"supported": False, "status": "invalid_input", "reason": "invalid_element_symbol"}
    from pymatgen.core import Element
    try:
        elements = {Element(value).symbol: Element(value) for value in expected_elements}
    except (ValueError, TypeError, KeyError):
        return {"supported": False, "status": "invalid_input", "reason": "invalid_element_symbol"}
    anions = sorted(set(elements) & set(ANION_STATES))
    if len(anions) != 1:
        return {"supported": False, "status": "unsupported", "reason": "requires_exactly_one_registered_anion",
                "registered_anions_present": anions}
    anion = anions[0]
    cations = sorted(set(elements) - {anion})
    if not cations:
        return {"supported": False, "status": "unsupported", "reason": "requires_at_least_one_metal_cation"}
    catalog = {}
    for symbol in cations:
        element = elements[symbol]
        if not element.is_metal:
            return {"supported": False, "status": "unsupported", "reason": "nonmetal_cation_outside_declared_scope",
                    "element": symbol}
        try:
            states = _positive_integer_states(element.icsd_oxidation_states)
        except (AttributeError, KeyError, TypeError, ValueError):
            states = ()
        source = "pymatgen.Element.icsd_oxidation_states"
        if not states:
            try:
                states = _positive_integer_states(element.common_oxidation_states)
            except (AttributeError, KeyError, TypeError, ValueError):
                states = ()
            source = "pymatgen.Element.common_oxidation_states_fallback"
        if not states:
            return {"supported": False, "status": "unsupported", "reason": "no_positive_cation_states_in_declared_catalogs",
                    "element": symbol}
        catalog[symbol] = {"states": list(states), "source": source}
    return {"supported": True, "status": "supported", "elements": sorted(elements),
            "anion": {"element": anion, "oxidation_state": ANION_STATES[anion]},
            "cation_catalog": catalog}


def supports_elements(expected_elements) -> bool:
    return describe_ionic_system(expected_elements)["supported"]


def supports_ionic_system(expected_elements) -> bool:
    """Public service-routing alias, with no implicit chemistry generalization."""
    return supports_elements(expected_elements)


class _BudgetExceeded(Exception):
    pass


def _integer_dp(units, target: int, limits: dict, budget: dict) -> dict:
    """A unit contains element, atom multiplicity and allowed per-atom states."""
    minimum = sum(count * min(states) for _, count, states in units)
    maximum = sum(count * max(states) for _, count, states in units)
    if not minimum <= target <= maximum:
        return {"possible": False, "witness": None, "proof": "target_outside_exact_charge_bounds",
                "minimum_charge": minimum, "maximum_charge": maximum}
    bits = 1
    layers = [bits]
    memory_bytes = 1
    mask = (1 << (target + 1)) - 1
    for element, count, states in units:
        estimated = bits.bit_count() * len(states)
        if budget["transitions"] + estimated > limits["max_transitions"]:
            raise _BudgetExceeded("transition_budget_exhausted")
        budget["transitions"] += estimated
        next_bits = 0
        for state in states:
            next_bits |= bits << (count * state)
        bits = next_bits & mask
        memory_bytes += max(1, (bits.bit_length() + 7) // 8)
        budget["peak_saved_bitset_bytes"] = max(budget["peak_saved_bitset_bytes"], memory_bytes)
        if memory_bytes > limits["max_memory_bytes"]:
            raise _BudgetExceeded("saved_bitset_memory_budget_exhausted")
        layers.append(bits)
        if not bits:
            return {"possible": False, "witness": None, "proof": "exhaustive_integer_dp"}
    if not (bits >> target) & 1:
        return {"possible": False, "witness": None, "proof": "exhaustive_integer_dp"}
    remaining, witness = target, {}
    for index in range(len(units) - 1, -1, -1):
        element, count, states = units[index]
        for state in states:
            previous = remaining - count * state
            if previous >= 0 and (layers[index] >> previous) & 1:
                state_counts = witness.setdefault(element, {})
                state_counts[str(state)] = state_counts.get(str(state), 0) + count
                remaining = previous
                break
        else:
            raise AssertionError("Integer DP backtracking failed")
    if remaining:
        raise AssertionError("Integer DP witness is not charge-conserving")
    return {"possible": True, "witness": witness, "proof": "exact_integer_witness"}


def formal_charge_balance(composition: dict, *, max_atoms: int = DEFAULT_LIMITS["max_atoms"],
                          max_target_charge: int = DEFAULT_LIMITS["max_target_charge"],
                          max_transitions: int = DEFAULT_LIMITS["max_transitions"],
                          max_memory_bytes: int = DEFAULT_LIMITS["max_memory_bytes"]) -> dict:
    """Find integer oxidation-state counts, or state exactly why no answer exists.

mixed_possible means that mixing is allowed; it is also true for a uniform
solution. requires_mixed_valence is true only after exhaustive uniform failure
and a mixed witness. Resource failure never becomes a no-solution verdict.
"""
    limits = {"max_atoms": max_atoms, "max_target_charge": max_target_charge,
              "max_transitions": max_transitions, "max_memory_bytes": max_memory_bytes}
    result = {"model_id": POLICY_ID, "status": "unknown", "uniform_possible": None,
              "mixed_possible": None, "requires_mixed_valence": None,
              "uniform_witness": None, "witness": None, "reason": None,
              "resource_limits": limits, "limitations": list(LIMITATIONS)}
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in limits.values()):
        return {**result, "status": "invalid_input", "reason": "resource_limits_must_be_nonnegative_integers"}
    if any(value > DEFAULT_LIMITS[name] for name, value in limits.items()):
        return {**result, "status": "invalid_input", "reason": "caller_cannot_increase_hard_resource_limits"}
    if not isinstance(composition, dict) or not composition:
        return {**result, "status": "invalid_input", "reason": "composition_must_be_a_nonempty_element_count_mapping"}
    if len(composition) > MAX_ELEMENT_COUNT:
        return {**result, "status": "unknown", "reason": "element_entry_budget_exceeded"}
    from pymatgen.core import Element
    counts, total_atoms = {}, 0
    for symbol, value in composition.items():
        if not isinstance(symbol, str) or not 1 <= len(symbol) <= 2:
            return {**result, "status": "invalid_input", "reason": "invalid_element_symbol"}
        try:
            Element(symbol)
        except (ValueError, TypeError, KeyError):
            return {**result, "status": "invalid_input", "reason": "invalid_element_symbol"}
        if (isinstance(value, bool)
                or not isinstance(value, (int, float))):
            return {**result, "status": "invalid_input", "reason": "atom_counts_must_be_positive_integers"}
        if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
            return {**result, "status": "invalid_input", "reason": "atom_counts_must_be_positive_integers"}
        if value <= 0:
            return {**result, "status": "invalid_input", "reason": "atom_counts_must_be_positive_integers"}
        counts[symbol] = int(value)
        total_atoms += counts[symbol]
        if total_atoms > max_atoms:
            return {**result, "status": "unknown", "reason": "atom_budget_exceeded"}
    support = describe_ionic_system(list(counts))
    result.update(composition=dict(sorted(counts.items())), system=support)
    if not support["supported"]:
        return {**result, "status": support["status"], "reason": support["reason"]}
    anion = support["anion"]["element"]
    anion_state = support["anion"]["oxidation_state"]
    target = -counts[anion] * anion_state
    result.update(total_atoms=total_atoms, target_cation_charge=target)
    if target > max_target_charge:
        return {**result, "status": "unknown", "reason": "target_charge_budget_exceeded"}
    catalog = support["cation_catalog"]
    uniform_units = [(symbol, counts[symbol], tuple(catalog[symbol]["states"])) for symbol in sorted(catalog)]
    budget = {"transitions": 0, "peak_saved_bitset_bytes": 0}
    result["resources_used"] = budget

    def finish_witness(cation_witness):
        witness = {symbol: dict(sorted(states.items(), key=lambda item: int(item[0])))
                   for symbol, states in sorted(cation_witness.items())}
        witness[anion] = {str(anion_state): counts[anion]}
        if set(witness) != set(counts) or any(sum(witness[symbol].values()) != count for symbol, count in counts.items()):
            raise AssertionError("Witness does not conserve atom counts")
        if sum(int(state) * count for states in witness.values() for state, count in states.items()) != 0:
            raise AssertionError("Witness does not conserve formal charge")
        return witness

    try:
        uniform = _integer_dp(uniform_units, target, limits, budget)
        result["uniform_possible"] = uniform["possible"]
        if uniform["possible"]:
            witness = finish_witness(uniform["witness"])
            return {**result, "status": "feasible", "mixed_possible": True,
                    "requires_mixed_valence": False, "uniform_witness": witness,
                    "witness": witness, "reason": "uniform_integer_assignment_found"}
        mixed_units = [(symbol, 1, states) for symbol, count, states in uniform_units for _ in range(count)]
        mixed = _integer_dp(mixed_units, target, limits, budget)
        result["mixed_possible"] = mixed["possible"]
        if mixed["possible"]:
            return {**result, "status": "feasible", "requires_mixed_valence": True,
                    "witness": finish_witness(mixed["witness"]), "reason": "mixed_integer_assignment_found"}
        return {**result, "status": "no_solution", "requires_mixed_valence": None,
                "reason": "no_assignment_within_declared_states_and_atom_counts",
                "no_solution_proof": mixed["proof"]}
    except _BudgetExceeded as exc:
        return {**result, "status": "unknown", "mixed_possible": None, "reason": str(exc)}


def _review_result(charge: dict, metrics: dict | None = None) -> dict:
    feasible = charge["status"] == "feasible" and charge["mixed_possible"] is True
    details = dict(metrics or {})
    details["charge_feasibility"] = charge
    return {"policy_id": POLICY_ID,
            "tier": "priority_relaxation" if feasible else "manual_review",
            "priority": "优先弛豫" if feasible else "人工复核",
            "score": None, "eligible_for_relaxation_shortlist": feasible,
            "issues": [] if feasible else ["formal_charge_" + charge["status"] + ":" + str(charge.get("reason"))],
            "metrics": details, "limitations": list(LIMITATIONS)}


def analyze_mixed_valence_candidate(record: dict, expected_elements, *,
                                   symprec: float = 0.1, angle_tolerance: float = 5.0) -> dict:
    support = describe_ionic_system(expected_elements)
    if not support["supported"]:
        return _review_result({"model_id": POLICY_ID, "status": support["status"],
                               "reason": support["reason"], "system": support,
                               "uniform_possible": None, "mixed_possible": None,
                               "witness": None, "limitations": list(LIMITATIONS)})
    row, crystal = _inspect(record, set(expected_elements), symprec, angle_tolerance)
    if crystal is None:
        return {"policy_id": POLICY_ID, "tier": "not_eligible", "priority": "未通过结构预检",
                "score": None, "eligible_for_relaxation_shortlist": False,
                "issues": list(row["exclusion_reasons"]), "metrics": {}, "limitations": list(LIMITATIONS)}
    try:
        composition = dict(Counter(site.specie.symbol for site in crystal))
        charge = formal_charge_balance(composition)
        return _review_result(charge, {"composition": composition,
                                      "input_site_count": len(crystal),
                                      "cell_volume_angstrom3": float(crystal.volume),
                                      "volume_per_atom_angstrom3": float(crystal.volume) / len(crystal),
                                      "density_g_cm3": float(crystal.density),
                                      "density_and_volume_are_descriptive_only": True})
    except Exception as exc:
        return _review_result({"model_id": POLICY_ID, "status": "unknown", "reason": "analysis_unavailable",
                               "analysis_error": f"{type(exc).__name__}: {exc}",
                               "uniform_possible": None, "mixed_possible": None,
                               "witness": None, "limitations": list(LIMITATIONS)})


def recommend_with_mixed_valence(records: list[dict], expected_elements, limit: int = 12, *,
                                 priority_candidate_ids: list[str] | None = None,
                                 priority_selection_basis: str | None = None,
                                 symprec: float = 0.1, angle_tolerance: float = 5.0) -> dict:
    """Use the existing structural gate, formal feasibility, then coverage selection."""
    support = describe_ionic_system(expected_elements)
    if not support["supported"]:
        raise ValueError(f"{POLICY_ID} does not support this declared element system: {support['reason']}; use structural_v1.")
    expected = list(expected_elements)
    audit = recommend_candidates(records, expected, limit,
        priority_candidate_ids=priority_candidate_ids, priority_selection_basis=priority_selection_basis,
        symprec=symprec, angle_tolerance=angle_tolerance)
    by_id = {record["candidate_id"]: record for record in records}
    triage, priority_ids = {}, []
    for row in audit["records"]:
        if row["qualified"]:
            item = analyze_mixed_valence_candidate(by_id[row["candidate_id"]], expected,
                                                   symprec=symprec, angle_tolerance=angle_tolerance)
        else:
            item = {"policy_id": POLICY_ID, "tier": "not_eligible", "priority": "未通过代表结构资格检查",
                    "score": None, "eligible_for_relaxation_shortlist": False,
                    "issues": list(row["exclusion_reasons"]), "metrics": {}, "limitations": list(LIMITATIONS)}
        triage[row["candidate_id"]] = item
        if item["eligible_for_relaxation_shortlist"]:
            priority_ids.append(row["candidate_id"])
    result = recommend_candidates(records, expected, limit, priority_candidate_ids=priority_ids,
        priority_selection_basis=f"{POLICY_ID}: formal ionic charge feasibility; no physical-property labels used",
        symprec=symprec, angle_tolerance=angle_tolerance)
    original_rows = {row["candidate_id"]: row for row in audit["records"]}
    for row in result["records"]:
        original = original_rows[row["candidate_id"]]
        row["structural_representative_qualified"] = original["qualified"]
        row["structural_exclusion_reasons"] = list(original["exclusion_reasons"])
        row["relaxation_priority"] = triage[row["candidate_id"]]
        if original.get("representative_id") is not None:
            row["representative_id"] = original["representative_id"]
        if not original["qualified"]:
            # The internal priority-only pass is an implementation detail. Do
            # not invent an extra caller exclusion for a bad SHA, duplicate or
            # other candidate that already failed the original structural pass.
            row["exclusion_reasons"] = list(original["exclusion_reasons"])
            row["not_selected_reason"] = "; ".join(row["exclusion_reasons"])
        elif not triage[row["candidate_id"]]["eligible_for_relaxation_shortlist"]:
            row["exclusion_reasons"] = [reason for reason in row["exclusion_reasons"] if reason != "outside_explicit_priority_selector"]
            row["exclusion_reasons"].append("formal_charge_requires_review")
            row["not_selected_reason"] = "formal_charge_requires_review"
    tiers = Counter(item["tier"] for item in triage.values())
    result.update(method="verified_cif_ionic_mixed_valence_then_category_coverage_v1",
        relaxation_triage_policy=policy_description(), ionic_system_support=support,
        caller_priority_selector=audit["priority_selector"],
        stage_counts={"input_count": len(records),
                      "structural_representative_count": audit["eligible_representative_count"],
                      "priority_relaxation_count": tiers["priority_relaxation"],
                      "manual_review_count": tiers["manual_review"], "defer_count": tiers["defer"],
                      "not_eligible_count": tiers["not_eligible"], "selected_count": result["selected_count"],
                      "requested_count": limit, "shortfall_count": max(0, limit - result["selected_count"])})
    result["limitations"] += list(LIMITATIONS)
    return result
