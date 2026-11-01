"""Frozen ranker contracts; optional old-development parity is not external validation."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from matpilot.mixed_valence import formal_charge_balance
from matpilot.priority_ranking import (
    FEATURE_NAMES, POLICY_ID, extract_features, policy_description,
    recommend_ranked, rerank_priority_pool,
)
from matpilot import priority_ranking_model


def fixture_row(name, volume=12., composition=None, density=5., space_group=225):
    composition = composition or {"Fe": 3, "O": 4}
    charge = formal_charge_balance(composition)
    assert charge["status"] == "feasible"
    count = sum(composition.values())
    return {"candidate_id": name, "sha256": hashlib.sha256(name.encode()).hexdigest(),
            "qualified": True, "selected": False, "site_count": count,
            "reduced_formula": "".join(f"{key}{value}" for key, value in sorted(composition.items())),
            "space_group_number": space_group, "size_bin": "1-10", "exclusion_reasons": [],
            "relaxation_priority": {"tier": "priority_relaxation", "eligible_for_relaxation_shortlist": True,
                "metrics": {"charge_feasibility": charge, "composition": dict(composition),
                    "input_site_count": count, "cell_volume_angstrom3": count * volume,
                    "volume_per_atom_angstrom3": volume, "density_g_cm3": density}}}


def fixture_result(rows=None, limit=2):
    rows = copy.deepcopy(rows or [fixture_row("A", 14.), fixture_row("B", 12.), fixture_row("C", 10.)])
    selected = rows[:limit]
    for index, row in enumerate(selected):
        row.update(selected=True, selection_rank=index + 1, selection_reason={"legacy": True})
    return {"records": rows, "selected_candidate_ids": [row["candidate_id"] for row in selected],
            "selected_count": len(selected), "requested_count": limit, "target_met": len(rows) >= limit,
            "method": "legacy", "limitations": ["legacy limitation"],
            "coverage": {key: {"selected_values": sorted({row[key] for row in selected}),
                               "selected_count": len({row[key] for row in selected}),
                               "pool_count": len({row[key] for row in rows})}
                         for key in ("reduced_formula", "space_group_number", "size_bin")},
            "stage_counts": {"priority_relaxation_count": len(rows), "manual_review_count": 0,
                             "selected_count": len(selected), "shortfall_count": max(0, limit - len(rows))}}


class PriorityRankingTests(unittest.TestCase):
    def test_frozen_model_is_bundled_exactly_and_training_is_disclosed(self):
        model = priority_ranking_model.load_model()
        self.assertEqual(priority_ranking_model.MODEL_SHA256,
                         "f681dabf96c19d63e7900e3bf5f01502ee0384c54ecc551f30fee2172caa5009")
        self.assertEqual(model["features"], list(FEATURE_NAMES))
        card = policy_description()
        self.assertEqual(card["id"], POLICY_ID)
        self.assertTrue(card["performance_labels_used_for_training"])
        self.assertFalse(card["runtime_property_labels_used"])
        self.assertEqual(card["training"]["records"], 335)
        self.assertIn("not_probability", card["score_interpretation"])

    def test_only_priority_pool_is_reranked_and_input_is_not_mutated(self):
        base = fixture_result()
        excluded = {"candidate_id": "review", "qualified": False, "selected": False,
                    "exclusion_reasons": ["formal_charge_requires_review"],
                    "relaxation_priority": {"tier": "manual_review", "eligible_for_relaxation_shortlist": False}}
        base["records"].append(excluded)
        before = copy.deepcopy(base)
        result = rerank_priority_pool(base)
        self.assertEqual(base, before)
        self.assertEqual(result["priority_ranking"]["status"], "applied")
        self.assertEqual(result["selected_count"], 2)
        self.assertEqual(result["selected_candidate_ids"], ["C", "B"])
        self.assertEqual(result["stage_counts"]["priority_relaxation_count"], 3)
        self.assertEqual(result["records"][-1]["ranking_evidence"]["status"], "outside_priority_pool")
        for row, original in zip(result["records"], before["records"]):
            for key in ("qualified", "exclusion_reasons", "relaxation_priority"):
                self.assertEqual(row[key], original[key])
        chosen = {row["candidate_id"] for row in result["records"] if row["selected"]}
        self.assertEqual(chosen, set(result["selected_candidate_ids"]))

    def test_features_and_score_are_invariant_to_integer_supercell_counts(self):
        row = fixture_row("original")
        expanded = copy.deepcopy(row)
        charge = expanded["relaxation_priority"]["metrics"]["charge_feasibility"]
        charge["composition"] = {key: value * 4 for key, value in charge["composition"].items()}
        charge["target_cation_charge"] *= 4
        expanded["site_count"] *= 4
        metrics = expanded["relaxation_priority"]["metrics"]
        metrics["input_site_count"] *= 4
        metrics["cell_volume_angstrom3"] *= 4
        self.assertEqual(extract_features(row), extract_features(expanded))
        out = rerank_priority_pool(fixture_result([row, {**expanded, "candidate_id": "expanded"}], limit=2))
        evidence = [item["ranking_evidence"] for item in out["records"]]
        self.assertEqual(evidence[0]["uncalibrated_score"], evidence[1]["uncalibrated_score"])

    def test_score_contributions_close_and_control_matches_frozen_weight_sign(self):
        out = rerank_priority_pool(fixture_result())
        evidence = {row["candidate_id"]: row["ranking_evidence"] for row in out["records"]}
        for value in evidence.values():
            self.assertAlmostEqual(value["linear_score"], value["intercept"] + sum(value["logit_contributions"].values()), places=14)
            self.assertAlmostEqual(value["uncalibrated_score"], 1 / (1 + math.exp(-value["linear_score"])), places=14)
            self.assertIn("not_calibrated_probability", value["score_interpretation"])
        # A software control, not a scientific assertion that lower volume is better.
        self.assertGreater(evidence["C"]["uncalibrated_score"], evidence["A"]["uncalibrated_score"])

    def test_input_order_and_injected_property_labels_do_not_change_selection(self):
        original = fixture_result()
        altered = copy.deepcopy(original)
        altered["records"].reverse()
        for index, row in enumerate(altered["records"]):
            row.update(hull_distance_ev_atom=999 * index, low_hull=index % 2 == 0,
                       energy=-999, scientific_quality=100000)
        one, two = rerank_priority_pool(original), rerank_priority_pool(altered)
        self.assertEqual(one["selected_candidate_ids"], two["selected_candidate_ids"])
        self.assertEqual({row["candidate_id"]: row["priority_rank_score"] for row in one["records"]},
                         {row["candidate_id"]: row["priority_rank_score"] for row in two["records"]})

    def test_sha_then_id_tie_break_is_explicit_and_deterministic(self):
        rows = [fixture_row("C"), fixture_row("A"), fixture_row("B")]
        rows[0]["sha256"] = rows[1]["sha256"] = "0" * 64
        rows[2]["sha256"] = "1" * 64
        out = rerank_priority_pool(fixture_result(rows, limit=2))
        self.assertEqual(out["selected_candidate_ids"], ["A", "C"])
        self.assertEqual(out["records"][1]["selection_reason"]["tie_break"], "CIF_sha256_then_candidate_id")

    def test_coverage_bonus_is_based_only_on_formula_and_space_group(self):
        rows = [fixture_row("A", space_group=1), fixture_row("B", space_group=2)]
        rows[1]["size_bin"] = "21+"
        out = rerank_priority_pool(fixture_result(rows, limit=2))
        evidence = sorted(out["records"], key=lambda row: row["selection_rank"])
        self.assertAlmostEqual(evidence[0]["selection_reason"]["coverage_bonus"], .05)
        self.assertAlmostEqual(evidence[1]["selection_reason"]["coverage_bonus"], .025)
        self.assertNotIn("size_bin", evidence[1]["selection_reason"]["new_category_coverage"])

    def test_applied_policy_separates_legacy_ordering_from_active_explanation(self):
        legacy = [
            "Candidate ordering after the feasibility gate maximizes fixed categorical coverage; it does not rank physical performance or guarantee a globally optimal shortlist.",
            "Greedy fixed-weight category coverage is not a proof of globally optimal selection.",
        ]
        base = fixture_result()
        base["limitations"] += legacy
        base["policy"] = {"category_weights": {"reduced_formula": 1, "space_group_number": 1, "size_bin": 1},
                          "size_bins_sites": ["1-10", "11-20", "21+"], "symprec_angstrom": .1}
        out = rerank_priority_pool(base)
        self.assertEqual(out["active_ranking_policy"], POLICY_ID)
        self.assertEqual(out["policy"]["active_ranking_policy"], POLICY_ID)
        self.assertEqual(out["policy"]["symprec_angstrom"], .1)
        self.assertEqual(out["policy"]["legacy_precheck_provenance"]["category_weights"], base["policy"]["category_weights"])
        self.assertNotIn("category_weights", out["policy"])
        self.assertEqual(out["priority_ranking"]["legacy_ordering_limitations"], legacy)
        self.assertTrue(all(text not in out["limitations"] for text in legacy))
        self.assertIn("legacy limitation", out["limitations"])
        self.assertTrue(any("weak formula/space-group coverage bonus" in text for text in out["limitations"]))
        base["records"][0]["relaxation_priority"]["metrics"]["density_g_cm3"] = 0
        fallback = rerank_priority_pool(base)
        self.assertEqual(fallback["policy"], base["policy"])
        self.assertEqual(fallback["limitations"], base["limitations"])
        self.assertNotIn("active_ranking_policy", fallback)

    def assert_full_fallback(self, base, expected_reason):
        old = copy.deepcopy(base)
        out = rerank_priority_pool(base)
        self.assertEqual(out["priority_ranking"]["status"], "fallback_legacy")
        self.assertIn(expected_reason, json.dumps(out["priority_ranking"]["fallback_reasons"]))
        for key in ("selected_candidate_ids", "selected_count", "requested_count", "target_met", "coverage", "method", "stage_counts"):
            self.assertEqual(out[key], old[key])
        for row, original in zip(out["records"], old["records"]):
            for key in original:
                self.assertEqual(row[key], original[key])
        return out

    def test_one_invalid_priority_feature_causes_whole_pool_fallback(self):
        for value in (None, True, float("nan"), float("inf"), 0, -1):
            with self.subTest(value=value):
                base = fixture_result()
                base["records"][2]["relaxation_priority"]["metrics"]["density_g_cm3"] = value
                self.assert_full_fallback(base, "ranking_features_unavailable")
        base = fixture_result()
        del base["records"][0]["relaxation_priority"]["metrics"]["volume_per_atom_angstrom3"]
        self.assert_full_fallback(base, "ranking_features_unavailable")

    def test_unknown_charge_and_huge_counts_fall_back_without_excluding_rows(self):
        for change in ({"status": "unknown"}, {"composition": {"Fe": 10**100, "O": 4}},
                       {"target_cation_charge": float("inf")}, {"requires_mixed_valence": None}):
            with self.subTest(change=change):
                base = fixture_result()
                base["records"][0]["relaxation_priority"]["metrics"]["charge_feasibility"].update(change)
                self.assert_full_fallback(base, "ranking_features_unavailable")

    def test_untrained_anion_falls_back_but_does_not_change_mixed_gate(self):
        base = fixture_result([fixture_row("salt", composition={"Na": 1, "Cl": 1})], 1)
        out = self.assert_full_fallback(base, "anion_not_covered_in_training")
        self.assertTrue(out["records"][0]["relaxation_priority"]["eligible_for_relaxation_shortlist"])

    def test_unknown_system_and_out_of_range_features_warn_without_new_gate(self):
        rows = [fixture_row("novel", composition={"Mg": 1, "O": 1}, density=math.exp(700))]
        out = rerank_priority_pool(fixture_result(rows, 1))
        self.assertEqual(out["priority_ranking"]["status"], "applied")
        evidence = out["records"][0]["ranking_evidence"]
        self.assertIn("chemical_system_not_seen_in_training_extrapolation", evidence["warnings"])
        self.assertIn("features_outside_observed_training_range", evidence["warnings"])
        self.assertIn("standardized_features_clipped_to_frozen_limits", evidence["warnings"])
        self.assertEqual(evidence["standardized_features"]["log_density"], 5)
        self.assertEqual(out["selected_count"], 1)

    def test_corrupt_bundled_model_falls_back_and_empty_pool_is_explicit(self):
        with patch.object(priority_ranking_model, "MODEL_JSON", priority_ranking_model.MODEL_JSON + " "):
            self.assert_full_fallback(fixture_result(), "ranking_model_unavailable")
        base = fixture_result()
        for row in base["records"]:
            row["qualified"] = False
            row["selected"] = False
            row["relaxation_priority"]["eligible_for_relaxation_shortlist"] = False
        base.update(selected_candidate_ids=[], selected_count=0)
        out = rerank_priority_pool(base)
        self.assertEqual(out["priority_ranking"]["status"], "no_priority_candidates")
        self.assertEqual(out["selected_candidate_ids"], [])

    def test_wrapper_passes_all_gate_and_caller_options_unchanged(self):
        base = fixture_result()
        with patch("matpilot.priority_ranking.recommend_with_mixed_valence", return_value=base) as gate:
            result = recommend_ranked([{"candidate_id": "raw"}], ["Fe", "O"], 2,
                priority_candidate_ids=["raw"], priority_selection_basis="operator", symprec=.2, angle_tolerance=6)
        gate.assert_called_once_with([{"candidate_id": "raw"}], ["Fe", "O"], 2,
            priority_candidate_ids=["raw"], priority_selection_basis="operator", symprec=.2, angle_tolerance=6)
        self.assertEqual(result["priority_ranking"]["priority_pool_count"], 3)


@unittest.skipUnless(os.environ.get("MATPILOT_RANKING_DEVELOPMENT_DIR"), "optional parity uses only the old, explicitly designated development dataset")
class PriorityDevelopmentParityTests(unittest.TestCase):
    def test_all_335_feature_vectors_scores_and_final_fit_selections_match_independent_math(self):
        import numpy as np
        from scipy.special import expit
        root = Path(os.environ["MATPILOT_RANKING_DEVELOPMENT_DIR"])
        samples = json.loads((root / "development_features.json").read_text(encoding="utf-8"))
        raw_model = (root / "ranking_model.json").read_bytes()
        model = json.loads(raw_model)
        self.assertEqual(hashlib.sha256(raw_model).hexdigest(), priority_ranking_model.MODEL_SHA256)
        self.assertEqual(hashlib.sha256((root / "development_features.json").read_bytes()).hexdigest(), model["training"]["feature_input_sha256"])
        self.assertEqual(len(samples), 335)
        checked = 0
        for system in model["training"]["systems"]:
            base = json.loads((root.parent / "2026-10-31-mixed-valence" / "runs" / "new" / system / "result.json").read_text(encoding="utf-8"))
            actual = rerank_priority_pool(base)
            self.assertEqual(actual["priority_ranking"]["status"], "applied")
            by_id = {row["candidate_id"]: row for row in actual["records"]}
            sample = [row for row in samples if row["system"] == system]
            matrix = np.array([row["x"] for row in sample])
            z = np.clip((matrix - np.array(model["mean"])) / np.array(model["scale"]), -5, 5)
            scores = expit(model["intercept"] + z @ np.array(model["weights"]))
            for index, row in enumerate(sample):
                evidence = by_id[row["id"]]["ranking_evidence"]
                np.testing.assert_allclose([evidence["features"][name] for name in FEATURE_NAMES], row["x"], rtol=0, atol=1e-12)
                self.assertAlmostEqual(evidence["uncalibrated_score"], float(scores[index]), places=13)
                checked += 1
            remaining = list(range(len(sample)))
            selected, formulas, groups = [], set(), set()
            while remaining and len(selected) < base["requested_count"]:
                index = min(remaining, key=lambda i: (
                    -float(scores[i]) - model["coverage_bonus"] * ((sample[i]["formula"] not in formulas) + (sample[i]["sg"] not in groups)) / 2,
                    sample[i]["sha256"], sample[i]["id"]))
                remaining.remove(index)
                selected.append(sample[index]["id"])
                formulas.add(sample[index]["formula"])
                groups.add(sample[index]["sg"])
            self.assertEqual(actual["selected_candidate_ids"], selected)
            self.assertEqual(actual["selected_count"], base["selected_count"])
        self.assertEqual(checked, 335)


if __name__ == "__main__":
    unittest.main()
