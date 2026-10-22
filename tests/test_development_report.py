"""Synthetic CPU-only records; none of these fixtures are actual study results."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from matpilot.development_report import Sanitizer, build_development_report
from matpilot.evaluation import digest
from matpilot.study_workspace import file_hash

ROOT = Path(__file__).resolve().parents[1]


class DevelopmentReportTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".work/test-temp"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.runner = self.root / "runner"
        self.runner.mkdir()
        self.write("capability.json", {"scope": "SYNTHETIC TEST ONLY"})
        self.write("fixtures.json", {"purpose": "development", "fixtures": {"synthetic": {}}})
        shared = {"scientific_capability_hash": file_hash(self.root / "capability.json"),
                  "replay_fixture_hash": file_hash(self.root / "fixtures.json"), "clarification_policy_hash": "c" * 64}
        drivers = {}
        for group in ("U", "U+", "M"):
            prefix = group.replace("+", "plus")
            self.write(prefix + ".mjs", "// Synthetic driver, never executed")
            self.write(prefix + ".txt", "Synthetic instructions, never sent to an API")
            self.write(prefix + ".source.json", {"source_revision": "synthetic", "files": [
                {"path": prefix + ext, "sha256": file_hash(self.root / (prefix + ext))} for ext in (".mjs", ".txt")]})
            self.write(prefix + ".receipt.json", {"passed": True, "configuration_sha256": digest(group),
                       "checks": ["SYNTHETIC TEST ONLY"]})
            drivers[group] = {**shared, "configuration_hash": digest(group), "source_revision": "synthetic",
                              "module_path": prefix + ".mjs", "module_sha256": file_hash(self.root / (prefix + ".mjs")),
                              "instruction_path": prefix + ".txt", "instruction_sha256": file_hash(self.root / (prefix + ".txt")),
                              "capability_path": "capability.json", "source_manifest_path": prefix + ".source.json",
                              "source_manifest_sha256": file_hash(self.root / (prefix + ".source.json")),
                              "validation_report_path": prefix + ".receipt.json",
                              "validation_report_sha256": file_hash(self.root / (prefix + ".receipt.json")),
                              "validation_configuration_sha256": digest(group)}
        self.pack = {"format": "matpilot-study-runtime/v1", "mode": "development", "real_compute_allowed": False,
                     "provenance": {"suite_sha256": "a" * 64, "plan_sha256": "b" * 64}, "shared": shared, "drivers": drivers,
                     "replay": {"manifest_path": "fixtures.json", "manifest_sha256": shared["replay_fixture_hash"]},
                     "tasks": [{"id": "D0001", "prompt": "SYNTHETIC public task only", "fixture_id": "synthetic"}],
                     "rows": [{"run_id": "D0001--" + g.replace("+", "plus") + "--1", "task_id": "D0001", "group": g,
                               "repeat": 1, "mode": "development", "configuration_hash": drivers[g]["configuration_hash"]}
                              for g in drivers]}
        self.write("pack.json", self.pack)
        self.records = []
        self.ledger()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def ledger(self):
        return self.write("ledger.json", {"records": self.records, "accounted_upper_bound_cny": sum(r["accounted_cny"] for r in self.records)})

    def run_record(self, group="M", status="completed", partial=False):
        row = next(r for r in self.pack["rows"] if r["group"] == group)
        rid = row["run_id"]
        usage = {"inputTokens": 100, "outputTokens": 50, "cacheReadTokens": 0, "cacheWriteTokens": 0}
        retained = status != "completed"
        record = {"id": "request-" + group, "session_id": "session-" + group, "run_id": rid,
                  "started_at": "2026-10-22T01:00:00Z", "ended_at": "2026-10-22T01:00:01Z",
                  "status": "reservation_retained" if retained else "usage_reported", "usage": usage,
                  "pricing": {"input_cny_per_million": 1, "output_cny_per_million": 2, "max_input_tokens": 1000, "max_output_tokens": 1000},
                  "accounted_cny": .003 if retained else .0002}
        self.records.append(record)
        self.ledger()
        terminal = {"status": status, "failure": None if status == "completed" else "SYNTHETIC stop",
                    "turn_end_reason": {"kind": "completed" if status == "completed" else "max-tokens"}}
        driver = self.pack["drivers"][group]
        trace = {"format": "matpilot-study-trace/v1", "run_identity": {k: row[k] for k in ("run_id", "task_id", "group", "repeat", "configuration_hash")},
                 "provenance": {**self.pack["provenance"], "runtime_pack_sha256": file_hash(self.root / "pack.json"),
                                **{k: driver[k] for k in ("module_sha256", "source_manifest_sha256", "instruction_sha256")}},
                 "events": [{"type": "system/message", "data": {"text": "SYSTEM_SENTINEL"}},
                            {"type": "request/context", "data": {"text": "CONTEXT_SENTINEL"}},
                            {"type": "user/message", "data": {"role": "user", "content": [{"type": "text", "text": self.pack["tasks"][0]["prompt"]}]}},
                            {"type": "assistant/message", "data": {"message": {"role": "assistant", "content": [{"type": "text", "text": "Complete public answer END_SENTINEL"}]},
                                                                      "stream": [{"text": "PRIVATE_STREAM_SENTINEL"}]}},
                            {"type": "turn/end", "data": {"reason": terminal["turn_end_reason"]}}],
                 "tools": [{"name": "read_artifact", "arguments": {"path": "C:/private/secret/a.cif"}, "result": {"value": {"full": "TOOL_END_SENTINEL"}}}],
                 "clarifications": [], "usage_record_ids": [record["id"]], "terminal": terminal}
        folder = "runner/runs/" + rid + "/"
        path = self.write(folder + ("trace.partial.json" if partial else "trace.json"), trace)
        result = {"row": row, "trace_sha256": file_hash(path), "correctness": "ungraded",
                  "outcome": {**terminal, "configuration_hash": row["configuration_hash"], "wall_seconds": 2,
                              "human_active_seconds": None, "api_cny": record["accounted_cny"], "science_cny": 0, "science_seconds": 0}}
        if not partial:
            self.write(folder + "result.json", result)
        return row, trace, result

    def build(self, output="review", selected_ids=None):
        return build_development_report(self.root / "pack.json", self.runner, self.root / "ledger.json", self.root / output, selected_ids=selected_ids)

    def rewrite_run(self, row, trace, result):
        folder = "runner/runs/" + row["run_id"] + "/"
        path = self.write(folder + "trace.json", trace)
        result["trace_sha256"] = file_hash(path)
        self.write(folder + "result.json", result)

    def test_complete_failed_and_missing_preserved_without_grades(self):
        self.run_record("M")
        self.run_record("U", "failed")
        report = self.build()
        self.assertEqual((report["planned_runs"], report["selected_runs"], report["completed"], report["failed"], report["missing"]), (3, 3, 1, 1, 1))
        self.assertAlmostEqual(report["groups"]["U"]["api_accounted_cny"], .003)
        self.assertEqual(report["groups"]["U"]["reservation_retained_requests"], 1)
        self.assertFalse(report["correctness_scored"])
        self.assertFalse(report["improvement_or_speedup_estimated"])
        self.assertFalse(report["scientific_results_validated"])
        self.assertTrue(all(r["correctness"] is None and r["human_active_seconds"] is None for r in report["runs"]))
        self.assertEqual(report["groups"]["M"]["observed_llm_request_wall_seconds"], 1)
        self.assertEqual(next(r for r in report["runs"] if r["group"] == "M")["tool_calls_by_name"], {"read_artifact": 1})
        self.assertIn("正常结束不等于任务正确", (self.root / "review/report.md").read_text(encoding="utf-8"))

    def test_public_transcript_complete_and_raw_files_unchanged(self):
        row, trace, result = self.run_record()
        trace["events"][3]["data"]["message"]["content"][0]["text"] += ' C:/private/a.cif KEEP_AFTER_PATH ```four````'
        trace["tools"][0]["result"]["credentials"] = {"api_key": "SECRET_SENTINEL", "Authorization": "Bearer BEARER_SENTINEL"}
        trace["tools"][0]["result"]["encoded"] = json.dumps({"password": "PASSWORD_SENTINEL", "source": "/home/private/a.cif"})
        self.rewrite_run(row, trace, result)
        original = (self.runner / "runs" / row["run_id"] / "trace.json").read_bytes()
        self.build()
        for suffix in ("md", "json"):
            text = (self.root / "review/runs" / (row["run_id"] + "." + suffix)).read_text(encoding="utf-8")
            for absent in ("SYSTEM_SENTINEL", "CONTEXT_SENTINEL", "PRIVATE_STREAM_SENTINEL", "SECRET_SENTINEL", "BEARER_SENTINEL", "PASSWORD_SENTINEL", "C:/private", "/home/private"):
                self.assertNotIn(absent, text)
            for present in ("END_SENTINEL", "TOOL_END_SENTINEL", "KEEP_AFTER_PATH"):
                self.assertIn(present, text)
        self.assertEqual(original, (self.runner / "runs" / row["run_id"] / "trace.json").read_bytes())

    def test_selection_explicit_and_existing_unselected_kept_distinct(self):
        self.run_record("M")
        self.run_record("U")
        (self.runner / "runs/unexpected").mkdir()
        report = self.build(selected_ids=["D0001--M--1", "D0001--Uplus--1"])
        self.assertEqual((report["planned_runs"], report["selected_runs"], report["not_selected_runs"], report["missing"]), (3, 2, 1, 1))
        self.assertEqual(report["unselected_recorded_runs"], ["D0001--U--1"])
        self.assertEqual(report["unexpected_run_directories"], ["unexpected"])
        for ids in (["D0001--M--1"] * 2, ["unknown"]):
            with self.assertRaisesRegex(ValueError, "Selected"):
                self.build(output="invalid-selection", selected_ids=ids)

    def test_partial_records_keep_cost_and_do_not_invent_end_or_time(self):
        self.run_record("M", partial=True)
        report = self.build()
        self.assertEqual((report["partial"], report["missing"], report["completed"]), (1, 2, 0))
        self.assertEqual(report["groups"]["M"]["run_wall_time_measured_count"], 0)
        self.assertAlmostEqual(report["groups"]["M"]["api_accounted_cny"], .0002)

    def test_source_tamper_and_formal_mode_rejected(self):
        self.write("M.mjs", "// altered")
        with self.assertRaisesRegex(ValueError, "hash"):
            self.build()
        self.pack["mode"] = "formal"
        self.write("pack.json", self.pack)
        with self.assertRaisesRegex(ValueError, "development"):
            self.build()

    def test_trace_hash_mismatch_is_kept_as_invalid(self):
        row, trace, result = self.run_record()
        result["trace_sha256"] = "0" * 64
        self.write("runner/runs/" + row["run_id"] + "/result.json", result)
        report = self.build()
        self.assertEqual((report["invalid"], report["selected_runs"]), (1, 3))
        self.assertEqual(report["groups"]["M"]["api_accounted_cny"], 0)
        self.assertAlmostEqual(next(r for r in report["runs"] if r["group"] == "M")["api_accounted_cny"], .0002)

    def test_terminal_identity_cost_and_science_mismatches_never_pass(self):
        row, trace, result = self.run_record()
        cases = ("identity", "terminal", "amount", "human", "science", "negative-time")
        for case in cases:
            t, r = copy.deepcopy(trace), copy.deepcopy(result)
            if case == "identity": t["run_identity"]["group"] = "U"
            if case == "terminal": t["events"][-1]["data"]["reason"] = {"kind": "max-tokens"}
            if case == "amount": r["outcome"]["api_cny"] = .01
            if case == "human": r["outcome"]["human_active_seconds"] = 1
            if case == "science": r["outcome"]["science_seconds"] = 1
            if case == "negative-time": r["outcome"]["wall_seconds"] = -1
            self.rewrite_run(row, t, r)
            with self.subTest(case=case):
                self.assertEqual(self.build(output="review-" + case)["invalid"], 1)

    def test_api_ids_require_complete_single_session_and_pricing_match(self):
        row, trace, result = self.run_record()
        original = copy.deepcopy(self.records)
        for case in ("missing-id", "duplicate-id", "omitted-call", "wrong-run", "bad-price"):
            t, r = copy.deepcopy(trace), copy.deepcopy(result)
            self.records = copy.deepcopy(original)
            if case == "missing-id": t["usage_record_ids"] = ["missing"]
            if case == "duplicate-id": t["usage_record_ids"] *= 2
            if case == "omitted-call": self.records.append({**self.records[0], "id": "omitted"})
            if case == "wrong-run": self.records[0]["run_id"] = "different"
            if case == "bad-price": self.records[0]["pricing"]["input_cny_per_million"] = 100
            self.ledger()
            self.rewrite_run(row, t, r)
            with self.subTest(case=case):
                self.assertEqual(self.build(output="review-" + case)["invalid"], 1)

    def test_previous_same_run_session_stays_in_cumulative_only(self):
        self.run_record()
        old = {**self.records[0], "id": "historical", "session_id": "historical-session"}
        self.records.append(old)
        self.ledger()
        report = self.build()
        self.assertEqual(report["ledger_accounted_cumulative_cny"], .0004)
        self.assertEqual(report["groups"]["M"]["api_accounted_cny"], .0002)

    def test_event_journal_disagreement_is_retained_as_invalid(self):
        row, trace, result = self.run_record()
        self.write("runner/runs/" + row["run_id"] + "/events.jsonl", json.dumps(trace["events"][0]) + "\n")
        report = self.build()
        self.assertEqual(report["invalid"], 1)
        self.assertIn("event journal", next(r for r in report["runs"] if r["group"] == "M")["issues"][0])

    def test_final_unsettled_reservation_is_not_treated_as_verified_cost(self):
        row, trace, result = self.run_record(status="failed")
        self.records[0]["status"] = "reserved"
        self.ledger()
        report = self.build()
        self.assertEqual(report["invalid"], 1)
        self.assertEqual(report["groups"]["M"]["cost_audited_runs"], 0)

    def test_no_overwrite_and_all_missing_are_explicit(self):
        report = self.build()
        self.assertEqual((report["planned_runs"], report["missing"]), (3, 3))
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(len(list((self.root / "review/runs").glob("*.json"))), 3)

    def test_sanitizer_protects_quotes_json_and_keeps_public_suffix(self):
        sanitizer = Sanitizer()
        text = sanitizer.clean('path "C:/Secret Place/a.cif" preserve; api_key="HIDE_ME"; /home/user/file.cif END')
        self.assertNotIn("HIDE_ME", text)
        self.assertNotIn("Secret Place", text)
        self.assertNotIn("/home/user", text)
        self.assertIn("preserve", text)
        self.assertIn("END", text)
        self.assertEqual(sanitizer.clean({"role": "system", "content": "hidden"}), {"omitted": "non-public system/reasoning content"})

    def test_cli_is_offline_and_exports_explicit_selection(self):
        self.run_record()
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run([sys.executable, "-m", "matpilot.development_report", str(self.root / "pack.json"),
                                 str(self.runner), str(self.root / "ledger.json"), str(self.root / "cli-review"),
                                 "--run-id", "D0001--M--1"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["selected_runs"], 1)


if __name__ == "__main__":
    unittest.main()
