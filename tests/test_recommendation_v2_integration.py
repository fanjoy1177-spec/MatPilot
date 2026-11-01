"""Public-interface integration with synthetic CPU fixtures, never ML inference.

Metric values below are authored software controls. They test ordering and
identity guarantees, not material properties or scientific accuracy.
"""
import asyncio
import contextlib
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pymatgen.core import Lattice, Structure
from pymatgen.io.cif import CifWriter

from matpilot.cli import main
from matpilot.config import Settings
from matpilot.evidence import validate_metric
from matpilot.server import make_server
from matpilot.service import Service
from matpilot.worker import execute_job


class RecommendationV2IntegrationTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.sequence = 0

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def record(self, cid, species=("Na", "Cl"), lattice=None):
        crystal = Structure(lattice or Lattice.cubic(5), list(species),
                            [[i / len(species)] * 3 for i in range(len(species))])
        path = self.root / (cid + ".cif")
        path.write_text(str(CifWriter(crystal)), encoding="utf-8")
        return {"candidate_id": cid, "structure_version_id": "original_" + cid,
                "path": str(path), "sha256": self.digest(path),
                "parent_version_id": None, "transformation": "generated"}

    def metric(self, record, value=.1, **changes):
        self.sequence += 1
        path = self.root / f"synthetic_metric_{self.sequence}.json"
        path.write_text(json.dumps({"software_control_only": True, "value": value}), encoding="utf-8")
        return validate_metric({
            "candidate_id": record["candidate_id"], "structure_version_id": record["structure_version_id"],
            "property": "energy_above_hull", "unit": "eV/atom", "value": value,
            "method": {"name": "synthetic_integration_fixture", "version": "1",
                       "weights_identity": "no_model_no_prediction", "parameters": {}},
            "input_artifact": {"path": record["path"], "sha256": record["sha256"]},
            "raw_output": {"path": str(path), "sha256": self.digest(path)},
            "convergence": "not_applicable", "reference_set": {"id": "synthetic_only", "sha256": "a" * 64},
            **changes}, record)

    @staticmethod
    def selection(metric, job_id=None):
        result = {k: metric[k] for k in ("candidate_id", "structure_version_id", "metric_id")}
        if job_id is not None:
            result["job_id"] = job_id
        return result

    @staticmethod
    def objective(**changes):
        return {"property": "energy_above_hull", "unit": "eV/atom", "direction": "minimize", **changes}

    def register(self, records, elements=("Na", "Cl"), metrics=(), **changes):
        by_version = {}
        for metric in metrics:
            by_version.setdefault(metric["structure_version_id"], []).append(metric)
        manifest = {"schema_version": 2, "expected_elements": list(elements),
                    "provenance": "synthetic_cpu_software_control_not_material_results",
                    "records": records, "metrics_by_structure_version": by_version, **changes}
        self.sequence += 1
        manifest_path = self.root / f"collection_{self.sequence}.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        config = self.root / f"config_{self.sequence}.json"
        config.write_text(json.dumps({"backend": "demo", "data_dir": f"jobs_{self.sequence}",
            "candidate_collections": {"fixture": {"manifest_path": manifest_path.name,
                "manifest_sha256": self.digest(manifest_path)}}}), encoding="utf-8")
        return Service(Settings.load(str(config))), manifest_path, config

    def replace_manifest(self, service, path, manifest):
        """Simulate an operator re-registering a manifest, not bypassing metrics."""
        path.write_text(json.dumps(manifest), encoding="utf-8")
        service.settings.candidate_collections["fixture"]["manifest_sha256"] = self.digest(path)

    def recommend(self, service, **kwargs):
        return service.recommend_candidates(collection_id="fixture", include_all_records=True, **kwargs)

    def job(self):
        service = Service(Settings(data_dir=self.root / "demo_jobs", backend="demo"))
        plan = service.prepare_plan({"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 2})
        self.assertEqual(plan["state"], "ready")
        job, _ = service.store.create_job(plan, "cpu-fixture-job", "demo", service.settings.snapshot())
        # This worker branch copies the fixed demo fixture; no generation model.
        execute_job(service.store, service.store.claim(os.getpid()))
        return service, job["job_id"], service.get_results(job["job_id"])["records"]

    @staticmethod
    def tool_result(result):
        structured = result[1] if isinstance(result, tuple) else result
        if isinstance(structured, dict):
            return structured
        blocks = result[0] if isinstance(result, tuple) else result
        return json.loads(blocks[0].text)

    def test_default_routes_nacl_alloy_and_covalent_without_fenio_restriction(self):
        cases = [("salt", ("Na", "Cl"), "ionic", "ionic_mixed_valence_v2"),
                 ("alloy", ("Fe", "Ni"), "metallic", "structural_v2"),
                 ("sic", ("Si", "C"), "covalent", "structural_v2")]
        for cid, elements, material_class, effective in cases:
            with self.subTest(material_class=material_class):
                service, _, _ = self.register([self.record(cid, elements)], elements)
                result = self.recommend(service, material_class=material_class)
                self.assertEqual(result["requested_selection_policy"], "auto")
                self.assertEqual(result["effective_selection_policy"], effective)
                self.assertEqual(result["selected_candidate_ids"], [cid])
                resolution = result["selection_policy_resolution"]
                self.assertEqual(resolution["chemical_feasibility_policy_invoked"], material_class == "ionic")
                self.assertFalse(resolution["property_prediction_performed"])
                if material_class != "ionic":
                    self.assertIn("chemical_feasibility_not_assessed", result["triage_policy"]["evidence_scope"])

    def test_declared_class_cannot_silently_force_an_inapplicable_ionic_policy(self):
        row = self.record("alloy", ("Fe", "Ni"))
        service, _, _ = self.register([row], ("Fe", "Ni"))
        with self.assertRaises(ValueError):
            self.recommend(service, material_class="metallic", selection_policy="ionic_mixed_valence_v2")
        for invalid in ("Fe-Ni-O", "", "run-model"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.recommend(service, material_class=invalid)

    def test_evidence_changes_equivalent_representative_and_retains_every_record(self):
        rows = [self.record("A"), self.record("B", lattice=Lattice.cubic(5.2))]
        metrics = [self.metric(rows[0], .8), self.metric(rows[1], .1)]
        service, _, _ = self.register(rows, metrics=metrics)
        self.assertEqual(self.recommend(service)["selected_candidate_ids"], ["A"])
        result = self.recommend(service, objective=self.objective(), metric_selections=[self.selection(m) for m in metrics])
        self.assertEqual(result["selected_candidate_ids"], ["B"])
        self.assertEqual(result["records"][0]["representative_id"], "B")
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["effective_selection_policy"], "evidence_v1")
        self.assertEqual(result["stage_counts"]["priority_relaxation_count"], 2)
        self.assertEqual(result["stage_counts"]["priority_representative_count"], 1)
        self.assertEqual(result["evidence_ranking"]["ranking_metadata"]["scientific_accuracy"], "not_validated")

    def test_comparable_evidence_precedes_diversity_across_representatives(self):
        rows = [self.record("A"), self.record("B", lattice=Lattice.orthorhombic(8, 5, 5)),
                self.record("C", ("Na", "Na", "Cl"))]
        metrics = [self.metric(row, value) for row, value in zip(rows, (.1, .2, .9))]
        service, _, _ = self.register(rows, metrics=metrics)
        coverage = self.recommend(service, limit=2, material_class="unknown")
        self.assertEqual(coverage["selected_candidate_ids"], ["A", "C"])
        ranked = self.recommend(service, limit=2, material_class="unknown", objective=self.objective(),
                                metric_selections=[self.selection(m) for m in metrics])
        self.assertEqual(ranked["selected_candidate_ids"], ["A", "B"])
        self.assertEqual(ranked["records"][1]["selection_reason"]["ranking_mode"], "evidence_key_then_coverage")

    def test_missing_explicit_evidence_is_review_even_when_values_are_available(self):
        rows = [self.record("A"), self.record("B", lattice=Lattice.cubic(5.2))]
        metrics = [self.metric(rows[0], .1), self.metric(rows[1], .2)]
        service, _, _ = self.register(rows, metrics=metrics)
        for selections in (None, []):
            with self.subTest(selections=selections):
                result = self.recommend(service, objective=self.objective(), metric_selections=selections)
                self.assertEqual(result["selected_count"], 0)
                self.assertEqual(result["stage_counts"]["manual_review_count"], 2)
                self.assertEqual(len(result["evidence_ranking"]["metric_selection_audit"]["unselected_metrics"]), 2)
        partial = self.recommend(service, objective=self.objective(), metric_selections=[self.selection(metrics[1])])
        self.assertEqual(partial["selected_candidate_ids"], ["B"])
        self.assertEqual(partial["records"][0]["relaxation_priority"]["tier"], "manual_review")

    def test_explicit_operator_priority_selector_cannot_be_bypassed_by_better_value(self):
        rows = [self.record("A"), self.record("B", lattice=Lattice.cubic(5.2))]
        metrics = [self.metric(rows[0], .8), self.metric(rows[1], .1)]
        service, _, _ = self.register(rows, metrics=metrics, priority_candidate_ids=["A"],
                                     priority_selection_basis="synthetic explicit operator preference")
        result = self.recommend(service, objective=self.objective(), metric_selections=[self.selection(m) for m in metrics])
        self.assertEqual(result["selected_candidate_ids"], ["A"])
        self.assertFalse(result["records"][1]["caller_allowed"])

    def test_collection_metrics_require_schema_two_unique_version_and_immutable_identity(self):
        rows = [self.record("A"), self.record("B")]
        metrics = [self.metric(rows[0]), self.metric(rows[1])]
        service, path, _ = self.register(rows, metrics=metrics)
        baseline = json.loads(path.read_text(encoding="utf-8"))
        variants = []
        old_schema = deepcopy(baseline)
        old_schema["schema_version"] = 1
        variants.append(old_schema)
        duplicate_id = deepcopy(baseline)
        duplicate_id["records"][1]["candidate_id"] = "A"
        variants.append(duplicate_id)
        duplicate_version = deepcopy(baseline)
        duplicate_version["records"][1]["structure_version_id"] = "original_A"
        variants.append(duplicate_version)
        for field, value in (("candidate_id", "B"), ("structure_version_id", "original_B"), ("value", .0001)):
            mutated = deepcopy(baseline)
            mutated["metrics_by_structure_version"]["original_A"][0][field] = value
            variants.append(mutated)
        relative_metric_path = deepcopy(baseline)
        relative_metric_path["metrics_by_structure_version"]["original_A"][0]["raw_output"]["path"] = "metric.json"
        variants.append(relative_metric_path)
        for index, manifest in enumerate(variants):
            with self.subTest(index=index):
                self.replace_manifest(service, path, manifest)
                with self.assertRaises(ValueError):
                    self.recommend(service, objective=self.objective())

    def test_changed_manifest_raw_output_or_cif_is_never_ranked(self):
        for artifact in ("manifest", "raw_output", "input_artifact"):
            with self.subTest(artifact=artifact):
                row = self.record("A_" + artifact)
                metric = self.metric(row)
                service, manifest, _ = self.register([row], metrics=[metric])
                path = manifest if artifact == "manifest" else Path(metric[artifact]["path"])
                path.write_bytes(path.read_bytes() + b"\n# altered\n")
                with self.assertRaises(ValueError):
                    self.recommend(service, objective=self.objective(), metric_selections=[self.selection(metric)])

    def test_metric_selection_is_not_interchangeable_by_candidate_or_version(self):
        rows = [self.record("A"), self.record("B")]
        metrics = [self.metric(rows[0]), self.metric(rows[1])]
        service, _, _ = self.register(rows, metrics=metrics)
        for field, value in (("candidate_id", "B"), ("structure_version_id", "original_B"),
                             ("metric_id", metrics[1]["metric_id"]), ("job_id", "unregistered_job")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.recommend(service, objective=self.objective(),
                               metric_selections=[{**self.selection(metrics[0]), field: value}])
        with self.assertRaises(ValueError):
            self.recommend(service, metric_selections=[self.selection(metrics[0])])

    def test_incompatible_methods_cannot_silently_produce_quality_order(self):
        rows = [self.record("A"), self.record("B")]
        methods = [self.metric(rows[0]), self.metric(rows[1], method={"name": "different_fixture", "version": "1",
                    "weights_identity": "no_model", "parameters": {}})]
        service, _, _ = self.register(rows, metrics=methods)
        with self.assertRaises(ValueError):
            self.recommend(service, objective=self.objective(), metric_selections=[self.selection(m) for m in methods])

    def test_evidence_directory_withholds_values_and_flags_ambiguous_choices(self):
        rows = [self.record("A"), self.record("B")]
        metrics = [self.metric(rows[0], .1), self.metric(rows[0], .9), self.metric(rows[1], .2)]
        service, _, _ = self.register(rows, metrics=metrics)
        result = service.get_candidate_evidence(collection_id="fixture")
        self.assertTrue(result["values_withheld"])
        self.assertEqual(len(result["method_groups"]), 1)
        group = result["method_groups"][0]
        self.assertEqual(group["candidate_count"], 2)
        self.assertEqual(group["ambiguous_candidate_ids"], ["A"])
        self.assertEqual(len(group["metric_selections"]), 3)
        self.assertNotIn('"value"', json.dumps(result))
        with self.assertRaises(ValueError):
            self.recommend(service, objective=self.objective(), metric_selections=group["metric_selections"])

    def test_read_only_recommendation_and_disabled_prediction_never_start_a_process(self):
        row = self.record("A")
        service, _, _ = self.register([row], metrics=[self.metric(row)])
        with patch("subprocess.Popen", side_effect=AssertionError("Unexpected process")), \
             patch("matpilot.worker.execute_job", side_effect=AssertionError("Unexpected worker")), \
             patch.object(Service, "submit_job", side_effect=AssertionError("Unexpected job")):
            result = self.recommend(service)
            self.assertEqual(result["selected_count"], 1)
            service.get_candidate_evidence(collection_id="fixture")
            prepared = service.prepare_property_prediction(candidate_id="A", structure_version_id="original_A",
                material_class="ionic", structure_state="original_generated", collection_id="fixture")
            self.assertEqual(prepared["status"], "disabled")
            self.assertFalse(prepared["real_prediction_performed"])
        with self.assertRaises(TypeError):
            service.recommend_candidates(collection_id="fixture", command="arbitrary command")
        with self.assertRaises(ValueError):
            service.prepare_property_prediction(candidate_id="A", structure_version_id="other-version",
                material_class="ionic", structure_state="original_generated", collection_id="fixture")

    def test_job_ranking_checks_job_identity_and_preserves_hard_requirement_gate(self):
        service, job_id, rows = self.job()
        metrics = [service.store.record_metric({k: v for k, v in self.metric(row, value).items() if k != "metric_id"})
                   for row, value in zip(rows, (.8, .1))]
        selections = [self.selection(m, job_id) for m in metrics]
        result = service.recommend_candidates(job_id=job_id, objective=self.objective(), metric_selections=selections)
        self.assertEqual(result["selected_candidate_ids"], [rows[1]["candidate_id"]])
        wrong = deepcopy(selections)
        wrong[0]["job_id"] = "different_job"
        with self.assertRaises(ValueError):
            service.recommend_candidates(job_id=job_id, objective=self.objective(), metric_selections=wrong)
        plan = service.store.plan(service.store.job(job_id)["plan_id"])
        plan["requirements"].append({"requirement_id": "unmeasured", "kind": "numeric_bound",
            "property": "dft_band_gap", "normalized_value": 1., "normalized_unit": "eV",
            "strength": "hard", "relation": "at_least"})
        with patch.object(service.store, "plan", return_value=plan):
            gated = service.recommend_candidates(job_id=job_id, objective=self.objective(), metric_selections=selections)
        self.assertEqual(gated["selected_count"], 0)

    def test_report_objectives_and_selections_are_isolated_from_legacy_and_default(self):
        service, job_id, rows = self.job()
        metrics = [service.store.record_metric({k: v for k, v in self.metric(row, value).items() if k != "metric_id"})
                   for row, value in zip(rows, (.8, .1))]
        selections = [self.selection(m, job_id) for m in metrics]
        frozen = []
        for kwargs in ({}, {"recommendation_limit": 1, "recommendation_policy": "auto"},
                       {"recommendation_limit": 1, "recommendation_policy": "structural_v1"}):
            report = service.build_report(job_id, **kwargs)
            frozen.extend((Path(report[key]), Path(report[key]).read_bytes()) for key in ("report_path", "evidence_path"))
        reports = []
        for objective, choices in ((self.objective(), selections),
                                   (self.objective(direction="maximize"), selections),
                                   (self.objective(target_max=.15), selections),
                                   (self.objective(), selections[:1]),
                                   (self.objective(), [])):
            reports.append(service.build_report(job_id, recommendation_limit=1,
                recommendation_objective=objective, recommendation_metric_selections=choices))
        reports.append(service.build_report(job_id, recommendation_limit=1, recommendation_policy="triage_v2"))
        self.assertEqual(len({report["report_path"] for report in reports}), len(reports))
        first = json.loads(Path(reports[0]["evidence_path"]).read_text(encoding="utf-8"))["recommendation"]
        second = json.loads(Path(reports[1]["evidence_path"]).read_text(encoding="utf-8"))["recommendation"]
        self.assertEqual(first["selected_candidate_ids"], [rows[1]["candidate_id"]])
        self.assertEqual(second["selected_candidate_ids"], [rows[0]["candidate_id"]])
        for path, content in frozen:
            self.assertEqual(path.read_bytes(), content)
        self.assertIn("ionic_mixed_valence_v2", Path(reports[-1]["report_path"]).parts)

    def test_cli_and_fastmcp_route_full_evidence_contract_without_inference(self):
        rows = [self.record("A"), self.record("B", lattice=Lattice.cubic(5.2))]
        metrics = [self.metric(rows[0], .8), self.metric(rows[1], .1)]
        service, _, config = self.register(rows, metrics=metrics)
        objective_path, selections_path = self.root / "objective.json", self.root / "selections.json"
        objective_path.write_text(json.dumps(self.objective()), encoding="utf-8")
        choices = [self.selection(m) for m in metrics]
        selections_path.write_text(json.dumps(choices), encoding="utf-8")
        output = io.StringIO()
        args = ["matpilot", "--config", str(config), "recommend-collection", "fixture", "--limit", "1",
                "--objective-file", str(objective_path), "--metric-selections-file", str(selections_path),
                "--material-class", "ionic", "--include-all-records"]
        with patch("sys.argv", args), contextlib.redirect_stdout(output), \
             patch("subprocess.Popen", side_effect=AssertionError("Unexpected process")):
            main()
        cli = json.loads(output.getvalue())
        self.assertEqual(cli["selected_candidate_ids"], ["B"])
        self.assertEqual(cli["effective_selection_policy"], "evidence_v1")

        async def exercise():
            app = make_server(service.settings)
            tools = {tool.name: tool for tool in await app.list_tools()}
            self.assertIn("get_candidate_evidence", tools)
            self.assertIn("prepare_property_prediction", tools)
            self.assertTrue(tools["recommend_candidates"].annotations.readOnlyHint)
            schema = tools["recommend_candidates"].inputSchema["properties"]
            self.assertEqual(schema["selection_policy"]["default"], "auto")
            self.assertNotIn("command", schema)
            result = self.tool_result(await app.call_tool("recommend_candidates", {
                "collection_id": "fixture", "limit": 1, "objective": self.objective(),
                "metric_selections": choices, "material_class": "ionic", "include_all_records": True}))
            self.assertEqual(result["selected_candidate_ids"], cli["selected_candidate_ids"])
            self.assertEqual(result["all_candidate_audit_sha256"], cli["all_candidate_audit_sha256"])
            directory = self.tool_result(await app.call_tool("get_candidate_evidence", {"collection_id": "fixture"}))
            self.assertTrue(directory["values_withheld"])
            self.assertNotIn('"value"', json.dumps(directory))
            prepared = self.tool_result(await app.call_tool("prepare_property_prediction", {
                "collection_id": "fixture", "candidate_id": "A", "structure_version_id": "original_A",
                "material_class": "ionic", "structure_state": "original_generated"}))
            self.assertEqual(prepared["status"], "disabled")
            self.assertFalse(prepared["real_prediction_performed"])
        with patch("subprocess.Popen", side_effect=AssertionError("Unexpected process")):
            asyncio.run(exercise())

    def test_cli_and_fastmcp_report_pass_through_objective_and_explicit_job_bindings(self):
        service, job_id, rows = self.job()
        metrics = [service.store.record_metric({k: v for k, v in self.metric(row, value).items() if k != "metric_id"})
                   for row, value in zip(rows, (.8, .1))]
        choices = [self.selection(m, job_id) for m in metrics]
        config, objective, selections = (self.root / name for name in
                                        ("report_config.json", "report_objective.json", "report_selections.json"))
        config.write_text(json.dumps({"backend": "demo", "data_dir": str(service.store.root)}), encoding="utf-8")
        objective.write_text(json.dumps(self.objective(direction="maximize")), encoding="utf-8")
        selections.write_text(json.dumps(choices), encoding="utf-8")
        output = io.StringIO()
        args = ["matpilot", "--config", str(config), "report", job_id, "--recommendation-limit", "1",
                "--objective-file", str(objective), "--metric-selections-file", str(selections),
                "--material-class", "ionic"]
        with patch("sys.argv", args), contextlib.redirect_stdout(output):
            main()
        cli = json.loads(output.getvalue())
        cli_bytes = Path(cli["evidence_path"]).read_bytes()
        evidence = json.loads(cli_bytes)["recommendation"]
        self.assertEqual(evidence["selected_candidate_ids"], [rows[0]["candidate_id"]])

        async def exercise():
            app = make_server(service.settings)
            return self.tool_result(await app.call_tool("build_report", {
                "job_id": job_id, "recommendation_limit": 1, "recommendation_objective": self.objective(),
                "recommendation_metric_selections": choices, "material_class": "ionic"}))
        report = asyncio.run(exercise())
        self.assertNotEqual(report["evidence_path"], cli["evidence_path"])
        self.assertEqual(Path(cli["evidence_path"]).read_bytes(), cli_bytes)
        evidence = json.loads(Path(report["evidence_path"]).read_text(encoding="utf-8"))["recommendation"]
        self.assertEqual(evidence["selected_candidate_ids"], [rows[1]["candidate_id"]])


if __name__ == "__main__":
    unittest.main()
