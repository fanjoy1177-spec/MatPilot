"""Synthetic offline tests only; scores/attestations here are not study evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from matpilot.evaluation import (
    audit_suite, freeze_suite, validate_frozen, schedule, validate_plan,
    record_run, blind_packet, summarize, digest, configuration_identity,
    GROUPS, RUBRIC, FLAGS, ABLATIONS,
)

ROOT = Path(__file__).resolve().parents[1]


def rehash(value, field):
    value[field] = digest({k: v for k, v in value.items() if k != field})


def certify(config):
    """Synthetic receipt for testing; deliberately no actual driver claim."""
    config["driver_validated"] = True
    config["driver_validation"] = {
        "configuration_sha256": configuration_identity(config),
        "report_sha256": digest({"synthetic_report": configuration_identity(config)}),
    }


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.suite = json.loads((ROOT / "benchmarks/study/test_candidates.draft.json").read_text(encoding="utf-8"))
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.review = {"suite_sha256": digest(self.suite), "approved": True, "independent_of_implementation": True,
                       "reviewer": "UNIT TEST ONLY", "materials_qualification": "simulated metadata",
                       "reviewed_at": "test", "notes": "No real human review"}
        self.frozen = freeze_suite(self.suite, self.review, self.root / "frozen.json")
        common = {k: "synthetic-fixture" for k in ("host_version", "llm_provider", "llm_version", "reasoning_mode")}
        common.update({k: digest(k) for k in ("scientific_capability_hash", "replay_fixture_hash", "clarification_policy_hash",
                                             "instruction_hash", "driver_sha256", "baseline_patch_hash")})
        common.update(max_turns=12, max_output_tokens=2048, sampling_cap=32, max_requests=12,
                      timeout_ms=30000, guidance_scale=2.0, source_revision="synthetic-test")
        self.environment = {}
        for group in GROUPS:
            config = dict(common, requirement_mapping=group == "M", evidence_checks=group == "M")
            certify(config)
            self.environment[group] = config

    def tearDown(self):
        self.tmp.cleanup()

    def ablations(self):
        result = {}
        for group, (name, mapping, evidence) in ABLATIONS.items():
            config = copy.deepcopy(self.environment["M"])
            config.update(ablation=name, requirement_mapping=mapping, evidence_checks=evidence)
            certify(config)
            result[group] = config
        return result

    def outcome(self, row, **changes):
        result = {"status": "completed", "wall_seconds": 1, "human_active_seconds": None, "api_cny": .01,
                  "science_cny": 0, "science_seconds": 0, "failure": None, "configuration_hash": row["configuration_hash"]}
        result.update(changes)
        return result

    def results(self, plan):
        """In-memory records for arithmetic tests; never fake files/LLM execution."""
        records, grades = [], {}
        for row in plan["runs"]:
            trace_hash = digest({"synthetic_trace": row["run_id"]})
            record = {**row, "trace_sha256": trace_hash, "outcome": self.outcome(row),
                      "plan_sha256": plan["plan_sha256"], "correctness": "ungraded", "human_time_status": "not_measured"}
            rehash(record, "record_sha256")
            records.append(record)
            grade = {k: True for k in RUBRIC}
            grade.update({k: False for k in FLAGS})
            grade.update(executable_completed=True, reviewer="UNIT TEST ONLY", evidence_notes="Synthetic arithmetic, no model run",
                         trace_sha256=trace_hash)
            if row["group"] == "U+" and row["repeat"] == 1:
                grade["actions_correct"] = False
            grades[row["run_id"]] = grade
        return records, grades

    def test_suite_quotas_and_draft_status(self):
        audit = audit_suite(self.suite, formal=True)
        self.assertTrue(audit["valid"])
        self.assertEqual(audit["count"], 36)
        self.assertGreaterEqual(audit["cross_model_count"], 8)
        self.assertGreaterEqual(audit["initially_executable_count"], 18)
        self.assertEqual(audit["independent_semantic_review"], "pending")
        self.assertFalse(audit_suite({"tasks": [None]}, formal=True)["valid"])

    def test_freezing_requires_review_and_does_not_overwrite(self):
        with self.assertRaises(ValueError):
            freeze_suite(self.suite, {}, self.root / "unreviewed.json")
        with self.assertRaises(FileExistsError):
            freeze_suite(self.suite, self.review, self.root / "frozen.json")
        self.assertTrue(validate_frozen(self.frozen)["valid"])

    def test_cannot_bypass_review_by_constructing_frozen_object(self):
        forged = {"schema_version": 1, "suite": self.suite, "suite_sha256": digest(self.suite)}
        with self.assertRaisesRegex(ValueError, "reviewer"):
            schedule(forged, self.environment)
        self.frozen["review"]["independent_of_implementation"] = False
        with self.assertRaisesRegex(ValueError, "reviewer"):
            schedule(self.frozen, self.environment)

    def test_changed_frozen_content_and_changed_review_rejected(self):
        plan = schedule(self.frozen, self.environment)
        self.frozen["review"]["reviewer"] = "different attestation"
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_plan(self.frozen, plan)
        self.frozen["suite"]["tasks"][0]["prompt"] = "changed"
        with self.assertRaisesRegex(ValueError, "changed"):
            schedule(self.frozen, self.environment)

    def test_schedule_has_full_coverage_and_no_external_mutation(self):
        plan = schedule(self.frozen, self.environment)
        self.assertEqual(validate_plan(self.frozen, plan)["planned_runs"], 324)
        self.assertEqual(len({r["run_id"] for r in plan["runs"]}), 324)
        for task in self.suite["tasks"]:
            for group in GROUPS:
                self.assertEqual({r["repeat"] for r in plan["runs"] if r["task_id"] == task["id"] and r["group"] == group}, {1, 2, 3})
        self.environment["M"]["llm_version"] = "mutated caller input"
        self.assertTrue(validate_plan(self.frozen, plan)["valid"])

    def test_shared_budgets_and_science_conditions_must_match(self):
        for key, value in (("llm_version", "other"), ("max_requests", 9), ("timeout_ms", 35000), ("guidance_scale", 3.0)):
            with self.subTest(key=key):
                environment = copy.deepcopy(self.environment)
                environment["M"][key] = value
                certify(environment["M"])
                with self.assertRaisesRegex(ValueError, "Unfair"):
                    schedule(self.frozen, environment)

    def test_validation_receipt_binds_exact_config_and_baseline_patch(self):
        self.environment["M"]["instruction_hash"] = digest("changed instructions")
        with self.assertRaisesRegex(ValueError, "receipt"):
            schedule(self.frozen, self.environment)
        certify(self.environment["M"])
        self.environment["M"]["baseline_patch_hash"] = digest("different fixes")
        certify(self.environment["M"])
        with self.assertRaisesRegex(ValueError, "baseline"):
            schedule(self.frozen, self.environment)

    def test_plan_deleted_repeat_is_rejected_even_with_recomputed_hash(self):
        plan = schedule(self.frozen, self.environment)
        plan["runs"] = [row for row in plan["runs"] if row["repeat"] != 3]
        with self.assertRaisesRegex(ValueError, "hash"):
            validate_plan(self.frozen, plan)
        rehash(plan, "plan_sha256")
        with self.assertRaisesRegex(ValueError, "coverage"):
            summarize(self.frozen, plan, [], {})

    def test_plan_duplicate_wrong_repeat_and_order_are_rejected(self):
        for mutation in ("duplicate", "repeat", "order", "bool_repeat"):
            with self.subTest(mutation=mutation):
                plan = schedule(self.frozen, self.environment)
                if mutation == "duplicate":
                    plan["runs"][1] = copy.deepcopy(plan["runs"][0])
                elif mutation == "repeat":
                    plan["runs"][0]["repeat"] = 4
                elif mutation == "bool_repeat":
                    plan["runs"][0]["repeat"] = True
                else:
                    plan["runs"][0], plan["runs"][1] = plan["runs"][1], plan["runs"][0]
                rehash(plan, "plan_sha256")
                with self.assertRaises(ValueError):
                    validate_plan(self.frozen, plan)

    def test_ablations_require_real_distinct_feature_configurations(self):
        ids = [self.suite["tasks"][0]["id"]]
        with self.assertRaisesRegex(ValueError, "separately"):
            schedule(self.frozen, self.environment, ablation_ids=ids)
        configs = self.ablations()
        configs["M_without_mapping"] = copy.deepcopy(self.environment["M"])
        with self.assertRaisesRegex(ValueError, "switches"):
            schedule(self.frozen, self.environment, ablation_ids=ids, ablation_environment=configs)
        configs = self.ablations()
        configs["M_without_mapping"]["driver_validation"] = copy.deepcopy(self.environment["M"]["driver_validation"])
        with self.assertRaisesRegex(ValueError, "receipt"):
            schedule(self.frozen, self.environment, ablation_ids=ids, ablation_environment=configs)
        plan = schedule(self.frozen, self.environment, ablation_ids=ids, ablation_environment=self.ablations())
        self.assertEqual(validate_plan(self.frozen, plan)["ablation_runs"], 6)
        self.assertEqual(len({r["configuration_hash"] for r in plan["runs"] if r["group"].startswith("M")}), 3)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            schedule(self.frozen, self.environment, ablation_ids=ids * 2, ablation_environment=self.ablations())

    def test_record_keeps_failures_immutable_and_redacts_blind_copy(self):
        plan = schedule(self.frozen, self.environment)
        row = plan["runs"][0]
        trace = self.root / "trace.json"
        trace.write_text(json.dumps({"group": row["group"], "nested": {"configuration_hash": "hidden", "error": "injected timeout"}}), encoding="utf-8")
        outcome = self.outcome(row, status="timeout", wall_seconds=2, failure="test timeout")
        record = record_run(self.root / "runs", row, outcome, trace, frozen=self.frozen, plan=plan)
        self.assertEqual(record["outcome"]["api_cny"], .01)
        self.assertEqual(record["human_time_status"], "not_measured")
        packet = blind_packet(self.root / "runs", row["run_id"], "blind-1")
        self.assertNotIn("group", packet["trace"])
        self.assertEqual(packet["trace"]["nested"], {"error": "injected timeout"})
        self.assertEqual(packet["trace_sha256"], record["trace_sha256"])
        with self.assertRaises(FileExistsError):
            record_run(self.root / "runs", row, outcome, trace, frozen=self.frozen, plan=plan)
        (self.root / "runs" / row["run_id"] / "trace.json").write_text('{}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            blind_packet(self.root / "runs", row["run_id"], "blind-1")

    def test_unbound_legacy_record_cannot_be_used_for_formal_summary(self):
        plan = schedule(self.frozen, self.environment)
        row = plan["runs"][0]
        trace = self.root / "trace.json"
        trace.write_text('{}', encoding="utf-8")
        record = record_run(self.root / "runs", row, self.outcome(row), trace)
        with self.assertRaisesRegex(ValueError, "plan identity"):
            summarize(self.frozen, plan, [record], {})

    def test_missing_runs_and_ungraded_records_never_show_complete_or_zero_errors(self):
        plan = schedule(self.frozen, self.environment)
        report = summarize(self.frozen, plan, [], {}, bootstrap_samples=100)
        self.assertEqual(len(report["missing_runs"]), 324)
        self.assertEqual(report["status"], "incomplete_do_not_interpret_effectiveness")
        self.assertIsNone(report["paired_correctness"])
        self.assertEqual(report["groups"]["M"]["planned_runs"], 108)
        self.assertIsNone(report["groups"]["M"]["complete_task_correct_rate"])
        self.assertIsNone(report["groups"]["M"]["wrong_start_rate"])
        records, grades = self.results(plan)
        grades.pop(records[0]["run_id"])
        report = summarize(self.frozen, plan, records, grades, bootstrap_samples=100)
        self.assertEqual(report["ungraded_runs"], [records[0]["run_id"]])
        self.assertIsNone(report["paired_correctness"])
        self.assertTrue(all(value is None for value in report["efficiency"].values()))

    def test_task_clustered_analysis_no_invented_human_time_and_ablation_pairs(self):
        ids = [task["id"] for task in self.suite["tasks"][:2]]
        plan = schedule(self.frozen, self.environment, ablation_ids=ids, ablation_environment=self.ablations())
        records, grades = self.results(plan)
        report = summarize(self.frozen, plan, records, grades, bootstrap_samples=100)
        self.assertEqual(report["status"], "complete_scored")
        self.assertEqual(report["paired_correctness"]["tasks"], 36)
        self.assertEqual(report["paired_executable_completion"]["tasks"], 18)
        self.assertAlmostEqual(report["paired_correctness"]["difference_M_minus_Uplus"], 1 / 3)
        self.assertIsNone(report["groups"]["M"]["human_active_seconds_per_correct_task"])
        self.assertIsNone(report["efficiency"]["human_active_seconds_per_correct_task_reduction_M_vs_Uplus"])
        self.assertEqual(report["paired_ablations"]["M_without_mapping"]["tasks"], 2)

    def test_failed_run_cost_is_retained_in_correct_task_cost(self):
        plan = schedule(self.frozen, self.environment)
        records, grades = self.results(plan)
        record = next(r for r in records if r["group"] == "M")
        record["outcome"].update(status="timeout", failure="injected timeout", api_cny=5)
        rehash(record, "record_sha256")
        grades[record["run_id"]]["executable_completed"] = False
        report = summarize(self.frozen, plan, records, grades, bootstrap_samples=100)
        m = report["groups"]["M"]
        self.assertEqual(m["correct_runs"], 107)
        self.assertAlmostEqual(m["api_cny_observed_total"], 6.07)
        self.assertAlmostEqual(m["api_cny_per_correct_task"], 6.07 / 107)

    def test_mismatched_record_task_repeat_config_or_outcome_is_rejected(self):
        plan = schedule(self.frozen, self.environment)
        for field, value in (("task_id", "OTHER"), ("group", "M_without_mapping"), ("repeat", 2),
                             ("configuration_hash", digest("other config")), ("plan_sha256", digest("other plan"))):
            with self.subTest(field=field):
                records, grades = self.results(plan)
                record = records[0]
                record[field] = value
                if field == "configuration_hash":
                    record["outcome"][field] = value
                rehash(record, "record_sha256")
                with self.assertRaises(ValueError):
                    summarize(self.frozen, plan, records, grades)
        records, grades = self.results(plan)
        records[0]["outcome"]["configuration_hash"] = digest("other runtime config")
        rehash(records[0], "record_sha256")
        with self.assertRaisesRegex(ValueError, "Runtime"):
            summarize(self.frozen, plan, records, grades)

    def test_mutated_record_invalid_cost_duplicate_or_orphan_grade_rejected(self):
        plan = schedule(self.frozen, self.environment)
        records, grades = self.results(plan)
        records[0]["outcome"]["api_cny"] = 0
        with self.assertRaisesRegex(ValueError, "hash"):
            summarize(self.frozen, plan, records, grades)
        for bad in (-1, float("nan"), float("inf"), True):
            with self.subTest(cost=bad):
                records, grades = self.results(plan)
                records[0]["outcome"]["api_cny"] = bad
                with self.assertRaises(ValueError):
                    summarize(self.frozen, plan, records, grades)
        records, grades = self.results(plan)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            summarize(self.frozen, plan, records + [records[0]], grades)
        with self.assertRaisesRegex(ValueError, "orphaned"):
            summarize(self.frozen, plan, records[1:], grades)

    def test_trace_binding_contradictory_or_partial_grades_rejected(self):
        plan = schedule(self.frozen, self.environment)
        for change in ({"trace_sha256": digest("other trace")}, {"unsupported_conclusion": True}, {"wrong_start": None}):
            with self.subTest(change=change):
                records, grades = self.results(plan)
                grades[records[0]["run_id"]].update(change)
                with self.assertRaises(ValueError):
                    summarize(self.frozen, plan, records, grades)
        with self.assertRaisesRegex(ValueError, "Bootstrap"):
            summarize(self.frozen, plan, [], {}, bootstrap_samples=0)


if __name__ == "__main__":
    unittest.main()
