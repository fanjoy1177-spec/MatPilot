"""Production recommendation integration using fixed CPU CIF fixtures only."""
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

from pymatgen.core import Lattice, Structure
from pymatgen.io.cif import CifWriter

from matpilot.cli import main
from matpilot.config import Settings
from matpilot.server import make_server
from matpilot.service import Service
from matpilot.worker import execute_job


class RecommendationIntegrationTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.service = Service(Settings(data_dir=self.root / "jobs", backend="demo"))

    def tearDown(self):
        self.temp.cleanup()

    def job(self, request=None):
        plan = self.service.prepare_plan(request or {"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 2})
        self.assertEqual(plan["state"], "ready", plan["issues"])
        job, _ = self.service.store.create_job(plan, "fixture-job", "demo", self.service.settings.snapshot())
        execute_job(self.service.store, self.service.store.claim(os.getpid()))
        return plan, job["job_id"]

    def collection(self):
        crystal = Structure(Lattice.cubic(5.64), ["Na", "Cl"], [[0, 0, 0], [0.5, 0.5, 0.5]])
        cif = self.root / "candidate.cif"
        cif.write_text(str(CifWriter(crystal)), encoding="utf-8")
        manifest = {"schema_version": 1, "expected_elements": ["Na", "Cl"],
                    "provenance": "synthetic_cpu_fixture_archived_separately_from_generation_backend",
                    "records": [{"candidate_id": "fixture-1", "path": "candidate.cif",
                                 "sha256": hashlib.sha256(cif.read_bytes()).hexdigest()}],
                    "priority_candidate_ids": ["fixture-1"], "priority_selection_basis": "explicit fixture operator selection"}
        path = self.root / "collection.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        config = self.root / "config.json"
        config.write_text(json.dumps({"backend": "demo", "data_dir": "registry-data", "candidate_collections": {
            "registered-fixture": {"manifest_path": "collection.json", "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}), encoding="utf-8")
        return Service(Settings.load(str(config))), path, cif, config

    def test_registered_collection_checks_identity_and_keeps_own_provenance(self):
        service, path, cif, config = self.collection()
        self.assertEqual(service.get_capabilities()["candidate_recommendation"]["collection_ids"], ["registered-fixture"])
        self.assertEqual(service.settings.candidate_collections["registered-fixture"]["manifest_path"], str(path))
        result = service.recommend_candidates(collection_id="registered-fixture", limit=1)
        self.assertEqual(result["selected_candidate_ids"], ["fixture-1"])
        self.assertEqual(result["source"]["kind"], "registered_collection")
        self.assertNotEqual(result["source"]["provenance"], "demonstration_fixture")
        self.assertIn("explicit_caller_preference", result["priority_selector"]["evidence_scope"])
        self.assertFalse(result["include_all_records"])
        self.assertEqual(len(result["all_candidate_audit_sha256"]), 64)
        cif.write_text(cif.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        changed = service.recommend_candidates(collection_id="registered-fixture")
        self.assertEqual(changed["selected_count"], 0)
        self.assertEqual(changed["excluded_reason_counts"], {"artifact_sha256_mismatch": 1})
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "manifest SHA256 mismatch"):
            service.recommend_candidates(collection_id="registered-fixture")

    def test_unknown_arbitrary_and_conflicting_sources_are_rejected(self):
        for arguments in ({}, {"job_id": "x", "collection_id": "x"}, {"collection_id": str(self.root)}, {"collection_id": "missing"}):
            with self.assertRaises(ValueError):
                self.service.recommend_candidates(**arguments)
        with self.assertRaises(ValueError):
            Settings(data_dir=self.root, candidate_collections={"x": {"manifest_path": "foo", "manifest_sha256": "bad"}})

    def test_job_recommendation_compact_audit_and_no_unconditional_guess(self):
        _, job_id = self.job()
        compact = self.service.recommend_candidates(job_id=job_id, limit=2)
        full = self.service.recommend_candidates(job_id=job_id, limit=2, include_all_records=True)
        self.assertEqual(compact["selected_count"], 1)
        self.assertEqual(len(compact["records"]), 1)
        self.assertEqual(len(full["records"]), 2)
        self.assertEqual(compact["all_candidate_audit_sha256"], full["all_candidate_audit_sha256"])
        self.assertEqual(compact["source"]["provenance"], "demonstration_fixture")
        plan = self.service.store.plan(self.service.store.job(job_id)["plan_id"])
        plan["conditions"] = {}
        with patch.object(self.service.store, "plan", return_value=plan), self.assertRaisesRegex(ValueError, "cannot be guessed"):
            self.service.recommend_candidates(job_id=job_id)

    def test_unknown_hard_requirement_cannot_enter_job_shortlist(self):
        plan, job_id = self.job()
        # Simulate reading a legacy/imported plan with an unresolved hard result
        # requirement. Real per-requirement screening still runs, without writing
        # a weakened plan or bypassing the public submission policy.
        plan["requirements"].append({"requirement_id": "unmeasured", "kind": "numeric_bound",
            "property": "dft_band_gap", "normalized_value": 1.0, "normalized_unit": "eV",
            "strength": "hard", "relation": "at_least"})
        with patch.object(self.service.store, "plan", return_value=plan):
            screening = self.service.screen_candidates(job_id)
            self.assertEqual(screening["records"][0]["requirement_checks"][-1]["status"], "unknown")
            result = self.service.recommend_candidates(job_id=job_id)
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["excluded_reason_counts"]["existing_hard_requirement_checks_not_passed"], 2)

    def test_recommendation_report_isolated_from_default_and_metric_selection_reports(self):
        _, job_id = self.job()
        ordinary = self.service.build_report(job_id)
        before = {key: Path(ordinary[key]).read_bytes() for key in ("report_path", "evidence_path")}
        recommendation = self.service.build_report(job_id, recommendation_limit=2)
        self.assertIn("recommendations", Path(recommendation["report_path"]).parts)
        self.assertEqual(Path(recommendation["report_path"]).parent.name, "2")
        evidence = json.loads(Path(recommendation["evidence_path"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["recommendation"]["selected_count"], 1)
        self.assertEqual(len(evidence["recommendation"]["records"]), 2)
        for key, content in before.items():
            self.assertEqual(Path(ordinary[key]).read_bytes(), content)
        screened = self.service.screen_candidates(job_id)
        screened["metric_selection_audit"] = {"selection_sha256": "fixture-selection", "selections": [], "unselected_metric_ids": []}
        with patch.object(self.service, "screen_candidates", return_value=screened):
            selected = self.service.build_report(job_id, metric_selections=[])
            selected_bytes = Path(selected["evidence_path"]).read_bytes()
            nested = self.service.build_report(job_id, metric_selections=[], recommendation_limit=1)
        self.assertEqual(Path(nested["evidence_path"]).parent.parent.parent, Path(selected["evidence_path"]).parent)
        self.assertEqual(Path(selected["evidence_path"]).read_bytes(), selected_bytes)

    def test_cli_and_actual_fastmcp_discover_and_call_read_only_tool(self):
        service, _, _, config = self.collection()
        output = io.StringIO()
        with patch("sys.argv", ["matpilot", "--config", str(config), "recommend-collection", "registered-fixture", "--limit", "1"]), contextlib.redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue())["selected_count"], 1)

        async def exercise():
            app = make_server(service.settings)
            tools = await app.list_tools()
            self.assertEqual(len(tools), 10)
            tool = next(t for t in tools if t.name == "recommend_candidates")
            self.assertTrue(tool.annotations.readOnlyHint)
            result = await app.call_tool("recommend_candidates", {"collection_id": "registered-fixture", "limit": 1})
            # FastMCP returns both text blocks and structured data in this version.
            structured = result[1] if isinstance(result, tuple) else result
            if isinstance(structured, dict) and "selected_count" in structured:
                self.assertEqual(structured["selected_count"], 1)
            else:
                blocks = result[0] if isinstance(result, tuple) else result
                self.assertEqual(json.loads(blocks[0].text)["selected_count"], 1)
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
