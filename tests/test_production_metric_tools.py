"""Production Service/FastMCP contracts with CPU demo and synthetic evidence only."""
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


class ProductionMetricToolsTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.service = Service(Settings(data_dir=self.root, backend="demo", real_compute_enabled=False))
        self.counter = 0
        self.job = self.execute()
        self.original, self.other = self.service.get_results(self.job)["records"]
        transformed = self.root / "synthetic-child.cif"
        transformed.write_text("# SYNTHETIC serialization transform; no relaxation performed\n" + Path(self.original["path"]).read_text(), encoding="utf-8")
        self.child = self.service.store.add_structure_version(self.original["structure_version_id"], transformed,
            "synthetic_serialization_change", {"synthetic": True, "science_computation_performed": False})

    def tearDown(self):
        self.temp.cleanup()

    def execute(self):
        plan = self.service.prepare_plan({"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 2, "batch_size": 1})
        self.assertEqual(plan["state"], "ready")
        self.counter += 1
        job, _ = self.service.store.create_job(plan, f"synthetic-evidence-{self.counter}", "demo", self.service.settings.snapshot())
        execute_job(self.service.store, self.service.store.claim(os.getpid()))
        return job["job_id"]

    def metric(self, record, value=1.0, *, method="synthetic-method-a", unit="eV", convergence="not_applicable"):
        self.counter += 1
        raw = self.root / f"synthetic-metric-{self.counter}.json"
        raw.write_text(json.dumps({"synthetic": True, "value": value, "no_model_or_property_calculation": True}), encoding="utf-8")
        return self.service.store.record_metric({"candidate_id": record["candidate_id"],
            "structure_version_id": record["structure_version_id"], "property": "dft_band_gap", "value": value, "unit": unit,
            "method": {"name": method, "version": "synthetic-v1", "weights_identity": "synthetic-no-weights", "parameters": {"synthetic": True}},
            "input_artifact": {"path": record["path"], "sha256": record["sha256"]},
            "raw_output": {"path": str(raw), "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()},
            "convergence": convergence, "reference_set": None})

    def selection(self, metric, job_id=None):
        return {"job_id": job_id or self.job, **{k: metric[k] for k in ("candidate_id", "structure_version_id", "metric_id")}}

    def test_get_results_exposes_each_version_metrics_without_inheritance_or_count_change(self):
        old = self.metric(self.original, 1.1)
        result = self.service.get_results(self.job)
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["structure_versions"][self.original["candidate_id"]]), 2)
        metrics = result["metrics_by_structure_version"]
        self.assertEqual(metrics[self.original["structure_version_id"]][0]["metric_id"], old["metric_id"])
        self.assertEqual(metrics[self.child["structure_version_id"]], [])
        new = self.metric(self.child, 1.6)
        result = self.service.get_results(self.job)
        self.assertEqual(result["metrics_by_structure_version"][self.child["structure_version_id"]][0]["metric_id"], new["metric_id"])
        screened = self.service.screen_candidates(self.job)
        self.assertEqual(screened["count"], 2)
        self.assertEqual(screened["screening_version_scope"], "original_generated_versions_only")
        self.assertNotIn(self.child["structure_version_id"], screened["metrics_by_structure_version"])
        report = self.service.build_report(self.job)
        self.assertEqual(json.loads(Path(report["evidence_path"]).read_bytes())["screening"]["count"], 2)

    def test_single_job_explicit_versions_compare_only_the_selected_metrics(self):
        old, selected = self.metric(self.original, 1.1), self.metric(self.child, 1.6)
        unselected = self.metric(self.child, 99)
        selections = [self.selection(old), self.selection(selected)]
        result = self.service.compare_runs([self.job], selections)
        self.assertTrue(result["metric_comparison"]["comparable"])
        self.assertEqual(result["metric_comparison"]["metric_ids"], [old["metric_id"], selected["metric_id"]])
        self.assertNotIn(unselected["metric_id"], result["metric_comparison"]["metric_ids"])
        self.assertEqual([m["value"] for m in result["selected_metrics"]], [1.1, 1.6])
        self.assertNotIn("ranking", result)
        self.assertNotIn("ranking", result["metric_comparison"])
        self.assertEqual(result["rows"][0]["count"], 2)

    def test_cross_candidate_version_metric_and_job_binding_are_rejected(self):
        old, newer = self.metric(self.original), self.metric(self.child, 2)
        valid = [self.selection(old), self.selection(newer)]
        for field, wrong in (("candidate_id", self.other["candidate_id"]),
                             ("structure_version_id", self.other["structure_version_id"]),
                             ("metric_id", old["metric_id"]), ("job_id", "unknown-job")):
            selections = copy.deepcopy(valid)
            selections[1][field] = wrong
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "do not match|does not belong"):
                self.service.compare_runs([self.job], selections)
        other_job = self.execute()
        outsider = self.service.get_results(other_job)["records"][0]
        outside_metric = self.metric(outsider, 2)
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.service.compare_runs([self.job], [valid[0], self.selection(outside_metric, other_job)])

    def test_incompatible_method_unit_and_unknown_metadata_never_get_a_ranking(self):
        first = self.metric(self.original)
        for args in ({"method": "synthetic-method-b"}, {"unit": "meV"}, {"convergence": "unknown"}):
            second = self.metric(self.child, 2, **args)
            result = self.service.compare_runs([self.job], [self.selection(first), self.selection(second)])
            self.assertFalse(result["metric_comparison"]["comparable"])
            self.assertTrue(result["metric_comparison"]["reasons"])
            self.assertNotIn("ranking", result["metric_comparison"])

    def test_known_cross_model_provenance_uses_metric_protocol_without_mixing_demo(self):
        # Only source labels are simulated here. Both jobs above/below execute
        # fixed CPU demos, never actual MatterGen or DiffCSP inference.
        other_job = self.execute()
        other_record = self.service.get_results(other_job)["records"][0]
        first, second = self.metric(self.original), self.metric(other_record, 2)
        selections = [self.selection(first), self.selection(second, other_job)]
        self.service.store.update(self.job, provenance="mattergen_execution")
        self.service.store.update(other_job, provenance="diffcsp_execution")
        result = self.service.compare_runs([self.job, other_job], selections)
        self.assertTrue(result["metric_comparison"]["comparable"])
        self.assertNotIn("ranking", result["metric_comparison"])
        self.service.store.update(other_job, provenance="demonstration_fixture")
        with self.assertRaisesRegex(ValueError, "mix demo and real"):
            self.service.compare_runs([self.job, other_job], selections)
        self.service.store.update(other_job, provenance="unknown_evaluator_source")
        with self.assertRaisesRegex(ValueError, "unknown provenance"):
            self.service.compare_runs([self.job, other_job], selections)

    def test_changed_raw_metric_and_structure_are_rejected(self):
        first, second = self.metric(self.original), self.metric(self.child, 2)
        Path(second["raw_output"]["path"]).write_text("changed", encoding="utf-8")
        for call in (lambda: self.service.get_results(self.job),
                     lambda: self.service.compare_runs([self.job], [self.selection(first), self.selection(second)])):
            with self.assertRaisesRegex(ValueError, "checksum"):
                call()
        Path(self.child["path"]).write_text("changed CIF", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.service.get_results(self.job)

    def test_legacy_comparison_is_unchanged_and_selection_requires_explicit_unique_ids(self):
        with self.assertRaisesRegex(ValueError, "2 to 10"):
            self.service.compare_runs([self.job])
        another = self.execute()
        result = self.service.compare_runs([self.job, another])
        self.assertEqual(set(result), {"same_task", "rows", "interpretation"})
        self.assertEqual(len(result["rows"]), 2)
        metric = self.metric(self.original)
        for choices in ([], [self.selection(metric)], [self.selection(metric)] * 2,
                        [{"candidate_id": metric["candidate_id"]}] * 2):
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                self.service.compare_runs([self.job], choices)

    def test_actual_fastmcp_has_ten_tools_and_accepts_explicit_metric_schema(self):
        first, second = self.metric(self.original), self.metric(self.child, 2)
        async def exercise():
            app = make_server(self.service.settings)
            async def call(name, arguments):
                response = await app.call_tool(name, arguments)
                return response[1] if isinstance(response, tuple) else json.loads(response[0].text)
            tools = await app.list_tools()
            self.assertEqual(len(tools), 10)
            schema = next(t.inputSchema for t in tools if t.name == "compare_runs")
            self.assertIn("metric_selections", schema["properties"])
            self.assertIn("MetricSelection", schema["$defs"])
            self.assertEqual(set(schema["$defs"]["MetricSelection"]["required"]),
                             {"job_id", "candidate_id", "structure_version_id", "metric_id"})
            results = await call("get_results", {"job_id": self.job})
            self.assertIn(self.child["structure_version_id"], results["metrics_by_structure_version"])
            compared = await call("compare_runs", {"job_ids": [self.job],
                "metric_selections": [self.selection(first), self.selection(second)]})
            self.assertTrue(compared["metric_comparison"]["comparable"])
            with self.assertRaisesRegex(Exception, "metric_id"):
                await app.call_tool("compare_runs", {"job_ids": [self.job], "metric_selections": [{"job_id": self.job}] * 2})
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
