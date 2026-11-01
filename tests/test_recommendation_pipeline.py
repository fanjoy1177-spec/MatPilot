"""Regression and synthetic-control tests, not material-property validation."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pymatgen.core import Lattice, Structure
from pymatgen.io.cif import CifWriter

from matpilot.recommendation_pipeline import recommend_pipeline
from matpilot.relaxation_triage import analyze_relaxation_candidate, recommend_for_relaxation

POLICY = {"id": "software-control", "scope": "synthetic fixtures, any declared element system",
          "evidence_scope": "control signals only, not material properties", "score_order": "higher_is_better"}
RANKING = {"id": "synthetic-comparable-rank", "description": "Lower synthetic key preferred",
           "evidence_scope": "software controls; upstream scientific evidence validation is not exercised here",
           "target_direction": "minimize", "selection_sha256": "test-only-not-real-evidence",
           "comparable_group": {"method": "software-control", "unit": "test"}}


def control(record, expected, **kwargs):
    return {"tier": "priority_relaxation", "priority": "软件控制信号", "score": record.get("software_score", 100),
            "eligible_for_relaxation_shortlist": True, "issues": [], "metrics": {"software_fixture": True}}


class RecommendationPipelineTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.binary = Structure(Lattice.cubic(5), ["Na", "Cl"], [[0, 0, 0], [.5, .5, .5]])
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def record(self, cid, crystal=None, **extra):
        self.counter += 1
        data = str(CifWriter(crystal if crystal is not None else self.binary)).encode("utf-8")
        path = self.root / f"fixture-{self.counter}.cif"
        path.write_bytes(data)
        return {"candidate_id": cid, "structure_version_id": f"v-{cid}-{self.counter}",
                "path": str(path), "sha256": hashlib.sha256(data).hexdigest(), **extra}

    def run_pipeline(self, records, elements=None, limit=12, **kwargs):
        return recommend_pipeline(records, elements or ["Na", "Cl"], limit,
            triage_callback=kwargs.pop("triage_callback", control), policy=kwargs.pop("policy", POLICY), **kwargs)

    def equivalent_pair(self):
        other = self.binary.copy()
        other.scale_lattice(self.binary.volume * 1.2)
        return [self.record("A", software_score=100), self.record("B", other, software_score=50)]

    def distinct_candidates(self):
        stretched = Structure(Lattice.orthorhombic(8, 5, 5), ["Na", "Cl"], [[0, 0, 0], [.5, .5, .5]])
        different_formula = Structure(Lattice.cubic(5), ["Na", "Na", "Cl"],
                                      [[0, 0, 0], [.25, .25, .25], [.5, .5, .5]])
        return [self.record("A"), self.record("B", stretched), self.record("C", different_formula)]

    def test_legacy_heuristic_counterexample_is_repaired_before_deduplication(self):
        good = Structure(Lattice.cubic(5), ["Fe", "Ni", "O", "O"],
                         [[0, 0, 0], [.5, .5, 0], [.5, 0, .5], [0, .5, .5]])
        expanded = good.copy()
        expanded.scale_lattice(good.volume * 8)
        rows = [self.record("A_expanded", expanded), self.record("B_original", good)]
        old = recommend_for_relaxation(rows, ["Fe", "Ni", "O"], 1)
        new = self.run_pipeline(rows, ["Fe", "Ni", "O"], 1, triage_callback=analyze_relaxation_candidate,
            policy={**POLICY, "id": "frozen-fe-ni-o-adapter", "scope": "explicit legacy Fe-Ni-O regression only"})
        self.assertEqual(old["selected_candidate_ids"], [])
        self.assertEqual(new["selected_candidate_ids"], ["B_original"])
        self.assertEqual(new["records"][0]["relaxation_priority"]["tier"], "manual_review")
        self.assertEqual(new["records"][0]["representative_id"], "B_original")
        self.assertEqual(new["stage_counts"]["structural_precheck_count"], 2)
        self.assertEqual(new["stage_counts"]["structural_representative_count"], 1)

    def test_generic_non_fenio_policy_selects_best_score_within_equivalent_group(self):
        rows = self.equivalent_pair()
        rows[0]["software_score"], rows[1]["software_score"] = 25, 75
        result = self.run_pipeline(list(reversed(rows)), limit=1)
        self.assertEqual(result["selected_candidate_ids"], ["B"])
        self.assertEqual(result["expected_elements"], ["Cl", "Na"])
        self.assertEqual(result["records"][0]["representative_id"], "B")
        self.assertEqual(result["stage_counts"]["priority_relaxation_count"], 2)
        self.assertEqual(result["stage_counts"]["priority_representative_count"], 1)

    def test_evidence_wins_over_heuristic_for_equivalent_representative(self):
        rows = self.equivalent_pair()
        result = self.run_pipeline(rows, limit=1, rank_keys={"A": (5,), "B": (1,)}, ranking_metadata=RANKING)
        self.assertEqual(result["selected_candidate_ids"], ["B"])
        self.assertEqual(result["records"][0]["representative_id"], "B")
        self.assertEqual(result["records"][1]["evidence_rank_key"], [1])
        self.assertEqual(result["ranking_audit"]["metadata"]["comparable_group"], RANKING["comparable_group"])

    def test_cross_representative_quality_precedes_diversity(self):
        rows = self.distinct_candidates()
        coverage = self.run_pipeline(rows, limit=2)
        ranked = self.run_pipeline(rows, limit=2, rank_keys={"A": (0,), "B": (1,), "C": (2,)}, ranking_metadata=RANKING)
        self.assertEqual(coverage["selected_candidate_ids"], ["A", "C"])
        self.assertEqual(ranked["selected_candidate_ids"], ["A", "B"])
        self.assertEqual(ranked["records"][1]["selection_reason"]["ranking_mode"], "evidence_key_then_coverage")

    def test_equal_evidence_keys_use_diversity_and_multidimensional_keys_are_lexicographic(self):
        rows = self.distinct_candidates()
        equal = self.run_pipeline(rows, limit=2, rank_keys={"A": (0,), "B": (0,), "C": (0,)}, ranking_metadata=RANKING)
        self.assertEqual(equal["selected_candidate_ids"], ["A", "C"])
        multidimensional = self.run_pipeline(rows, limit=2,
            rank_keys={"A": (1, 0), "B": (0, 5), "C": (0, 2)}, ranking_metadata=RANKING)
        self.assertEqual(multidimensional["selected_candidate_ids"], ["C", "B"])

    def test_empty_rank_map_is_missing_evidence_not_no_ranking(self):
        rows = self.equivalent_pair()
        result = self.run_pipeline(rows, rank_keys={}, ranking_metadata=RANKING)
        self.assertEqual(result["selected_count"], 0)
        self.assertTrue(result["ranking_audit"]["enabled"])
        self.assertEqual(result["stage_counts"]["manual_review_count"], 2)
        self.assertEqual(result["ranking_audit"]["missing_priority_candidate_ids"], ["A", "B"])
        self.assertTrue(all(row["upstream_triage"]["tier"] == "priority_relaxation" for row in result["records"]))
        self.assertTrue(all(row["relaxation_priority"]["issues"] == ["missing_comparable_ranking_evidence"] for row in result["records"]))

    def test_missing_evidence_cannot_displace_evidenced_equivalent_candidate(self):
        rows = self.equivalent_pair()
        result = self.run_pipeline(rows, limit=1, rank_keys={"B": (3,)}, ranking_metadata=RANKING)
        self.assertEqual(result["selected_candidate_ids"], ["B"])
        self.assertEqual(result["records"][0]["relaxation_priority"]["tier"], "manual_review")
        self.assertEqual(result["records"][0]["representative_id"], "B")
        with self.assertRaisesRegex(ValueError, "lack comparable ranking evidence"):
            self.run_pipeline(rows, rank_keys={"B": (3,)}, ranking_metadata=RANKING, missing_rank_policy="error")

    def test_invalid_rank_keys_are_rejected_without_numeric_coercion(self):
        rows = self.equivalent_pair()
        invalid = [{"unknown": (0,)}, {"A": ()}, {"A": [True]}, {"A": ["0"]},
                   {"A": [float("nan")]}, {"A": [float("inf")]}, {"A": [10**1000]},
                   {"A": (0,), "B": (1, 2)}, {"A": 0}, []]
        for keys in invalid:
            with self.subTest(keys=repr(keys)[:100]), self.assertRaises(ValueError):
                self.run_pipeline(rows, rank_keys=keys, ranking_metadata=RANKING)
        with self.assertRaises(ValueError):
            self.run_pipeline(rows, rank_keys={"A": (0,)})
        with self.assertRaises(ValueError):
            self.run_pipeline(rows, ranking_metadata=RANKING)
        with self.assertRaises(ValueError):
            self.run_pipeline(rows, missing_rank_policy="best")

    def test_no_callback_or_undeclared_policy_is_not_a_universal_chemistry_default(self):
        rows = [self.record("A")]
        with self.assertRaises(ValueError):
            self.run_pipeline(rows, triage_callback=None)
        with self.assertRaises(ValueError):
            self.run_pipeline(rows, policy={"id": "undeclared"})
        with self.assertRaises(ValueError):
            self.run_pipeline(rows, policy={**POLICY, "score_order": []})
        with self.assertRaises(ValueError):
            self.run_pipeline(rows, elements=["NotAnElement"])

    def test_unknown_unsupported_invalid_or_failed_callbacks_remain_review(self):
        rows = [self.record("A")]
        def unsupported(*args, **kwargs):
            return {"tier": "manual_review", "score": None, "eligible_for_relaxation_shortlist": False,
                    "issues": ["unsupported_chemical_system"]}
        def failure(*args, **kwargs):
            raise RuntimeError("Controlled analysis failure")
        callbacks = [unsupported, failure, lambda *a, **k: None,
                     lambda *a, **k: {"tier": "priority_relaxation", "eligible_for_relaxation_shortlist": True, "score": float("nan")},
                     lambda *a, **k: {"tier": "manual_review", "eligible_for_relaxation_shortlist": True}]
        for callback in callbacks:
            with self.subTest(callback=callback):
                result = self.run_pipeline(rows, triage_callback=callback)
                self.assertEqual(result["selected_count"], 0)
                self.assertEqual(result["records"][0]["relaxation_priority"]["tier"], "manual_review")
        # Missing-rank error applies only to candidates the upstream policy allows.
        result = self.run_pipeline(rows, triage_callback=unsupported, rank_keys={}, ranking_metadata=RANKING, missing_rank_policy="error")
        self.assertEqual(result["selected_count"], 0)

    def test_duplicate_ids_are_retained_as_failures_and_never_reach_callback(self):
        rows = [self.record("duplicate"), self.record("duplicate")]
        calls = []
        def traced(*args, **kwargs):
            calls.append(1)
            return control(*args, **kwargs)
        result = self.run_pipeline(rows, triage_callback=traced)
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(calls, [])
        self.assertTrue(all("ambiguous_duplicate_candidate_id" in row["exclusion_reasons"] for row in result["records"]))
        with self.assertRaises(ValueError):
            self.run_pipeline(rows, rank_keys={"duplicate": (0,)}, ranking_metadata=RANKING)

    def test_selector_is_explicit_and_cannot_be_bypassed_by_best_evidence(self):
        rows = self.distinct_candidates()
        result = self.run_pipeline(rows, limit=1, rank_keys={"A": (0,), "B": (1,), "C": (2,)}, ranking_metadata=RANKING,
            priority_candidate_ids=["C"], priority_selection_basis="Explicit operator restriction")
        self.assertEqual(result["selected_candidate_ids"], ["C"])
        self.assertEqual(result["caller_priority_selector"]["candidate_ids"], ["C"])
        self.assertIn("outside_explicit_priority_selector", result["records"][0]["exclusion_reasons"])
        empty = self.run_pipeline(rows, priority_candidate_ids=[], priority_selection_basis="No candidates permitted")
        self.assertEqual(empty["selected_count"], 0)
        for ids, basis in [(["unknown"], "unknown ID"), (["A", "A"], "duplicate"), (["A"], None), (None, "orphan basis")]:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.run_pipeline(rows, priority_candidate_ids=ids, priority_selection_basis=basis)

    def test_hash_mismatch_and_hard_failure_cannot_be_promoted(self):
        bad_hash = self.record("A")
        bad_hash["sha256"] = "0" * 64
        hard_failure = self.record("B", all_hard_requirements_pass=False)
        result = self.run_pipeline([bad_hash, hard_failure], rank_keys={"A": (-100,), "B": (-99,)}, ranking_metadata=RANKING)
        self.assertEqual(result["selected_count"], 0)
        self.assertIn("artifact_sha256_mismatch", result["records"][0]["exclusion_reasons"])
        self.assertIn("existing_hard_requirement_checks_not_passed", result["records"][1]["exclusion_reasons"])

    def test_partial_occupancy_is_not_repaired_by_callback(self):
        crystal = Structure(Lattice.cubic(5), [{"Na": .5}, "Cl"], [[0, 0, 0], [.5, .5, .5]])
        result = self.run_pipeline([self.record("A", crystal)])
        self.assertEqual(result["selected_count"], 0)
        self.assertIn("partial_occupancy_or_disordered_sites", result["records"][0]["exclusion_reasons"])

    def test_equivalence_failure_does_not_invent_a_distinct_representative(self):
        rows = self.equivalent_pair()
        with patch("pymatgen.analysis.structure_matcher.StructureMatcher.fit", side_effect=RuntimeError("controlled failure")):
            result = self.run_pipeline(rows, limit=2)
        self.assertEqual(result["selected_candidate_ids"], ["A"])
        self.assertEqual(result["representative_selection"]["equivalence_unknown_candidate_ids"], ["B"])
        self.assertIn("structural_equivalence_unknown", result["records"][1]["exclusion_reasons"])
        self.assertIsNone(result["records"][1]["representative_id"])

    def test_callback_changes_to_input_dict_do_not_change_identity_or_metadata(self):
        rows = self.equivalent_pair()
        snapshot = deepcopy(rows)
        metadata = deepcopy(RANKING)
        def mutate(record, expected, **kwargs):
            record["candidate_id"] = "not-the-real-id"
            expected.clear()
            return control(record, expected, **kwargs)
        result = self.run_pipeline(rows, triage_callback=mutate, rank_keys={"A": (1,), "B": (0,)}, ranking_metadata=metadata)
        self.assertEqual(result["selected_candidate_ids"], ["B"])
        self.assertEqual(rows, snapshot)
        result["ranking_audit"]["metadata"]["comparable_group"]["unit"] = "changed"
        self.assertEqual(metadata, RANKING)
        json.dumps(result)  # Public tool result remains ordinary JSON.

    def test_later_callback_cannot_change_an_earlier_candidate_without_detection(self):
        rows = self.distinct_candidates()[:2]
        def mutate_file(record, expected, **kwargs):
            if record["candidate_id"] == "B":
                Path(rows[0]["path"]).write_text("changed by controlled test", encoding="utf-8")
            return control(record, expected, **kwargs)
        result = self.run_pipeline(rows, triage_callback=mutate_file, rank_keys={"A": (0,), "B": (1,)}, ranking_metadata=RANKING)
        self.assertEqual(result["selected_candidate_ids"], ["B"])
        self.assertIn("artifact_sha256_mismatch_after_triage", result["records"][0]["exclusion_reasons"])
        self.assertFalse(result["records"][0]["artifact_verified"])

    def test_score_order_none_does_not_apply_heuristic_numbers(self):
        rows = self.equivalent_pair()
        rows[0]["software_score"], rows[1]["software_score"] = 0, 100
        result = self.run_pipeline(rows, policy={**POLICY, "score_order": "none"})
        self.assertEqual(result["selected_candidate_ids"], ["A"])

    def test_invalid_top_level_limits_and_shapes_are_rejected(self):
        rows = [self.record("A")]
        for limit in (True, 0, 257, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.run_pipeline(rows, limit=limit)
        for value in (0, -1, float("nan"), True):
            with self.subTest(symprec=value), self.assertRaises(ValueError):
                self.run_pipeline(rows, symprec=value)
        with self.assertRaises(ValueError):
            self.run_pipeline([])


if __name__ == "__main__":
    unittest.main()
