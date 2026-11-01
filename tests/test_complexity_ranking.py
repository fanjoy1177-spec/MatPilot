"""Contract checks for the explicit composition heuristic; not scientific proof."""
import copy
import hashlib
import unittest
from unittest.mock import patch

from matpilot.mixed_valence import formal_charge_balance
from matpilot.complexity_ranking import (
    POLICY_ID, policy_description, recommend_complexity,
    reduced_formula_atom_count, rerank_by_complexity,
)


def fixture_row(name, composition=None):
    composition = composition or {"Fe": 1, "Ni": 1, "O": 3}
    charge = formal_charge_balance(composition)
    assert charge["status"] == "feasible"
    count = sum(composition.values())
    return {
        "candidate_id": name, "sha256": hashlib.sha256(name.encode()).hexdigest(),
        "qualified": True, "selected": False, "site_count": count,
        "reduced_formula": str(sorted(composition.items())), "space_group_number": 1,
        "size_bin": "1-10" if count <= 10 else "11-20", "exclusion_reasons": [],
        "relaxation_priority": {"tier": "priority_relaxation", "eligible_for_relaxation_shortlist": True,
            "metrics": {"charge_feasibility": charge, "composition": dict(composition),
                "input_site_count": count, "cell_volume_angstrom3": count * 12,
                "volume_per_atom_angstrom3": 12, "density_g_cm3": 5}},
    }


def fixture_result(rows=None, limit=2):
    if rows is None:
        rows = [fixture_row("A", {"Fe": 1, "Ni": 1, "O": 2}),
                fixture_row("B", {"Fe": 1, "Ni": 1, "O": 3}),
                fixture_row("C", {"Fe": 3, "Ni": 3, "O": 8})]
    rows = copy.deepcopy(rows)
    pool = [row for row in rows if row.get("qualified") is True
            and row.get("relaxation_priority", {}).get("eligible_for_relaxation_shortlist") is True]
    selected = pool[:limit]
    for index, row in enumerate(selected):
        row.update(selected=True, selection_rank=index + 1, selection_reason={"legacy": True})
    return {
        "records": rows, "selected_candidate_ids": [row["candidate_id"] for row in selected],
        "selected_count": len(selected), "requested_count": limit, "target_met": len(selected) == limit,
        "method": "legacy", "limitations": ["retained gate limitation"],
        "policy": {"category_weights": {"reduced_formula": 1, "space_group_number": 1, "size_bin": 1},
                   "size_bins_sites": ["1-10", "11-20", "21+"], "symprec_angstrom": .1},
        "coverage": {key: {"selected_values": sorted({row[key] for row in selected}),
                           "selected_count": len({row[key] for row in selected}),
                           "pool_count": len({row[key] for row in pool})}
                     for key in ("reduced_formula", "space_group_number", "size_bin")},
        "stage_counts": {"priority_relaxation_count": len(pool), "manual_review_count": len(rows) - len(pool),
                         "selected_count": len(selected), "shortfall_count": max(0, limit - len(selected))},
    }


class ComplexityRankingTests(unittest.TestCase):
    def test_policy_discloses_development_choice_and_has_no_predictive_score(self):
        card = policy_description()
        self.assertEqual(card["id"], POLICY_ID)
        self.assertTrue(card["policy_choice_informed_by_development_labels"])
        self.assertFalse(card["runtime_property_labels_used"])
        self.assertFalse(card["fitted_model"])
        result = rerank_by_complexity(fixture_result())
        for row in result["records"]:
            self.assertIsNone(row["ranking_evidence"]["uncalibrated_score"])
            self.assertNotIn("priority_rank_score", row)

    def test_integer_reduction_controls_order_with_same_pool_and_budget(self):
        base = fixture_result()
        before = copy.deepcopy(base)
        result = rerank_by_complexity(base)
        self.assertEqual(base, before)
        self.assertEqual(result["selected_candidate_ids"], ["C", "B"])
        self.assertEqual(result["selected_count"], base["selected_count"])
        self.assertEqual(result["stage_counts"]["priority_relaxation_count"], 3)
        self.assertEqual([row["ranking_evidence"]["reduced_formula_atom_count"] for row in result["records"]], [4, 5, 14])
        for row, original in zip(result["records"], before["records"]):
            for key in ("qualified", "exclusion_reasons", "relaxation_priority"):
                self.assertEqual(row[key], original[key])
        self.assertEqual({row["candidate_id"] for row in result["records"] if row["selected"]}, {"B", "C"})
        self.assertEqual(result["records"][0]["not_selected_reason"],
                         "requested_shortlist_limit_reached_under_reduced_composition_heuristic")

    def test_primary_count_invariant_to_integer_supercell_not_input_site_size(self):
        original = fixture_row("A", {"Fe": 1, "Ni": 1, "O": 3})
        expanded = fixture_row("A", {"Fe": 7, "Ni": 7, "O": 21})
        self.assertEqual(reduced_formula_atom_count(original), 5)
        self.assertEqual(reduced_formula_atom_count(expanded), 5)
        base = fixture_result([original, fixture_row("B", {"Fe": 3, "Ni": 3, "O": 8})], 1)
        altered = fixture_result([expanded, base["records"][1]], 1)
        self.assertEqual(rerank_by_complexity(base)["selected_candidate_ids"], ["B"])
        self.assertEqual(rerank_by_complexity(altered)["selected_candidate_ids"], ["B"])

    def test_sha_then_id_tie_ignores_input_site_count_and_categories(self):
        rows = [fixture_row("C"), fixture_row("A"), fixture_row("B")]
        rows[0]["sha256"] = rows[1]["sha256"] = "0" * 64
        rows[2]["sha256"] = "1" * 64
        rows[0]["site_count"], rows[1]["site_count"], rows[2]["site_count"] = 1, 1000, 2
        rows[2]["space_group_number"] = 225
        rows[2]["size_bin"] = "rare"
        result = rerank_by_complexity(fixture_result(rows, 2))
        self.assertEqual(result["selected_candidate_ids"], ["A", "C"])
        self.assertEqual(result["records"][1]["selection_reason"]["tie_break"], "CIF_sha256_then_candidate_id")

    def test_order_and_property_label_injection_do_not_change_ranking(self):
        base = fixture_result()
        altered = copy.deepcopy(base)
        altered["records"].reverse()
        for index, row in enumerate(altered["records"]):
            row.update(hull_distance_ev_atom=-999 * index, low_hull=index % 2 == 0,
                       scientific_quality=9999, predicted_hull=-100)
        one, two = rerank_by_complexity(base), rerank_by_complexity(altered)
        self.assertEqual(one["selected_candidate_ids"], two["selected_candidate_ids"])
        self.assertEqual({row["candidate_id"]: row["ranking_evidence"]["heuristic_key"] for row in one["records"]},
                         {row["candidate_id"]: row["ranking_evidence"]["heuristic_key"] for row in two["records"]})

    def test_nonpriority_records_remain_unselected_even_with_missing_metrics(self):
        rows = fixture_result()["records"]
        rows.append({"candidate_id": "review", "qualified": False, "selected": False,
                     "relaxation_priority": {"eligible_for_relaxation_shortlist": False}})
        base = fixture_result(rows, 2)
        result = rerank_by_complexity(base)
        self.assertEqual(result["selected_candidate_ids"], ["C", "B"])
        self.assertFalse(result["records"][-1]["selected"])
        self.assertEqual(result["records"][-1]["ranking_evidence"]["status"], "outside_priority_pool")
        self.assertEqual(result["stage_counts"]["manual_review_count"], 1)

    def assert_full_fallback(self, base, expected_reason):
        original = copy.deepcopy(base)
        result = rerank_by_complexity(base)
        self.assertEqual(base, original)
        self.assertEqual(result["priority_ranking"]["status"], "fallback_legacy")
        self.assertIn(expected_reason, str(result["priority_ranking"]["fallback_reasons"]))
        for key in ("selected_candidate_ids", "selected_count", "target_met", "coverage", "method", "stage_counts", "policy", "limitations"):
            self.assertEqual(result[key], original[key])
        for row, old in zip(result["records"], original["records"]):
            for key in old:
                self.assertEqual(row[key], old[key])
        return result

    def test_invalid_counts_trigger_whole_pool_fallback(self):
        for value in (0, -1, True, None, "2", 2.0, .5, float("nan"), float("inf"), 10**100):
            with self.subTest(value=value):
                base = fixture_result()
                base["records"][2]["relaxation_priority"]["metrics"]["charge_feasibility"]["composition"]["Fe"] = value
                self.assert_full_fallback(base, "ranking_inputs_unavailable")

    def test_malformed_or_missing_composition_triggers_whole_pool_fallback(self):
        for value in ({}, [], {"Fe": 1}, {"invalid": 1, "O": 3}, {"Fe": 600, "O": 600}):
            with self.subTest(value=value):
                base = fixture_result()
                base["records"][2]["relaxation_priority"]["metrics"]["charge_feasibility"]["composition"] = value
                self.assert_full_fallback(base, "ranking_inputs_unavailable")
        base = fixture_result()
        del base["records"][2]["relaxation_priority"]["metrics"]["charge_feasibility"]["composition"]
        self.assert_full_fallback(base, "required_verified_composition_missing")

    def test_malformed_sha_triggers_whole_pool_fallback(self):
        for value in (None, "", "0" * 63, "G" * 64, 1, True):
            with self.subTest(value=value):
                base = fixture_result()
                base["records"][0]["sha256"] = value
                self.assert_full_fallback(base, "missing_or_invalid_verified_cif_sha256")

    def test_invalid_charge_and_ambiguous_ids_fall_back(self):
        for value in ({"status": "unknown"}, {"mixed_possible": False}):
            base = fixture_result()
            base["records"][0]["relaxation_priority"]["metrics"]["charge_feasibility"].update(value)
            self.assert_full_fallback(base, "priority_charge_not_feasible")
        base = fixture_result()
        base["records"][1]["candidate_id"] = base["records"][0]["candidate_id"]
        self.assert_full_fallback(base, "invalid_or_ambiguous_candidate_ids")

    def test_shortfall_and_empty_pool_preserve_exact_available_quota(self):
        short = rerank_by_complexity(fixture_result(limit=20))
        self.assertEqual(short["selected_count"], 3)
        self.assertFalse(short["target_met"])
        self.assertEqual(short["stage_counts"]["shortfall_count"], 17)
        empty = rerank_by_complexity(fixture_result([], 20))
        self.assertEqual(empty["priority_ranking"]["status"], "no_priority_candidates")
        self.assertEqual(empty["selected_candidate_ids"], [])

    def test_inconsistent_legacy_quota_and_coverage_fall_back(self):
        base = fixture_result()
        base["selected_candidate_ids"] = ["A"]
        self.assert_full_fallback(base, "legacy_selection_inconsistent")
        base = fixture_result()
        base["coverage"]["unavailable_category"] = {}
        self.assert_full_fallback(base, "coverage_audit_unavailable")

    def test_active_explanation_separates_legacy_coverage_policy(self):
        base = fixture_result()
        legacy_text = "Greedy fixed-weight category coverage is not a proof of globally optimal selection."
        base["limitations"].append(legacy_text)
        result = rerank_by_complexity(base)
        self.assertEqual(result["active_ranking_policy"], POLICY_ID)
        self.assertEqual(result["policy"]["active_ranking_policy"], POLICY_ID)
        self.assertNotIn("category_weights", result["policy"])
        self.assertEqual(result["policy"]["legacy_precheck_provenance"]["category_weights"], base["policy"]["category_weights"])
        self.assertEqual(result["policy"]["symprec_angstrom"], .1)
        self.assertNotIn(legacy_text, result["limitations"])
        self.assertIn(legacy_text, result["priority_ranking"]["legacy_ordering_limitations"])
        self.assertIn("retained gate limitation", result["limitations"])

    def test_wrapper_passes_all_caller_and_gate_arguments_unchanged(self):
        with patch("matpilot.complexity_ranking.recommend_with_mixed_valence", return_value=fixture_result()) as gate:
            result = recommend_complexity([{"candidate_id": "raw"}], ["Fe", "Ni", "O"], 2,
                priority_candidate_ids=["raw"], priority_selection_basis="operator", symprec=.2, angle_tolerance=6)
        gate.assert_called_once_with([{"candidate_id": "raw"}], ["Fe", "Ni", "O"], 2,
            priority_candidate_ids=["raw"], priority_selection_basis="operator", symprec=.2, angle_tolerance=6)
        self.assertEqual(result["priority_ranking"]["status"], "applied")


if __name__ == "__main__":
    unittest.main()
