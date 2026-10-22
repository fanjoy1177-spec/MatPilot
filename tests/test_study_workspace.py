"""All people, validation reports and scores here are synthetic test fixtures."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from matpilot.evaluation import ABLATIONS, GROUPS, RUBRIC, FLAGS, configuration_identity, digest, schedule
from matpilot.study_workspace import (collect_grades, create_blind_review, export_runtime, export_development,
                                     development_selection, file_hash, import_run, prepare_review)

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.suite = json.loads((ROOT / "benchmarks/study/test_candidates.draft.json").read_text(encoding="utf-8"))
        self.review = {"suite_sha256": digest(self.suite), "approved": True, "independent_of_implementation": True,
                       "reviewer": "UNIT TEST ONLY", "materials_qualification": "synthetic", "reviewed_at": "test", "notes": "Not a real review"}
        self.frozen = {"schema_version": 1, "suite": self.suite, "suite_sha256": digest(self.suite), "review": self.review}
        fixtures = {t["fixture_id"]: {} for t in self.suite["tasks"]}
        self.write("fixtures.json", {"purpose": "formal_workflow_study", "fixtures": fixtures})
        self.write("fixture-review.json", {**self.review, "manifest_sha256": file_hash(self.root / "fixtures.json"), "fixture_ids": list(fixtures)})
        self.write("capability.json", {"scope": "UNIT TEST ONLY"})
        self.environment, drivers = {}, {}
        for group in GROUPS:
            prefix = group.replace("+", "plus")
            self.write(prefix + ".mjs", "// Test-only module for " + group)
            self.write(prefix + ".txt", "Test-only instructions for " + group)
            config = {"host_version": "test", "llm_provider": "test", "llm_version": "test", "reasoning_mode": "test",
                      "source_revision": "test-source", "scientific_capability_hash": file_hash(self.root / "capability.json"),
                      "replay_fixture_hash": file_hash(self.root / "fixtures.json"), "clarification_policy_hash": "c" * 64,
                      "max_turns": 12, "max_output_tokens": 2048, "max_requests": 12, "timeout_ms": 30000,
                      "sampling_cap": 32, "guidance_scale": 2.0, "driver_validated": True,
                      "driver_sha256": file_hash(self.root / (prefix + ".mjs")), "instruction_hash": file_hash(self.root / (prefix + ".txt")),
                      "baseline_patch_hash": None if group == "U" else "b" * 64,
                      "requirement_mapping": group == "M", "evidence_checks": group == "M", "driver_options_hash": digest({})}
            self.write(prefix + ".source.json", {"source_revision": "test-source", "files": [
                {"path": prefix + ext, "sha256": file_hash(self.root / (prefix + ext))} for ext in (".mjs", ".txt")]})
            config["source_manifest_sha256"] = file_hash(self.root / (prefix + ".source.json"))
            report = {"configuration_sha256": configuration_identity(config), "passed": True,
                      "checks": ["synthetic check; UNIT TEST ONLY, not an actual Harness validation"], "evidence_scope": "harness_replay_end_to_end"}
            self.write(prefix + ".report.json", report)
            config["driver_validation"] = {"configuration_sha256": configuration_identity(config),
                                           "report_sha256": file_hash(self.root / (prefix + ".report.json"))}
            self.environment[group] = config
            self.write(prefix + ".source.json", {"source_revision": "test-source", "files": [
                {"path": prefix + ext, "sha256": file_hash(self.root / (prefix + ext))} for ext in (".mjs", ".txt")]})
            drivers[group] = {"module_path": prefix + ".mjs", "instruction_path": prefix + ".txt", "capability_path": "capability.json",
                              "validation_report_path": prefix + ".report.json", "source_manifest_path": prefix + ".source.json",
                              "source_manifest_sha256": file_hash(self.root / (prefix + ".source.json"))}
        self.plan = schedule(self.frozen, self.environment)
        self.inventory = {"drivers": drivers, "fixture_manifest_path": "fixtures.json", "fixture_review_path": "fixture-review.json",
                          "fixture_review_sha256": file_hash(self.root / "fixture-review.json")}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        (self.root / name).write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def export(self):
        return export_runtime(self.frozen, self.plan, self.inventory, self.root / "pack.json", inventory_base=self.root)

    def imported(self):
        row = self.plan["runs"][0]
        trace = {"format": "matpilot-study-trace/v1", "run_identity": {k: row[k] for k in ("run_id", "task_id", "group", "repeat", "configuration_hash")},
                 "provenance": {"plan_sha256": self.plan["plan_sha256"], "suite_sha256": self.frozen["suite_sha256"], "module_sha256": self.environment[row["group"]]["driver_sha256"], "instruction_sha256": self.environment[row["group"]]["instruction_hash"], "source_manifest_sha256": self.environment[row["group"]]["source_manifest_sha256"]},
                 "events": [{"type": "assistant/message", "data": {"text": "Test-only response"}}, {"type": "turn/end", "data": {"reason": {"kind": "aborted"}}}],
                 "terminal": {"status": "timeout", "failure": "Synthetic timeout", "turn_end_reason": {"kind": "aborted"}}}
        self.write("trace.json", trace)
        outcome = {"status": "timeout", "failure": "Synthetic timeout", "turn_end_reason": {"kind": "aborted"}, "wall_seconds": 2,
                   "human_active_seconds": None, "api_cny": .01, "science_cny": 0, "science_seconds": 0, "configuration_hash": row["configuration_hash"]}
        record = import_run(self.frozen, self.plan, self.root / "runs", row, outcome, self.root / "trace.json")
        return row, outcome, trace, record

    def test_review_packet_is_unsigned_and_content_bound(self):
        prepare_review(self.suite, self.root / "review", self.root / "fixtures.json")
        r = json.loads((self.root / "review/review.to-complete.json").read_text(encoding="utf-8"))
        self.assertFalse(r["approved"])
        self.assertFalse(r["independent_of_implementation"])
        self.assertEqual(r["reviewer"], "")
        self.assertEqual(r["suite_sha256"], digest(self.suite))
        with self.assertRaises(FileExistsError):
            prepare_review(self.suite, self.root / "review")

    def test_export_drops_all_answers_and_preserves_every_run(self):
        p = self.export()
        self.assertEqual(len(p["rows"]), 324)
        for task in p["tasks"]:
            self.assertEqual(set(task), {"id", "prompt", "fixture_id", "fixed_clarifications"})
        text = json.dumps(p)
        self.assertNotIn('"gold"', text)
        self.assertNotIn('"initially_executable"', text)
        self.assertFalse(p["real_compute_allowed"])
        self.assertTrue(Path(p["drivers"]["M"]["module_path"]).is_absolute())

    def test_export_rejects_changed_driver_file(self):
        self.write("M.mjs", "// Tampered")
        with self.assertRaisesRegex(ValueError, "hash"):
            self.export()

    def test_export_rejects_modified_feature_switch(self):
        self.inventory["drivers"]["M"]["options"] = {"evidence_checks": False}
        with self.assertRaisesRegex(ValueError, "driver options"):
            self.export()

    def test_export_rejects_unsigned_task_set(self):
        self.frozen["review"]["approved"] = False
        with self.assertRaises(ValueError):
            self.export()

    def test_export_rejects_unsigned_fixture_review_even_when_hash_is_updated(self):
        r = json.loads((self.root / "fixture-review.json").read_text())
        r["approved"] = False
        self.write("fixture-review.json", r)
        self.inventory["fixture_review_sha256"] = file_hash(self.root / "fixture-review.json")
        with self.assertRaisesRegex(ValueError, "fixture review"):
            self.export()

    def test_development_export_does_not_forge_review_or_expose_answers(self):
        suite = json.loads((ROOT / "benchmarks/study/development_tasks.json").read_text(encoding="utf-8"))
        (self.root / "fixtures.json").write_bytes((ROOT / "benchmarks/study/fixtures.development.json").read_bytes())
        for group, c in self.environment.items():
            c["replay_fixture_hash"] = file_hash(self.root / "fixtures.json")
            report_file = self.inventory["drivers"][group]["validation_report_path"]
            report = json.loads((self.root / report_file).read_text())
            report["configuration_sha256"] = configuration_identity(c)
            self.write(report_file, report)
            c["driver_validation"] = {"configuration_sha256": configuration_identity(c), "report_sha256": file_hash(self.root / report_file)}
        pack = export_development(suite, self.environment, self.inventory, self.root / "development.json", inventory_base=self.root)
        self.assertEqual(pack["mode"], "development")
        self.assertEqual(len(pack["rows"]), 36)
        self.assertNotIn("review_path", pack["replay"])
        self.assertNotIn('"gold"', json.dumps(pack))
        self.assertEqual(next(t for t in pack["tasks"] if t["id"] == "D0302")["fixed_clarifications"][0]["slot"], "生成方式与元素体系")

    def development_inputs(self, ablations=tuple(ABLATIONS)):
        suite = json.loads((ROOT / "benchmarks/study/development_tasks.json").read_text(encoding="utf-8"))
        (self.root / "fixtures.json").write_bytes((ROOT / "benchmarks/study/fixtures.development.json").read_bytes())
        for group in ablations:
            self.environment[group] = copy.deepcopy(self.environment["M"])
            self.inventory["drivers"][group] = copy.deepcopy(self.inventory["drivers"]["M"])
            ablation, mapping, evidence = ABLATIONS[group]
            self.environment[group].update(ablation=ablation, requirement_mapping=mapping, evidence_checks=evidence)
        for group, config in self.environment.items():
            config.setdefault("ablation", None)
            config["replay_fixture_hash"] = file_hash(self.root / "fixtures.json")
            options = {"implementation_group": group, **{k: config[k] for k in ("ablation", "requirement_mapping", "evidence_checks")}}
            self.inventory["drivers"][group]["options"] = options
            config["driver_options_hash"] = digest(options)
            self.refresh_development_receipt(group)
        return suite

    def refresh_development_receipt(self, group):
        config = self.environment[group]
        report_file = group.replace("+", "plus") + ".development-report.json"
        self.write(report_file, {"configuration_sha256": configuration_identity(config), "passed": True,
                                "checks": ["synthetic export fixture only"], "evidence_scope": "cpu_tool_contract_only"})
        self.inventory["drivers"][group]["validation_report_path"] = report_file
        config["driver_validation"] = {"configuration_sha256": configuration_identity(config),
                                       "report_sha256": file_hash(self.root / report_file)}

    def test_development_subset_retains_comparator_definitions_and_binds_selection(self):
        suite = self.development_inputs()
        ids, groups = ["D0201", "D0502"], ["M", *ABLATIONS]
        pack = export_development(suite, self.environment, self.inventory, self.root / "subset.json", inventory_base=self.root,
                                  task_ids=ids, groups=groups, repeats=2)
        self.assertEqual(set(pack["drivers"]), set(GROUPS) | set(ABLATIONS))
        self.assertEqual([t["id"] for t in pack["tasks"]], ids)
        self.assertEqual(len(pack["rows"]), 12)
        self.assertEqual({r["group"] for r in pack["rows"]}, set(groups))
        self.assertEqual(pack["provenance"]["selection_sha256"], digest(pack["selection"]))
        self.assertEqual(pack["provenance"]["suite_sha256"], digest(suite))
        self.assertEqual(pack["selection"]["available_task_count"], 12)
        self.assertEqual(len({pack["drivers"][g]["instruction_sha256"] for g in groups}), 1)
        self.assertNotIn('"gold"', json.dumps(pack))
        self.assertNotIn('"review_path"', json.dumps(pack))
        repeat = export_development(suite, self.environment, self.inventory, self.root / "again.json", inventory_base=self.root,
                                    task_ids=ids, groups=groups, repeats=2)
        self.assertEqual(pack, repeat)
        changed = export_development(suite, self.environment, self.inventory, self.root / "changed.json", inventory_base=self.root,
                                     task_ids=ids[:1], groups=groups, repeats=2)
        self.assertNotEqual(pack["provenance"]["plan_sha256"], changed["provenance"]["plan_sha256"])

    def test_single_development_ablation_is_valid_with_matched_control(self):
        suite = self.development_inputs(("M_without_mapping",))
        pack = export_development(suite, self.environment, self.inventory, self.root / "single.json", inventory_base=self.root,
                                  task_ids=["D0202"], groups=["M", "M_without_mapping"])
        self.assertEqual(len(pack["rows"]), 2)
        self.assertEqual(len(pack["drivers"]), 4)

    def test_development_selection_rejects_empty_duplicate_unknown_and_unpaired_values(self):
        suite = json.loads((ROOT / "benchmarks/study/development_tasks.json").read_text(encoding="utf-8"))
        for values in ([], ["D0101", "D0101"], ["T0101"], "D0101", [None]):
            with self.subTest(task_ids=values), self.assertRaisesRegex(ValueError, "task_ids"):
                development_selection(suite, values)
        for values in ([], ["M", "M"], ["unknown"], "M", ["M_without_evidence"]):
            with self.subTest(groups=values), self.assertRaises(ValueError):
                development_selection(suite, groups=values)
        suite["split"] = "formal"
        with self.assertRaisesRegex(ValueError, "development split"):
            development_selection(suite)

    def test_development_ablation_rejects_changed_instructions_options_and_false_flags(self):
        suite = self.development_inputs()
        group = "M_without_mapping"
        original_config = copy.deepcopy(self.environment[group])
        original_options = copy.deepcopy(self.inventory["drivers"][group]["options"])
        for key, value in (("instruction_hash", "f" * 64), ("source_manifest_sha256", "f" * 64),
                           ("max_requests", 13), ("requirement_mapping", True)):
            self.environment[group] = {**copy.deepcopy(original_config), key: value}
            self.refresh_development_receipt(group)
            with self.subTest(key=key), self.assertRaises(ValueError):
                export_development(suite, self.environment, self.inventory, self.root / "rejected.json", inventory_base=self.root,
                                   task_ids=["D0101"], groups=["M", *ABLATIONS])
        self.environment[group] = original_config
        for options in ({**original_options, "request_timeout_ms": 99999},
                        {**original_options, "implementation_group": "M"},
                        {k: v for k, v in original_options.items() if k != "requirement_mapping"}):
            self.inventory["drivers"][group]["options"] = options
            self.environment[group]["driver_options_hash"] = digest(options)
            self.refresh_development_receipt(group)
            with self.subTest(options=options), self.assertRaises(ValueError):
                export_development(suite, self.environment, self.inventory, self.root / "rejected.json", inventory_base=self.root,
                                   task_ids=["D0101"], groups=["M", *ABLATIONS])
        self.assertFalse((self.root / "rejected.json").exists())

    def test_completed_label_cannot_override_failed_event(self):
        row, outcome, trace, _ = self.imported()
        outcome.update(status="completed", failure=None)
        trace["terminal"].update(status="completed", failure=None)
        self.write("trace.json", trace)
        with self.assertRaisesRegex(ValueError, "actual completed"):
            import_run(self.frozen, self.plan, self.root / "other-runs", row, outcome, self.root / "trace.json")

    def test_import_rejects_altered_driver_provenance(self):
        row, outcome, trace, _ = self.imported()
        trace["provenance"]["module_sha256"] = "a" * 64
        self.write("trace.json", trace)
        with self.assertRaisesRegex(ValueError, "driver provenance"):
            import_run(self.frozen, self.plan, self.root / "other-runs", row, outcome, self.root / "trace.json")

    def test_import_keeps_failed_cost_and_unknown_human_time_and_refuses_overwrite(self):
        row, outcome, trace, record = self.imported()
        self.assertIsNone(record["outcome"]["human_active_seconds"])
        self.assertEqual(record["outcome"]["api_cny"], .01)
        self.assertEqual(record["plan_sha256"], self.plan["plan_sha256"])
        with self.assertRaises(FileExistsError):
            import_run(self.frozen, self.plan, self.root / "runs", row, outcome, self.root / "trace.json")

    def test_import_rejects_wrong_identity_and_changed_terminal(self):
        row, outcome, trace, record = self.imported()
        trace["run_identity"]["group"] = "wrong"
        self.write("trace.json", trace)
        with self.assertRaisesRegex(ValueError, "another run"):
            import_run(self.frozen, self.plan, self.root / "other-runs", row, outcome, self.root / "trace.json")
        trace["run_identity"]["group"] = row["group"]
        trace["terminal"]["status"] = "completed"
        self.write("trace.json", trace)
        with self.assertRaisesRegex(ValueError, "Terminal"):
            import_run(self.frozen, self.plan, self.root / "other-runs", row, outcome, self.root / "trace.json")

    def test_blind_review_binds_scores_to_unchanged_copy_and_original(self):
        row, outcome, trace, record = self.imported()
        status = create_blind_review(self.frozen, self.plan, self.root / "runs", self.root / "blind")
        self.assertEqual(status["missing_runs"], 323)
        packet = json.loads((self.root / "blind/reviewer/B00001.json").read_text(encoding="utf-8"))
        mapping = json.loads((self.root / "blind/PRIVATE.mapping.json").read_text(encoding="utf-8"))
        self.assertNotIn("provenance", packet["trace"])
        self.assertNotIn("run_identity", packet["trace"])
        packet["grade"] = {k: False for k in (*RUBRIC, *FLAGS)}
        packet["reviewer"] = "UNIT TEST ONLY"
        packet["evidence_notes"] = "Synthetic grading test, no empirical finding"
        grades = collect_grades(mapping, [packet])
        self.assertEqual(grades[row["run_id"]]["trace_sha256"], record["trace_sha256"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            collect_grades(mapping, [packet, packet])
        packet["task"]["prompt"] = "Changed task"
        with self.assertRaisesRegex(ValueError, "frozen packet"):
            collect_grades(mapping, [packet])

    def test_review_copy_omits_runtime_headers_but_keeps_visible_evidence(self):
        from matpilot.study_workspace import _review_trace
        row = self.plan["runs"][0]
        trace = {"events": [{"type": "request/header", "data": {"model": "private config"}},
                            {"type": "system/message", "data": {"content": "group instructions"}},
                            {"type": "assistant/message", "data": {"text": "kept evidence"}}],
                 "tools": [{"name": "read_artifact", "arguments": {"path": "C:/secret/run/candidate.cif", "sessionId": "hidden"},
                            "result": {"path": "C:/secret/run/candidate.cif", "candidate_id": "c_a", "value": 1.2}}],
                 "driver_events": [{"group": "M"}], "usage_record_ids": ["billing-id"]}
        clean = _review_trace(trace, row)
        serialized = json.dumps(clean)
        self.assertNotIn("private config", serialized)
        self.assertNotIn("group instructions", serialized)
        self.assertNotIn("C:/secret", serialized)
        self.assertNotIn("sessionId", serialized)
        self.assertIn("kept evidence", serialized)
        self.assertEqual(clean["tools"][0]["arguments"]["path"], clean["tools"][0]["result"]["path"])
        self.assertEqual(clean["tools"][0]["result"]["candidate_id"], "c_a")


if __name__ == "__main__":
    unittest.main()
