"""Actual production checks over synthetic CPU replay, never material validation."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from matpilot.config import Settings
from matpilot.requirements import contract, audit_requirements
from matpilot.study_replay import ReplayError, ReplaySession


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("study_matpilot_checks", ROOT / "benchmarks/study_drivers/matpilot_checks.py")
CHECKS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKS)
MANIFEST = ROOT / "benchmarks/study/fixtures.development.json"


class MatPilotStudyChecksTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.counter = 0
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def replay(self, fixture="development_control_semantics_0"):
        self.counter += 1
        return ReplaySession(MANIFEST, fixture, self.root / f"replay-{self.counter}", f"replay-{self.counter}")

    def layer(self, replay, **flags):
        self.counter += 1
        settings = Settings(data_dir=self.root / f"store-{self.counter}", real_compute_enabled=False)
        return CHECKS.MatPilotChecks(settings, replay, **flags)

    def sample(self, replay, count=4):
        route = self.manifest["fixtures"][replay.fixture_id]["routes"]["generate"]
        return replay.invoke("sample", {**route, "num_samples": count})

    def request(self):
        example = copy.deepcopy(contract()["example"])
        return {**example, "conditions": {"chemical_system": "Fe-Ni-O"}, "num_samples": 4, "batch_size": 2}

    def test_mapping_ablation_changes_decisions_without_global_patching(self):
        replay = self.replay()
        full = self.layer(replay)
        without = self.layer(replay, requirement_mapping=False)
        request = self.request()
        request["requirements"] = request["requirements"][:1]  # Omit the explicit source budget.
        with patch("matpilot.service.audit_requirements", wraps=audit_requirements) as actual_audit:
            full_plan = full.prepare_plan(request)["plan"]
            ablated_plan = without.prepare_plan(request)["plan"]
            self.assertEqual(actual_audit.call_count, 1)
        self.assertNotEqual(full_plan["state"], "ready")
        self.assertTrue(full_plan["coverage"]["uncovered_signals"])
        self.assertEqual(ablated_plan["state"], "ready")
        self.assertEqual(ablated_plan["requirement_mappings"], [])
        self.assertIsNone(ablated_plan["coverage"]["program_mapping_coverage"])
        self.assertEqual(full_plan["conditions"], ablated_plan["conditions"])
        self.assertEqual(full_plan["num_samples"], ablated_plan["num_samples"])
        # No permanent mutation: the ordinary service still catches the omission.
        self.assertNotEqual(full.prepare_plan(request)["plan"]["state"], "ready")

    def test_mapping_ablation_preserves_units_and_adapter_input_rejection(self):
        replay = self.replay()
        without = self.layer(replay, requirement_mapping=False)
        request = {"raw_request": "带隙目标1200 meV", "num_samples": 1,
                   "conditions": {"dft_band_gap": {"value": 1200, "unit": "meV"}},
                   "requirements": [{"requirement_id": "gap", "source": {"quote": "1200 meV"},
                                     "kind": "numeric_target", "property": "dft_band_gap", "value": 1200,
                                     "unit": "meV", "strength": "goal", "relation": "target"}]}
        plan = without.prepare_plan(request)["plan"]
        self.assertEqual(plan["requirements"][0]["normalized_value"], 1.2)
        self.assertEqual(plan["requirements"][0]["normalized_unit"], "eV")
        self.assertEqual(plan["conditions"]["dft_band_gap"], 1.2)
        invalid = self.request()
        invalid["num_samples"] = 999
        self.assertNotEqual(without.prepare_plan(invalid)["plan"]["state"], "ready")

    def test_real_screening_checks_bytes_elements_geometry_and_duplicate_structures(self):
        replay = self.replay()
        layer = self.layer(replay)
        generated = self.sample(replay)
        self.assertNotIn("basic_geometry_pass", generated["records"][0])
        plan = layer.prepare_plan(self.request())["plan"]
        with patch.object(CHECKS.evidence, "screen_records", wraps=CHECKS.evidence.screen_records) as screen:
            result = layer.check_candidates(plan["plan_id"], [r["candidate_id"] for r in generated["records"]])
            self.assertEqual(screen.call_count, 1)
        rows = {r["candidate_id"]: r for r in result["records"]}
        self.assertTrue(rows["c_a"]["basic_geometry_pass"])
        self.assertTrue(rows["c_a"]["all_hard_requirements_pass"])
        self.assertFalse(rows["c_c"]["all_hard_requirements_pass"])
        self.assertEqual(rows["c_c"]["requirement_checks"][0]["status"], "fail")
        self.assertEqual(rows["c_b"]["equivalent_to_candidate_id"], "c_a")
        self.assertLess(result["unique_valid_candidate_count"], result["valid_candidate_count"])
        self.assertFalse(result["study_checks"]["science_computation_performed"])
        self.assertEqual(rows["c_a"]["provenance"]["kind"], "synthetic")

    def test_evidence_ablation_preserves_mapping_and_raw_access_but_calls_no_checks(self):
        replay = self.replay()
        layer = self.layer(replay, evidence_checks=False)
        generated = self.sample(replay)
        plan = layer.prepare_plan(self.request())["plan"]
        self.assertEqual(plan["coverage"]["program_mapping_coverage"], 1)
        with patch.object(CHECKS.evidence, "screen_records", side_effect=AssertionError("ablation must not screen")), \
             patch.object(CHECKS.structures, "analyze_cif", side_effect=AssertionError("ablation must not inspect")):
            result = layer.check_candidates(plan["plan_id"], [r["candidate_id"] for r in generated["records"]])
        self.assertFalse(result["checks_performed"])
        self.assertIsNone(result["valid_candidate_count"])
        self.assertNotIn("all_hard_requirements_pass", result["records"][0])
        raw = layer.invoke("read_artifact", {"artifact_id": "c_c.cif"})
        self.assertIn("SYNTHETIC WORKFLOW FIXTURE", raw["content"])
        self.assertEqual(raw["sha256"], replay.invoke("read_artifact", {"artifact_id": "c_c.cif"})["sha256"])

    def test_mapping_ablation_retains_actual_result_checking(self):
        replay = self.replay()
        layer = self.layer(replay, requirement_mapping=False)
        self.sample(replay)
        plan = layer.prepare_plan(self.request())["plan"]
        result = layer.check_candidates(plan["plan_id"], ["c_c"])
        self.assertTrue(result["checks_performed"])
        self.assertEqual(result["records"][0]["requirement_checks"][0]["status"], "fail")

    def test_same_formula_candidates_use_real_bound_metric_identity(self):
        replay = self.replay("development_candidate_evidence_0")
        layer = self.layer(replay)
        request = {"raw_request": "带隙至少1.2 eV", "num_samples": 1,
                   "conditions": {"dft_band_gap": {"value": 1.2, "unit": "eV"}},
                   "requirements": [{"requirement_id": "gap", "source": {"quote": "至少1.2 eV"},
                                     "kind": "numeric_bound", "property": "dft_band_gap", "value": 1.2,
                                     "unit": "eV", "strength": "hard", "relation": "at_least"}]}
        plan = layer.prepare_plan(request)["plan"]
        self.assertEqual(plan["state"], "blocked")  # It still cannot promise a generation guarantee.
        with patch.object(CHECKS.evidence, "validate_metric", wraps=CHECKS.evidence.validate_metric) as validate:
            result = layer.check_candidates(plan["plan_id"], ["c_a", "c_b"])
            self.assertGreaterEqual(validate.call_count, 2)
        rows = {r["candidate_id"]: r for r in result["records"]}
        self.assertEqual(rows["c_a"]["formula"], rows["c_b"]["formula"])
        self.assertNotEqual(rows["c_a"]["structure_version_id"], rows["c_b"]["structure_version_id"])
        # The fixture's generic band_gap is not silently relabeled as dft_band_gap.
        self.assertEqual(rows["c_a"]["requirement_checks"][0]["status"], "unknown")
        self.assertEqual(rows["c_b"]["requirement_checks"][0]["status"], "unknown")
        comparison = layer.compare_candidate_metrics(["c_a", "c_b"], "band_gap")
        self.assertTrue(comparison["comparable"])
        self.assertEqual({m["candidate_id"]: m["value"] for m in comparison["metrics"]}, {"c_a": .75, "c_b": 1.25})
        with self.assertRaises(ReplayError):
            layer.check_candidates(plan["plan_id"], ["NaCl"])

    def test_real_metric_comparability_and_ablation_removes_comparison(self):
        replay = self.replay("development_candidate_evidence_1")
        layer = self.layer(replay)
        result = layer.compare_candidate_metrics(["c_a", "c_b"], "energy")
        self.assertFalse(result["comparable"])
        self.assertTrue(any("differ" in reason for reason in result["reasons"]))
        ablated = self.layer(replay, evidence_checks=False)
        with patch.object(CHECKS.evidence, "compare_metrics", side_effect=AssertionError("no comparison in ablation")):
            result = ablated.compare_candidate_metrics(["c_a", "c_b"], "energy")
        self.assertIsNone(result["comparable"])
        self.assertEqual(len(result["metrics"]), 2)

    def test_unknown_scientific_goal_remains_unknown(self):
        replay = self.replay("development_candidate_evidence_0")
        layer = self.layer(replay)
        plan = layer.prepare_plan({"conditions": {"chemical_system": "Na-Cl"}, "review_targets": ["催化活性"], "num_samples": 1})["plan"]
        result = layer.check_candidates(plan["plan_id"], ["c_b"])
        checks = result["records"][0]["requirement_checks"]
        goal = next(c for c in checks if c["strength"] == "goal")
        self.assertEqual(goal["status"], "unknown")
        self.assertFalse(result["records"][0]["research_goals_verified"])

    def test_registered_templates_use_same_real_checks_in_mapping_ablation(self):
        replay = self.replay("development_candidate_evidence_0")
        record = replay.invoke("get_candidates", {"candidate_ids": ["c_a"]})["records"][0]
        settings = Settings(data_dir=self.root / "templates", templates={"cubic_nacl": {
            "path": record["path"], "sha256": record["sha256"], "source": "synthetic CPU test fixture",
            "review": {"by": "unit test only", "date": "test", "scope": "synthetic schema test"}, "demo_only": True}})
        request = {"model_id": "diffcsp", "conditions": {"chemical_formula": "NaCl", "space_group": 221},
                   "template_id": "cubic_nacl", "num_samples": 2}
        for mapping in (True, False):
            with self.subTest(mapping=mapping):
                layer = CHECKS.MatPilotChecks(settings, replay, requirement_mapping=mapping)
                result = layer.prepare_plan(request)["plan"]
                self.assertEqual(result["state"], "ready", result["issues"])
                self.assertEqual(result["template"]["space_group"], 221)
                self.assertEqual(result["template"]["sha256"], record["sha256"])
        Path(record["path"]).write_text("changed", encoding="utf-8")
        result = layer.prepare_plan(request)["plan"]
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(any(i["code"] == "invalid_template" for i in result["issues"]))

    def test_trace_artifact_tampering_and_real_execution_are_blocked(self):
        replay = self.replay("development_candidate_evidence_0")
        layer = self.layer(replay)
        plan = layer.prepare_plan({"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 1})["plan"]
        record = replay.invoke("get_candidates", {"candidate_ids": ["c_a"]})["records"][0]
        Path(record["path"]).write_text("changed", encoding="utf-8")
        with self.assertRaises(ReplayError):
            layer.check_candidates(plan["plan_id"], ["c_a"])
        for operation in ("submit_job", "control_job", "sample", "launch_worker"):
            with self.assertRaisesRegex(ValueError, "no submit"):
                layer.invoke(operation, {})
        with self.assertRaisesRegex(ValueError, "disabled"):
            CHECKS.MatPilotChecks(Settings(data_dir=self.root / "unsafe", real_compute_enabled=True), replay)
        with self.assertRaises(ValueError):
            self.layer(replay, requirement_mapping=False, evidence_checks=False)


if __name__ == "__main__":
    unittest.main()
