"""Production API routing tests using synthetic, CPU-only structural fixtures.

Formal-charge examples test the declared finite-rule implementation; these CIFs
are not claimed to be experimental or relaxed materials.
"""
import asyncio
import contextlib
import hashlib
import io
import itertools
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pymatgen.core import Composition, Lattice, Structure
from pymatgen.io.cif import CifWriter

from matpilot.cli import main
from matpilot.config import Settings
from matpilot.server import make_server
from matpilot.service import Service


class MixedValenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def collection(self, formula, *, token="fixture"):
        composition = Composition(formula)
        species = []
        for element, count in composition.items():
            self.assertEqual(int(count), count)
            species.extend([element.symbol] * int(count))
        grid = math.ceil(len(species) ** (1 / 3))
        while grid ** 3 < len(species):
            grid += 1
        side = max(4.0, (20.0 * len(species)) ** (1 / 3))
        coords = list(itertools.product([i / grid for i in range(grid)], repeat=3))[:len(species)]
        crystal = Structure(Lattice.cubic(side), species, coords)
        cif = self.root / (token + ".cif")
        cif.write_text(str(CifWriter(crystal, symprec=None)), encoding="utf-8")
        record = {"candidate_id": token, "path": str(cif.resolve()),
                  "sha256": hashlib.sha256(cif.read_bytes()).hexdigest()}
        manifest = self.root / (token + ".json")
        manifest.write_text(json.dumps({"schema_version": 1,
            "expected_elements": sorted(set(species)),
            "provenance": "synthetic_CPU_API_fixture_not_property_evidence", "records": [record]}), encoding="utf-8")
        config = self.root / (token + "-config.json")
        config.write_text(json.dumps({"data_dir": str(self.root / (token + "-jobs")),
            "real_compute_enabled": False, "candidate_collections": {token: {
                "manifest_path": str(manifest.resolve()),
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}}}), encoding="utf-8")
        return Service(Settings.load(str(config))), config

    def test_historical_auto_routes_oxide_halide_sulfide_and_nitride_by_supported_scope(self):
        for formula in ["Fe3O4", "Co3O4", "NiO", "Mn3O4", "LiCoO2", "NaCl", "FeS", "Li3N"]:
            with self.subTest(formula=formula):
                service, _ = self.collection(formula, token=formula)
                result = service.recommend_candidates(collection_id=formula, limit=1, include_all_records=True, selection_policy="auto")
                self.assertEqual(result["effective_selection_policy"], "ionic_mixed_valence_v1")
                self.assertFalse(result["selection_policy_resolution"]["fallback_applied"])
                self.assertIsNone(result["fallback_reason"])
                self.assertFalse(result["selection_policy_resolution"]["property_prediction_performed"])
                self.assertEqual(result["stage_counts"]["structural_representative_count"], 1)
                row = result["records"][0]
                charge = row["relaxation_priority"]["metrics"]["charge_feasibility"]
                self.assertEqual(charge["status"], "feasible")
                self.assertEqual(service.store.jobs(), [])

    def test_mixed_fenio_reaches_priority_but_legacy_replay_still_requests_review(self):
        service, _ = self.collection("Fe3Ni3O7")
        automatic = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True, selection_policy="auto")
        legacy = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True,
                                             selection_policy="fe_ni_o_relaxation_v1")
        explicit = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True,
                                               selection_policy="ionic_mixed_valence_v1")
        self.assertEqual(automatic["selected_count"], 1)
        self.assertEqual(legacy["selected_count"], 0)
        self.assertEqual(legacy["records"][0]["relaxation_priority"]["tier"], "manual_review")
        self.assertEqual(automatic["records"], explicit["records"])
        self.assertEqual(automatic["all_candidate_audit_sha256"], explicit["all_candidate_audit_sha256"])
        self.assertEqual(service.store.jobs(), [])

    def test_unsupported_metal_and_multianion_systems_fall_back_without_charge_claims(self):
        for formula in ["FeNi", "LiOF", "LiPO4"]:
            with self.subTest(formula=formula):
                service, _ = self.collection(formula, token=formula)
                automatic = service.recommend_candidates(collection_id=formula, limit=1, include_all_records=True, selection_policy="auto")
                structural = service.recommend_candidates(collection_id=formula, limit=1, include_all_records=True,
                                                         selection_policy="structural_v1")
                self.assertEqual(automatic["effective_selection_policy"], "structural_v1")
                self.assertTrue(automatic["selection_policy_resolution"]["fallback_applied"])
                self.assertFalse(automatic["selection_policy_resolution"]["chemical_feasibility_policy_invoked"])
                self.assertFalse(automatic["selection_policy_resolution"]["property_prediction_performed"])
                self.assertIn("chemical_feasibility_not_assessed", automatic["fallback_reason"])
                self.assertNotIn("relaxation_triage_policy", automatic)
                self.assertNotIn("relaxation_priority", automatic["records"][0])
                self.assertEqual(automatic["records"], structural["records"])
                self.assertFalse(structural["selection_policy_resolution"]["fallback_applied"])
                with self.assertRaisesRegex(ValueError, "does not support"):
                    service.recommend_candidates(collection_id=formula, selection_policy="ionic_mixed_valence_v1")

    def test_caller_preference_cannot_promote_model_no_solution_or_fill_shortfall(self):
        service, _ = self.collection("FeNiO")
        registration = service.settings.candidate_collections["fixture"]
        manifest_path = Path(registration["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(priority_candidate_ids=["fixture"],
                        priority_selection_basis="operator asks to consider this candidate")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        registration["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        result = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True, selection_policy="auto")
        self.assertEqual(result["selected_count"], 0)
        self.assertFalse(result["target_met"])
        self.assertEqual(result["stage_counts"]["shortfall_count"], 1)
        self.assertEqual(result["caller_priority_selector"]["candidate_ids"], ["fixture"])
        priority = result["records"][0]["relaxation_priority"]
        self.assertEqual(priority["tier"], "manual_review")
        self.assertIsNone(priority["score"])
        self.assertEqual(priority["metrics"]["charge_feasibility"]["status"], "no_solution")

    def test_cli_and_mcp_explicit_new_policy_are_actual_executable_paths(self):
        service, config = self.collection("Fe3Ni3O7")
        output = io.StringIO()
        with patch("sys.argv", ["matpilot", "--config", str(config), "recommend-collection", "fixture",
                               "--limit", "1", "--selection-policy", "ionic_mixed_valence_v1",
                               "--include-all-records"]), contextlib.redirect_stdout(output):
            main()
        cli_result = json.loads(output.getvalue())
        self.assertEqual(cli_result["effective_selection_policy"], "ionic_mixed_valence_v1")
        self.assertEqual(cli_result["selected_count"], 1)
        capabilities = service.get_capabilities()["candidate_recommendation"]
        self.assertIn("ionic_mixed_valence_v1", capabilities["selection_policies"])
        self.assertIn("fallback", capabilities["selection_policies"]["auto"]["routing"])

        async def exercise():
            app = make_server(service.settings)
            tools = await app.list_tools()
            tool = next(t for t in tools if t.name == "recommend_candidates")
            self.assertTrue(tool.annotations.readOnlyHint)
            result = await app.call_tool("recommend_candidates", {"collection_id": "fixture", "limit": 1,
                "selection_policy": "ionic_mixed_valence_v1", "include_all_records": True})
            structured = result[1] if isinstance(result, tuple) else result
            if not isinstance(structured, dict):
                blocks = result[0] if isinstance(result, tuple) else result
                structured = json.loads(blocks[0].text)
            self.assertEqual(structured["selected_candidate_ids"], cli_result["selected_candidate_ids"])
            self.assertEqual(structured["all_candidate_audit_sha256"], cli_result["all_candidate_audit_sha256"])
            self.assertEqual(structured["effective_selection_policy"], "ionic_mixed_valence_v1")
        asyncio.run(exercise())
        self.assertEqual(service.store.jobs(), [])


if __name__ == "__main__":
    unittest.main()
