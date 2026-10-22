import copy
import json
from pathlib import Path
import tempfile
import unittest
from matpilot.evaluation import (audit_suite, freeze_suite, schedule, record_run, blind_packet, summarize, digest, GROUPS, RUBRIC, FLAGS)

ROOT = Path(__file__).resolve().parents[1]


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.suite = json.loads((ROOT / "benchmarks/study/test_candidates.draft.json").read_text(encoding="utf-8"))
        self.frozen = {"suite": self.suite, "suite_sha256": digest(self.suite)}
        common = {k: "test-fixture" for k in ("host_version", "llm_provider", "llm_version", "reasoning_mode", "scientific_capability_hash", "replay_fixture_hash", "clarification_policy_hash")}
        common.update(max_turns=12, max_output_tokens=2048, sampling_cap=32, driver_validated=True, source_revision="test", instruction_hash="fixture", baseline_patch_hash="same-fixture")
        self.environment = {g: dict(common) for g in GROUPS}
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_suite_quotas_and_draft_status(self):
        audit = audit_suite(self.suite, formal=True)
        self.assertTrue(audit["valid"])
        self.assertEqual(audit["count"], 36)
        self.assertGreaterEqual(audit["cross_model_count"], 8)
        self.assertGreaterEqual(audit["initially_executable_count"], 18)
        self.assertEqual(audit["independent_semantic_review"], "pending")

    def test_cannot_freeze_without_real_review_attestation(self):
        with self.assertRaises(ValueError):
            freeze_suite(self.suite, {}, self.root / "frozen.json")
        review = {"suite_sha256": digest(self.suite), "approved": True, "independent_of_implementation": True,
                  "reviewer": "UNIT TEST ONLY", "materials_qualification": "simulated metadata", "reviewed_at": "test", "notes": "No real human review"}
        freeze_suite(self.suite, review, self.root / "frozen.json")
        with self.assertRaises(FileExistsError):
            freeze_suite(self.suite, review, self.root / "frozen.json")

    def test_schedule_has_324_runs_and_rejects_unfair_configs(self):
        plan = schedule(self.frozen, self.environment)
        self.assertEqual(len(plan["runs"]), 324)
        self.assertEqual(len({r["run_id"] for r in plan["runs"]}), 324)
        self.environment["M"]["llm_version"] = "different"
        with self.assertRaisesRegex(ValueError, "Unfair"):
            schedule(self.frozen, self.environment)

    def test_schedule_requires_baseline_fix_parity(self):
        self.environment["M"]["baseline_patch_hash"] = "different"
        with self.assertRaises(ValueError):
            schedule(self.frozen, self.environment)

    def test_changed_frozen_data_rejected(self):
        self.frozen["suite"]["tasks"][0]["prompt"] = "changed"
        with self.assertRaisesRegex(ValueError, "changed"):
            schedule(self.frozen, self.environment)

    def test_record_keeps_failures_and_does_not_overwrite(self):
        row = schedule(self.frozen, self.environment)["runs"][0]
        trace = self.root / "trace.json"
        trace.write_text('{"error":"injected timeout"}')
        outcome = {"status": "timeout", "wall_seconds": 2, "human_active_seconds": None, "api_cny": .01,
                   "science_cny": 0, "science_seconds": 0, "failure": "test timeout", "configuration_hash": row["configuration_hash"]}
        record_run(self.root / "runs", row, outcome, trace)
        packet = blind_packet(self.root / "runs", row["run_id"], "blind-1")
        self.assertNotIn("group", packet)
        self.assertEqual(packet["trace"]["error"], "injected timeout")
        with self.assertRaises(FileExistsError):
            record_run(self.root / "runs", row, outcome, trace)
        (self.root / "runs" / row["run_id"] / "trace.json").write_text('{}')
        with self.assertRaisesRegex(ValueError, "changed"):
            blind_packet(self.root / "runs", row["run_id"], "blind-1")

    def test_missing_runs_not_dropped_and_no_effectiveness_claim(self):
        plan = schedule(self.frozen, self.environment)
        report = summarize(self.frozen, plan, [], {}, bootstrap_samples=100)
        self.assertEqual(len(report["missing_runs"]), 324)
        self.assertIsNone(report["paired_correctness"])
        self.assertEqual(report["groups"]["M"]["planned_runs"], 108)
        self.assertIsNone(report["groups"]["M"]["human_active_seconds_per_correct_task"])

    def test_task_clustered_analysis_and_no_invented_human_time(self):
        plan = schedule(self.frozen, self.environment)
        records, grades = [], {}
        for row in plan["runs"]:
            records.append({**row, "trace_sha256": "fixture", "outcome": {"status": "completed", "human_active_seconds": None, "api_cny": .01, "science_cny": 0, "science_seconds": 0, "wall_seconds": 1}})
            grade = {k: True for k in RUBRIC}
            grade.update({k: False for k in FLAGS})
            grade.update(executable_completed=True, reviewer="UNIT TEST ONLY", evidence_notes="Synthetic statistics test, no model run", trace_sha256="fixture")
            if row["group"] == "U+" and row["repeat"] == 1:
                grade["actions_correct"] = False
            grades[row["run_id"]] = grade
        report = summarize(self.frozen, plan, records, grades, bootstrap_samples=100)
        self.assertEqual(report["paired_correctness"]["tasks"], 36)
        self.assertAlmostEqual(report["paired_correctness"]["difference_M_minus_Uplus"], 1/3)
        self.assertIsNone(report["groups"]["M"]["human_active_seconds_per_correct_task"])
        grades[plan["runs"][0]["run_id"]]["trace_sha256"] = "wrong"
        with self.assertRaisesRegex(ValueError, "trace"):
            summarize(self.frozen, plan, records, grades, bootstrap_samples=100)


if __name__ == "__main__":
    unittest.main()
