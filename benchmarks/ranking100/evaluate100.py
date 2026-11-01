"""Frozen 100/50/20 evaluation of signed reference-hull ranking.

API: evaluate100(truth, rankings, primary_method='plugin_wrenformer',
                truth_tie_keys=None, eligible_ids=None, prediction_values=None).
truth is {candidate_id: signed_real_value}; rankings maps method to an ordered
unique ID list (0..100 entries). Each method's first 20 entries are selected.
Missing recommendations incur a fixed-20 denominator, never favorable fillers.
truth_tie_keys maps every anonymous ID to its immutable source/material ID.
This module reads truth only at evaluation time and does not run any predictor.
"""
from fractions import Fraction
import hashlib
import json
import math

N, K, TOP = 100, 50, 20
TRUTH_TIE_SEED = "MatPilot-trend-100-truth-tie-v1:"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Truth and available prediction values must be finite numbers")
    try:
        value = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Numeric value is not finite/representable") from exc
    if not math.isfinite(value):
        raise ValueError("Truth and available prediction values must be finite numbers")
    return value


def _id_list(values, known=None):
    if not isinstance(values, list) or any(not isinstance(x, str) or not x.strip() for x in values):
        raise ValueError("IDs must be a list of nonempty strings")
    if len(values) != len(set(values)):
        raise ValueError("Candidate IDs must be unique")
    if known is not None and not set(values) <= known:
        raise ValueError("Candidate IDs fall outside the frozen 100-candidate universe")
    return values


def _hypergeom(population, positive, draws, observed):
    if not 0 <= positive <= population or not 0 <= draws <= population:
        raise ValueError("Invalid finite-pool random comparison")
    denominator = math.comb(population, draws)
    weights = [math.comb(positive, hits) * math.comb(population - positive, draws - hits)
               if hits <= positive and draws - hits <= population - positive else 0
               for hits in range(draws + 1)]
    assert sum(weights) == denominator
    def quantile(q):
        total = 0
        for value, weight in enumerate(weights):
            total += weight
            if Fraction(total, denominator) >= q:
                return value
        return draws
    tail = Fraction(sum(weights[observed:]), denominator) if observed <= draws else Fraction(0)
    expectation = draws * positive / population if population else 0.0
    return {"population": population, "positive": positive, "draws": draws,
            "expected_hits": expectation, "expected_precision_fixed20": expectation / TOP,
            "precision_denominator": TOP,
            "selection_95pct_interval_hits": [quantile(Fraction(1, 40)), quantile(Fraction(39, 40))],
            "upper_tail_ge_observed": float(tail),
            "exact_upper_tail_fraction": {"numerator": str(tail.numerator), "denominator": str(tail.denominator)},
            "interpretation": "Conditional fixed-pool random selection without replacement; not a population accuracy confidence interval"}


def _average_ranks(values):
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.] * len(values)
    start = 0
    while start < len(order):
        stop = start + 1
        while stop < len(order) and values[order[stop]] == values[order[start]]:
            stop += 1
        for index in order[start:stop]:
            result[index] = (start + 1 + stop) / 2
        start = stop
    return result


def _spearman(truth, predictions, ids):
    ids = sorted(ids)
    if len(ids) < 2:
        return {"n": len(ids), "rho": None, "reason": "fewer_than_two_pairs"}
    x = _average_ranks([truth[i] for i in ids])
    y = _average_ranks([predictions[i] for i in ids])
    center = (len(ids) + 1) / 2
    xx = sum((v - center) ** 2 for v in x)
    yy = sum((v - center) ** 2 for v in y)
    if not xx or not yy:
        return {"n": len(ids), "rho": None, "reason": "constant_truth_or_prediction"}
    return {"n": len(ids), "rho": sum((a - center) * (b - center) for a, b in zip(x, y)) / math.sqrt(xx * yy),
            "tie_handling": "average numeric ranks; no hash tie-breaking for Spearman"}


def evaluate100(truth, rankings, primary_method="plugin_wrenformer", *, truth_tie_keys=None,
                eligible_ids=None, prediction_values=None, truth_tie_seed=TRUTH_TIE_SEED):
    if not isinstance(truth, dict) or len(truth) != N:
        raise ValueError("Exactly 100 ground-truth candidates are required; missing labels cannot be dropped")
    ids = _id_list(list(truth))
    known = set(ids)
    values = {i: _number(value) for i, value in truth.items()}
    if not isinstance(rankings, dict) or primary_method not in rankings:
        raise ValueError("The predeclared primary_method must exist in rankings")
    if any(not isinstance(method, str) or not method for method in rankings):
        raise ValueError("Method names must be nonempty strings")
    if truth_tie_keys is None:
        truth_tie_keys = {i: i for i in ids}
    if not isinstance(truth_tie_keys, dict) or set(truth_tie_keys) != known:
        raise ValueError("truth_tie_keys must map every one of the 100 candidates")
    _id_list(list(truth_tie_keys.values()))
    if not isinstance(truth_tie_seed, str) or not truth_tie_seed:
        raise ValueError("The frozen truth-tie seed must be nonempty")
    tokens = {i: hashlib.sha256((truth_tie_seed + truth_tie_keys[i]).encode()).hexdigest() for i in ids}
    ordered = sorted(ids, key=lambda i: (values[i], tokens[i], truth_tie_keys[i]))
    true50 = set(ordered[:K])
    true20 = set(ordered[:20])
    ranks = {i: rank + 1 for rank, i in enumerate(ordered)}
    cutoff = values[ordered[K - 1]]
    better = {i for i in ids if values[i] < cutoff}
    tied = {i for i in ids if values[i] == cutoff}
    inclusive = better | tied
    tie_slots = K - len(better)
    if eligible_ids is None:
        eligible_ids = {}
    if not isinstance(eligible_ids, dict) or not set(eligible_ids) <= set(rankings):
        raise ValueError("eligible_ids must map existing method names to lists of IDs")
    if prediction_values is None:
        prediction_values = {}
    if not isinstance(prediction_values, dict) or not set(prediction_values) <= set(rankings):
        raise ValueError("prediction_values must map existing method names to numeric evidence maps")
    methods = {}
    for method, ranking in rankings.items():
        ranking = _id_list(ranking, known)
        selected = ranking[:TOP]
        hit_ids = [i for i in selected if i in true50]
        hits, returned = len(hit_ids), len(selected)
        selected_set = set(selected)
        strict_hits = len(selected_set & better)
        tied_hits = len(selected_set & tied)
        tie_min = strict_hits + max(0, tie_slots - (len(tied) - tied_hits))
        tie_max = strict_hits + min(tie_slots, tied_hits)
        tie_expect = strict_hits + tied_hits * tie_slots / len(tied)
        inclusive_hits = len(selected_set & inclusive)
        random = _hypergeom(N, K, TOP, hits)
        record = {
            "returned_count": returned, "shortfall": TOP - returned, "budget_denominator": TOP,
            "hit_count": hits, "precision_at_20": hits / TOP, "precision_at_20_percent": 100 * hits / TOP,
            "precision_at_returned": hits / returned if returned else None,
            "recall_at_20": hits / K, "lift_over_random": (hits / TOP) / .5,
            "selected_candidate_ids": list(selected), "hit_candidate_ids": hit_ids,
            "miss_candidate_ids": [i for i in selected if i not in true50],
            "selected_truth_ranks": [ranks[i] for i in selected],
            "random_all100": random,
            "secondary": {
                "precision_at_10_against_true_top50": len(set(ranking[:10]) & true50) / 10,
                "hits_at_10_against_true_top50": len(set(ranking[:10]) & true50),
                "precision_at_20_against_true_top20": len(selected_set & true20) / TOP,
                "hits_at_20_against_true_top20": len(selected_set & true20),
                "interpretation": "Exploratory sensitivity only; primary remains Top20 against true Top50",
            },
            "boundary_tie_sensitivity": {
                "selected_strictly_better": strict_hits, "selected_tied_at_cutoff": tied_hits,
                "possible_hit_range": [tie_min, tie_max], "possible_precision_range": [tie_min / TOP, tie_max / TOP],
                "fractional_expected_hits": tie_expect, "fractional_expected_precision": tie_expect / TOP,
                "inclusive_cutoff_hits": inclusive_hits, "inclusive_cutoff_precision": inclusive_hits / TOP,
                "inclusive_cutoff_random_precision": len(inclusive) / N,
                "inclusive_cutoff_lift": (inclusive_hits / TOP) / (len(inclusive) / N),
            },
        }
        if method in eligible_ids:
            eligible = set(_id_list(eligible_ids[method], known))
            if not selected_set <= eligible:
                raise ValueError("Selected candidates must be contained in their declared Service-eligible pool")
            within_random = _hypergeom(len(eligible), len(eligible & true50), min(TOP, len(eligible)), hits)
            record["service_eligible_pool"] = {
                "count": len(eligible), "true_top50_count": len(eligible & true50),
                "candidate_ids": sorted(eligible), "random_comparator": within_random,
                "interpretation": "Diagnostic gate-versus-order decomposition; does not replace the primary all100 random baseline or remove excluded candidates from evaluation",
            }
        if method in prediction_values:
            predictions = prediction_values[method]
            if not isinstance(predictions, dict) or not set(predictions) <= known:
                raise ValueError("Prediction values must be keyed by IDs from the frozen 100")
            predictions = {i: _number(value) for i, value in predictions.items()}
            available = set(predictions)
            spearman = {"available_count": len(available), "missing_count": N - len(available),
                        "all100": _spearman(values, predictions, known) if available == known else
                        {"n": len(available), "rho": None, "reason": "not_all100_predictions_available"},
                        "available_subset": _spearman(values, predictions, available)}
            if method in eligible_ids:
                spearman["service_survivors"] = _spearman(values, predictions, available & set(eligible_ids[method]))
            record["spearman"] = spearman
        methods[method] = record
    primary = methods[primary_method]
    canonical_truth = [{"candidate_id": i, "source_id": truth_tie_keys[i], "value": values[i]} for i in sorted(ids)]
    return {
        "schema_version": "matpilot-trend100-evaluation/v1", "population_size": N, "true_top_count": K, "budget": TOP,
        "ground_truth": {"direction": "minimize", "interpretation": "signed distance to the fixed independent reference hull; negative values are permitted",
                         "cutoff_value": cutoff, "strictly_better_count": len(better), "boundary_tie_count": len(tied),
                         "tie_slots_in_top50": tie_slots, "tie_crosses_cutoff": len(tied) > tie_slots,
                         "inclusive_cutoff_positive_count": len(inclusive), "distinct_value_count": len(set(values.values())),
                         "truth_tie_seed": truth_tie_seed, "truth_tie_identity": "immutable source ID supplied separately from anonymous candidate ID",
                         "true_top50_ids": ordered[:K], "true_top20_ids": ordered[:20]},
        "truth_order": [{"rank": ranks[i], "candidate_id": i, "source_id": truth_tie_keys[i], "value": values[i], "tie_token": tokens[i]} for i in ordered],
        "primary_method": primary_method, "primary": primary, "methods": methods,
        "predeclared_screening_trend_criterion": {
            "hits_above_random10": primary["hit_count"] > 10,
            "exact_random_upper_tail_lt_05": primary["random_all100"]["upper_tail_ge_observed"] < .05,
            "passed": primary["hit_count"] > 10 and primary["random_all100"]["upper_tail_ge_observed"] < .05,
            "no_automatic_default_switch": True,
        },
        "truth_semantic_sha256": hashlib.sha256(json.dumps(canonical_truth, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        "limitations": [
            "Precision@20 is shortlist hit rate, not classification accuracy over 100 candidates.",
            "Shortfalls retain the fixed denominator of 20; no returned candidate is replaced after seeing truth.",
            "Random probabilities and intervals are conditional on this fixed pool, not a population generalization confidence interval; no IID bootstrap is used.",
            "This evaluator checks IDs and metric arithmetic; label isolation and prediction provenance require a separate frozen-input execution audit.",
            "Using the same true target as the ranker's input gives a sorting implementation check, not independent forecast validation.",
            "Boundary ties are physically equal on the chosen target; hash-resolved exact Top50 and inclusive/fractional sensitivity have different interpretations.",
        ],
    }


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON object whose keys match evaluate100 arguments")
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    if arguments.output.exists():
        raise SystemExit("Refuse to overwrite an evaluation output")
    report = evaluate100(**json.loads(arguments.input.read_text(encoding="utf-8")))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report["primary"][key] for key in ("hit_count", "precision_at_20", "precision_at_returned", "shortfall", "recall_at_20", "lift_over_random")}))
