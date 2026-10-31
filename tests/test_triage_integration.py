"""CPU-only production-path checks for automatic Fe-Ni-O relaxation triage."""
import asyncio
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from pymatgen.core import Lattice, Structure
from pymatgen.io.cif import CifWriter

from matpilot.cli import main
from matpilot.config import Settings
from matpilot.server import make_server
from matpilot.service import Service
from matpilot.worker import execute_job


class TriageIntegrationTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.service = Service(Settings(data_dir=self.root / "jobs", backend="demo"))

    def tearDown(self):
        self.temp.cleanup()

    def fixture_job(self):
        """Use the real archive collector and job store, with hand-built CPU CIFs."""
        crystals = [
            Structure(Lattice.cubic(5), ["Fe", "Ni", "O", "O"],
                      [[0, 0, 0], [0.5, 0.5, 0], [0.5, 0, 0.5], [0, 0.5, 0.5]]),
            Structure(Lattice.orthorhombic(0.7, 8, 8), ["Fe", "Ni", "O", "O"],
                      [[0, 0, 0], [0, 0.5, 0], [0, 0, 0.5], [0, 0.5, 0.5]]),
            Structure(Lattice.cubic(4.8), ["Fe", "Ni", "O"],
                      [[0, 0, 0], [0.5, 0.5, 0], [0.5, 0, 0.5]]),
        ]
        def fixture_archive(path, count):
            self.assertEqual(count, 3)
            with zipfile.ZipFile(path, "w") as archive:
                for index, crystal in enumerate(crystals):
                    archive.writestr(f"candidate_{index}.cif", str(CifWriter(crystal)))
        plan = self.service.prepare_plan({"conditions": {"chemical_system": "Fe-Ni-O"},
                                          "num_samples": 3, "batch_size": 3})
        self.assertEqual(plan["state"], "ready", plan["issues"])
        job, _ = self.service.store.create_job(plan, "fixed-cpu-triage-fixture", "demo", self.service.settings.snapshot())
        with patch("matpilot.worker.demo_archive", side_effect=fixture_archive):
            execute_job(self.service.store, self.service.store.claim(os.getpid()))
        self.assertEqual(self.service.get_job(job["job_id"])["status"], "completed")
        return job["job_id"]

    def registered(self, records, elements=("Fe", "Ni", "O"), priority=None):
        manifest = {"schema_version": 1, "expected_elements": list(elements),
                    "provenance": "synthetic_CPU_fixture_not_model_generated", "records": records}
        if priority is not None:
            manifest.update(priority_candidate_ids=priority, priority_selection_basis="explicit operator review preference")
        path = self.root / "collection.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        config = self.root / "config.json"
        config.write_text(json.dumps({"backend": "demo", "data_dir": str(self.root / "collection-jobs"),
            "candidate_collections": {"fixture": {"manifest_path": str(path),
                "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}), encoding="utf-8")
        return Service(Settings.load(str(config))), config

    def test_default_auto_executes_priority_filter_before_diversity_and_preserves_audit(self):
        job_id = self.fixture_job()
        screening = self.service.screen_candidates(job_id)
        self.assertEqual(screening["unique_valid_candidate_count"], 3)
        result = self.service.recommend_candidates(job_id=job_id, limit=3, include_all_records=True)
        self.assertEqual((result["requested_selection_policy"], result["effective_selection_policy"]),
                         ("auto", "fe_ni_o_relaxation_v1"))
        self.assertEqual(result["selected_count"], 1)
        self.assertFalse(result["target_met"])
        self.assertEqual(result["stage_counts"]["structural_representative_count"], 3)
        self.assertEqual(result["stage_counts"]["priority_relaxation_count"], 1)
        self.assertEqual(result["stage_counts"]["manual_review_count"], 1)
        self.assertEqual(result["stage_counts"]["defer_count"], 1)
        selected = [r for r in result["records"] if r["selected"]]
        self.assertEqual(selected[0]["relaxation_priority"]["priority"], "优先弛豫")
        self.assertTrue(all(r["structural_representative_qualified"] for r in result["records"]))

    def test_explicit_structural_replay_does_not_silently_apply_new_priority_gate(self):
        job_id = self.fixture_job()
        old = self.service.recommend_candidates(job_id=job_id, limit=3, selection_policy="structural_v1")
        self.assertEqual(old["selected_count"], 3)
        self.assertEqual(old["effective_selection_policy"], "structural_v1")
        self.assertNotIn("relaxation_triage_policy", old)

    def test_other_elements_fall_back_and_explicit_inapplicable_policy_is_rejected(self):
        crystal = Structure(Lattice.cubic(5.64), ["Na", "Cl"], [[0, 0, 0], [0.5, 0.5, 0.5]])
        path = self.root / "nacl.cif"
        path.write_text(str(CifWriter(crystal)), encoding="utf-8")
        service, _ = self.registered([{"candidate_id": "nacl", "path": str(path),
                                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}], ["Na", "Cl"])
        result = service.recommend_candidates(collection_id="fixture", limit=1)
        self.assertEqual(result["effective_selection_policy"], "structural_v1")
        self.assertEqual(result["selected_count"], 1)
        self.assertNotIn("relaxation_triage_policy", result)
        with self.assertRaisesRegex(ValueError, "only.*Fe-Ni-O"):
            service.recommend_candidates(collection_id="fixture", selection_policy="fe_ni_o_relaxation_v1")
        with self.assertRaisesRegex(ValueError, "selection_policy must"):
            service.recommend_candidates(collection_id="fixture", selection_policy="unregistered")
        self.assertEqual(service.store.jobs(), [])

    def test_job_and_registered_collection_apply_identical_policy_to_same_bytes(self):
        job_id = self.fixture_job()
        records = self.service.get_results(job_id)["records"]
        service, _ = self.registered(records)
        job = self.service.recommend_candidates(job_id=job_id, limit=3, include_all_records=True)
        collection = service.recommend_candidates(collection_id="fixture", limit=3, include_all_records=True)
        for key in ("records", "selected_candidate_ids", "stage_counts", "all_candidate_audit_sha256",
                    "effective_selection_policy", "relaxation_triage_policy"):
            self.assertEqual(job[key], collection[key], key)
        self.assertEqual(job["source"]["kind"], "job")
        self.assertEqual(collection["source"]["kind"], "registered_collection")

    def test_external_preference_cannot_promote_deferred_structure(self):
        job_id = self.fixture_job()
        analysis = self.service.recommend_candidates(job_id=job_id, include_all_records=True)
        deferred = next(r["candidate_id"] for r in analysis["records"] if r["relaxation_priority"]["tier"] == "defer")
        service, _ = self.registered(self.service.get_results(job_id)["records"], priority=[deferred])
        result = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True)
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["caller_priority_selector"]["candidate_ids"], [deferred])
        self.assertFalse(result["target_met"])

    def test_reports_keep_default_and_structural_artifacts_and_explain_all_tiers(self):
        job_id = self.fixture_job()
        default = self.service.build_report(job_id)
        structural = self.service.build_report(job_id, recommendation_limit=3, recommendation_policy="structural_v1")
        before = {result[key]: Path(result[key]).read_bytes() for result in [default, structural]
                  for key in ("report_path", "evidence_path")}
        automatic = self.service.build_report(job_id, recommendation_limit=3)
        self.assertEqual(len({default["report_path"], structural["report_path"], automatic["report_path"]}), 3)
        for path, data in before.items():
            self.assertEqual(Path(path).read_bytes(), data)
        evidence = json.loads(Path(automatic["evidence_path"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["recommendation"]["selected_count"], 1)
        report = Path(automatic["report_path"]).read_text(encoding="utf-8")
        for expected in ["优先弛豫", "人工复核", "暂缓", "启发式", "fe_ni_o_relaxation_v1"]:
            self.assertIn(expected, report)
        self.assertNotIn("recommendation", json.loads(before[default["evidence_path"]]))

    def test_cli_and_actual_fastmcp_expose_policy_and_run_the_full_pipeline(self):
        job_id = self.fixture_job()
        service, config = self.registered(self.service.get_results(job_id)["records"])
        output = io.StringIO()
        with patch("sys.argv", ["matpilot", "--config", str(config), "recommend-collection", "fixture",
                               "--limit", "3", "--selection-policy", "structural_v1"]), contextlib.redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue())["selected_count"], 3)
        capabilities = service.get_capabilities()["candidate_recommendation"]
        policy = capabilities["selection_policies"]["fe_ni_o_relaxation_v1"]
        self.assertEqual(policy["id"], "fe_ni_o_relaxation_v1")
        self.assertEqual(policy["penalties"]["minimum_distance_below_0_8_angstrom"], 50)
        self.assertTrue(policy["limitations"])
        async def exercise():
            app = make_server(service.settings)
            tool = next(t for t in await app.list_tools() if t.name == "recommend_candidates")
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertEqual(tool.inputSchema["properties"]["selection_policy"]["default"], "auto")
            result = await app.call_tool("recommend_candidates", {"collection_id": "fixture", "limit": 3})
            structured = result[1] if isinstance(result, tuple) else result
            if not isinstance(structured, dict):
                blocks = result[0] if isinstance(result, tuple) else result
                structured = json.loads(blocks[0].text)
            self.assertEqual(structured["selected_count"], 1)
            self.assertEqual(structured["effective_selection_policy"], "fe_ni_o_relaxation_v1")
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
