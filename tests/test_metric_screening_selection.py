"""Explicit production requirement-check evidence; all values are synthetic CPU data."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from matpilot.config import Settings
from matpilot.server import make_server
from matpilot.service import Service
from matpilot.worker import execute_job


ROOT = Path(__file__).resolve().parents[1]


class MetricScreeningSelectionTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.service = Service(Settings(data_dir=self.root, backend="demo", real_compute_enabled=False))
        self.counter = 0
        self.plan = self.service.prepare_plan({"conditions": {"dft_band_gap": 1.1}, "num_samples": 2, "batch_size": 1})
        self.assertEqual(self.plan["state"], "ready", self.plan["issues"])
        job, _ = self.service.store.create_job(self.plan, "synthetic-selection-test", "demo", self.service.settings.snapshot())
        execute_job(self.service.store, self.service.store.claim(os.getpid()))
        self.job = job["job_id"]
        self.first, self.other = self.service.get_results(self.job)["records"]
        self.passing, self.failing = self.metric(self.first, 1.1), self.metric(self.first, 1.6)

    def tearDown(self):
        self.temp.cleanup()

    def metric(self, record, value, *, prop="dft_band_gap", unit="eV", unknown=False):
        self.counter += 1
        raw = self.root / f"synthetic-evidence-{self.counter}.json"
        raw.write_text(json.dumps({"synthetic": True, "value": value, "science_computation_performed": False}), encoding="utf-8")
        method = {"name": "synthetic", "version": "1", "weights_identity": "synthetic-no-weights", "parameters": {"synthetic": True}}
        if unknown:
            method["weights_identity"] = None
        return self.service.store.record_metric({"candidate_id": record["candidate_id"],
            "structure_version_id": record["structure_version_id"], "property": prop, "value": value, "unit": unit,
            "method": method, "input_artifact": {"path": record["path"], "sha256": record["sha256"]},
            "raw_output": {"path": str(raw), "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()},
            "convergence": "unknown" if unknown else "not_applicable", "reference_set": None})

    def selection(self, metric):
        return {"job_id": self.job, **{k: metric[k] for k in ("candidate_id", "structure_version_id", "metric_id")}}

    def checks(self, result, candidate=None):
        record = next(r for r in result["records"] if r["candidate_id"] == (candidate or self.first)["candidate_id"])
        return next(check for check in record["requirement_checks"] if check["property"] == "dft_band_gap")

    def test_default_unknown_and_explicit_pass_fail_leave_counts_budget_and_plan_unchanged(self):
        before = self.service.get_job(self.job)
        self.assertEqual(self.checks(self.service.screen_candidates(self.job))["status"], "unknown")
        for metric, status in ((self.passing, "pass"), (self.failing, "fail")):
            result = self.service.screen_candidates(self.job, [self.selection(metric)])
            check = self.checks(result)
            self.assertEqual(check["status"], status)
            self.assertEqual(check["metric_id"], metric["metric_id"])
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["screening_version_scope"], "original_generated_versions_only")
        after = self.service.get_job(self.job)
        self.assertEqual(before["sampling_attempts_charged"], after["sampling_attempts_charged"])
        self.assertEqual(before["sampling_budget_remaining"], after["sampling_budget_remaining"])
        self.assertEqual(self.service.store.plan(self.plan["plan_id"]), self.plan)
        self.assertEqual(self.checks(self.service.screen_candidates(self.job))["status"], "unknown")

    def test_all_adverse_metrics_remain_in_audit_and_unselected_candidates_keep_default_checks(self):
        self.metric(self.other, 1.1)
        self.metric(self.other, 1.6)
        result = self.service.screen_candidates(self.job, [self.selection(self.passing)])
        all_metrics = result["all_metrics_by_structure_version"][self.first["structure_version_id"]]
        effective = result["metrics_by_structure_version"][self.first["structure_version_id"]]
        self.assertEqual([m["value"] for m in all_metrics], [1.1, 1.6])
        self.assertEqual([m["metric_id"] for m in effective], [self.passing["metric_id"]])
        self.assertIn(self.failing["metric_id"], result["metric_selection_audit"]["unselected_metric_ids"])
        self.assertEqual(self.checks(result, self.other)["status"], "unknown")

    def test_missing_method_and_convergence_cannot_become_pass_when_selected(self):
        incomplete = self.metric(self.first, 1.1, unknown=True)
        result = self.service.screen_candidates(self.job, [self.selection(incomplete)])
        self.assertEqual(self.checks(result)["status"], "unknown")
        self.assertIn(incomplete["metric_id"], result["metric_selection_audit"]["selected_metric_ids"])

    def test_wrong_binding_unknown_duplicates_and_ambiguous_property_choices_are_rejected(self):
        for key, value in (("job_id", "missing"), ("candidate_id", self.other["candidate_id"]),
                           ("structure_version_id", self.other["structure_version_id"]), ("metric_id", "missing")):
            wrong = {**self.selection(self.passing), key: value}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "do not match|does not belong"):
                self.service.screen_candidates(self.job, [wrong])
        with self.assertRaisesRegex(ValueError, "duplicates"):
            self.service.screen_candidates(self.job, [self.selection(self.passing)] * 2)
        with self.assertRaisesRegex(ValueError, "one metric per"):
            self.service.screen_candidates(self.job, [self.selection(self.passing), self.selection(self.failing)])
        for selections in ([], [{"metric_id": self.passing["metric_id"]}]):
            with self.assertRaisesRegex(ValueError, "explicit metric selections"):
                self.service.screen_candidates(self.job, selections)

    def test_transformed_versions_and_irrelevant_property_or_units_are_rejected(self):
        path = self.root / "synthetic-child.cif"
        path.write_text("# synthetic serialization only\n" + Path(self.first["path"]).read_text(), encoding="utf-8")
        child = self.service.store.add_structure_version(self.first["structure_version_id"], path, "synthetic", {"synthetic": True})
        child_metric = self.metric(child, 1.1)
        with self.assertRaisesRegex(ValueError, "original generated"):
            self.service.screen_candidates(self.job, [self.selection(child_metric)])
        for changes in ({"prop": "ml_bulk_modulus", "unit": "GPa"}, {"unit": "meV"}):
            unrelated = self.metric(self.first, 1.1, **changes)
            with self.assertRaisesRegex(ValueError, "property and unit"):
                self.service.screen_candidates(self.job, [self.selection(unrelated)])

    def test_two_selection_reports_and_default_coexist_with_full_audit(self):
        reports = [self.service.build_report(self.job),
                   self.service.build_report(self.job, [self.selection(self.passing)]),
                   self.service.build_report(self.job, [self.selection(self.failing)])]
        self.assertEqual(len({r["evidence_path"] for r in reports}), 3)
        for report, status in zip(reports, ("unknown", "pass", "fail")):
            evidence = json.loads(Path(report["evidence_path"]).read_bytes())
            self.assertEqual(self.checks(evidence["screening"])["status"], status)
            self.assertTrue(Path(report["report_path"]).is_file())
            if status != "unknown":
                screening = evidence["screening"]
                self.assertEqual(len(screening["all_metrics_by_structure_version"][self.first["structure_version_id"]]), 2)
                self.assertEqual(len(screening["metric_selection_audit"]["selected_metric_ids"]), 1)
                self.assertIn("示例数据不构成真实材料性质结论", Path(report["report_path"]).read_text(encoding="utf-8"))
        other_metric = self.metric(self.other, 1.1)
        choices = [self.selection(self.passing), self.selection(other_metric)]
        first = self.service.build_report(self.job, choices)
        second = self.service.build_report(self.job, list(reversed(choices)))
        self.assertEqual(first["evidence_path"], second["evidence_path"])

    def test_selected_or_unselected_raw_evidence_tampering_still_blocks_report(self):
        # Selecting the favorable value must not hide damaged adverse evidence.
        Path(self.failing["raw_output"]["path"]).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.service.build_report(self.job, [self.selection(self.passing)])

    def test_actual_nine_tool_mcp_accepts_selection_for_screen_and_report(self):
        async def exercise():
            app = make_server(self.service.settings)
            tools = await app.list_tools()
            self.assertEqual(len(tools), 9)
            for name in ("screen_candidates", "build_report"):
                schema = next(t.inputSchema for t in tools if t.name == name)
                self.assertIn("metric_selections", schema["properties"])
                args = {"job_id": self.job, "metric_selections": [self.selection(self.passing)]}
                response = await app.call_tool(name, args)
                data = response[1] if isinstance(response, tuple) else json.loads(response[0].text)
                if name == "screen_candidates":
                    self.assertEqual(self.checks(data)["status"], "pass")
                else:
                    saved = json.loads(Path(data["evidence_path"]).read_bytes())
                    self.assertEqual(self.checks(saved["screening"])["status"], "pass")
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
