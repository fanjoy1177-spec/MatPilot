import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import zipfile

from matpilot.config import Settings
from matpilot.service import Service
from matpilot.templates import resolve_template
from matpilot.structures import demo_archive, collect_archive
from matpilot.worker import execute_job
from matpilot.bridges.diffcsp_runner import run, collect_outputs, verify_weights


class MultiModelTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        demo_archive(self.root / "fixture.zip", 1)
        with zipfile.ZipFile(self.root / "fixture.zip") as z:
            content = z.read(z.namelist()[0])
        self.cif = self.root / "fixture.cif"
        self.cif.write_bytes(content)
        self.settings = Settings(data_dir=self.root / "jobs", templates={"fixture": {
            "path": str(self.cif), "sha256": hashlib.sha256(content).hexdigest(),
            "source": "programmatic development fixture; not a scientific template", "demo_only": True,
            "review": {"by": "automated test", "date": "2026-10-22", "scope": "software fixture only"}}})
        self.service = Service(self.settings)
        self.request = {"model_id": "diffcsp", "template_id": "fixture", "conditions": {"chemical_formula": "NaCl", "space_group": 221}, "num_samples": 2, "batch_size": 2}

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, plan, settings=None):
        values = settings or self.settings.snapshot()
        job, _ = self.service.store.create_job(plan, "test-multi-model", values["backend"], values)
        claimed = self.service.store.claim(os.getpid())
        execute_job(self.service.store, claimed)
        return self.service.get_job(job["job_id"])

    def test_template_derives_actual_orbits_and_source(self):
        template = resolve_template("fixture", self.settings)
        self.assertEqual(template["space_group"], 221)
        self.assertEqual(template["chemical_formula"], "NaCl")
        self.assertEqual(sorted(template["wyckoff_letters"]), ["1a", "1b"])
        self.assertEqual(template["site_count"], 2)
        self.assertTrue(template["demo_only"])

    def test_ready_plan_maps_formula_and_template_then_checks_results(self):
        plan = self.service.prepare_plan(self.request)
        self.assertEqual(plan["state"], "ready", plan["issues"])
        self.assertEqual(len(plan["requirements"]), 4)
        job = self.execute(plan)
        result = self.service.screen_candidates(job["job_id"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["valid_candidate_count"], 2)
        self.assertEqual(result["unique_valid_candidate_count"], 1)
        self.assertEqual(result["provenance"], "demonstration_fixture")

    def test_no_template_does_not_guess_wyckoff(self):
        request = dict(self.request)
        request.pop("template_id")
        plan = self.service.prepare_plan(request)
        self.assertNotEqual(plan["state"], "ready")
        self.assertIsNone(plan["template"])
        self.assertIn("provide_template", [o["kind"] for o in plan["repair_options"]])

    def test_wrong_formula_or_symmetry_never_substituted(self):
        for prop, value in [("chemical_formula", "Na2Cl"), ("space_group", 225)]:
            request = {**self.request, "conditions": {**self.request["conditions"], prop: value}}
            plan = self.service.prepare_plan(request)
            self.assertEqual(plan["state"], "blocked")
            self.assertIn("template_mismatch", [i["code"] for i in plan["issues"]])

    def test_template_mutation_blocks_preparation_and_submission(self):
        plan = self.service.prepare_plan(self.request)
        self.cif.write_text("changed", encoding="utf-8")
        self.assertEqual(self.service.prepare_plan(self.request)["state"], "blocked")
        with patch("matpilot.service.launch_worker") as launch:
            with self.assertRaises(ValueError):
                self.service.submit_job(plan["plan_id"], "test-mutated-template", plan["plan_id"])
            launch.assert_not_called()

    def test_no_real_execution_from_demo_template(self):
        self.settings.backend = "native"
        self.assertEqual(self.service.prepare_plan(self.request)["state"], "blocked")

    def test_compute_gate_before_submitting_or_loading_model(self):
        settings = Settings(data_dir=self.root / "disabled", backend="native")
        service = Service(settings)
        plan = service.prepare_plan({"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 2})
        with patch("matpilot.service.launch_worker") as launch:
            with self.assertRaisesRegex(ValueError, "disabled"):
                service.submit_job(plan["plan_id"], "blocked-compute", plan["plan_id"])
            launch.assert_not_called()
        with patch("matpilot.bridges.diffcsp_runner.verify_checkout") as checkout:
            with self.assertRaisesRegex(ValueError, "disabled"):
                run({"real_compute_enabled": False})
            checkout.assert_not_called()

    def test_no_unsupported_property_silently_removed(self):
        request = {**self.request, "conditions": {**self.request["conditions"], "dft_band_gap": 2}}
        plan = self.service.prepare_plan(request)
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["original_request"]["conditions"]["dft_band_gap"], 2)

    def test_capability_card_does_not_claim_real_readiness(self):
        caps = self.service.get_capabilities()
        self.assertEqual({c["model_id"] for c in caps["models"]}, {"mattergen", "diffcsp"})
        for card in caps["models"]:
            self.assertEqual(card["availability"]["status"], "demo_only")
            self.assertEqual(card["availability"]["real_model_acceptance"], "not_verified_for_current_release")

    def test_short_return_preserves_candidates_and_charges_full_attempt(self):
        plan = self.service.prepare_plan(self.request)
        def short(path, count):
            demo_archive(path, 1)
        with patch("matpilot.worker.demo_archive", side_effect=short):
            job = self.execute(plan)
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["completed_samples"], 1)
        self.assertEqual(job["sampling_attempts_charged"], 2)
        self.assertEqual(len(self.service.get_results(job["job_id"])["records"]), 1)
        with self.assertRaisesRegex(ValueError, "budget"):
            self.service.store.control(job["job_id"], "resume")

    def test_invalid_cif_retains_valid_siblings_and_error(self):
        archive = self.root / "mixed.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("ok.cif", self.cif.read_bytes())
            z.writestr("bad.cif", "invalid data")
        errors = []
        result = collect_archive(archive, self.root / "collected", 2, None, allow_partial=True, errors=errors)
        self.assertEqual(len(result), 1)
        self.assertEqual(errors[0]["source_member"], "bad.cif")

    def test_bridge_rejects_outputs_outside_job_and_duplicate_paths(self):
        output = self.root / "output"
        output.mkdir()
        with self.assertRaises(ValueError):
            collect_outputs({"structures": [str(self.cif)], "num_generated": 1}, output, 1)
        child = output / "one.cif"
        child.write_bytes(self.cif.read_bytes())
        with self.assertRaises(ValueError):
            collect_outputs({"structures": [str(child), str(child)], "num_generated": 2}, output, 2)
        self.assertEqual(collect_outputs({"structures": [str(child)], "num_generated": 1}, output, 2), 1)

    def test_weight_manifest_covers_actual_selection_and_scalers(self):
        model = self.root / "weights"
        model.mkdir()
        for name in ["last.ckpt", "hparams.yaml", "lattice_scaler.pt"]:
            (model / name).write_bytes(b"FAKE TEST BYTES - NEVER DESERIALIZED")
        manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in model.iterdir()}
        verify_weights(model, manifest)
        (model / "extra.ckpt").write_bytes(b"fake")
        with self.assertRaises(ValueError):
            verify_weights(model, manifest)

    def test_repairs_are_explicit_unapplied_and_budget_preserving(self):
        request = {"conditions": {"dft_band_gap": {"value": 2, "relation": "at_least", "unit": "eV"}}, "num_samples": 2}
        plan = self.service.prepare_plan(request)
        options = plan["repair_options"]
        weaken = next(o for o in options if o["kind"] == "weaken_guarantee")
        self.assertEqual(weaken["requirement_changes"][0]["before"]["relation"], "at_least")
        self.assertTrue(all(not o["auto_apply"] and o["sampling_cap_change"] == 0 for o in options))
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["original_request"], request)

    def test_template_origin_mismatch_rejected_before_model_import(self):
        import types
        from pymatgen.core.operations import SymmOp
        from matpilot.bridges.diffcsp_runner import verify_symmetry
        module = types.ModuleType("pyxtal.symmetry")
        class GroupFixture:
            def __init__(self, number):
                self.number = number
            def __getitem__(self, letter):
                point = "0,0,0" if letter == "a" else "1/2,1/2,1/2"
                return types.SimpleNamespace(ops=[SymmOp.from_xyz_str(point)])
        module.Group = GroupFixture
        template = resolve_template("fixture", self.settings)
        with patch.dict("sys.modules", {"pyxtal.symmetry": module}):
            verify_symmetry(template)
            template["orbit_fractional_positions"][0] = [[.25,.25,.25]]
            with self.assertRaisesRegex(ValueError, "origin"):
                verify_symmetry(template)

    def test_an_unknown_metric_method_or_unit_is_not_usable(self):
        from matpilot.evidence import metric_usable
        metric = {"property": "dft_band_gap", "value": 2, "unit": " Unknown ", "method": {"name": "test", "version": "1", "weights_identity": "test", "parameters": {}}, "convergence": "converged"}
        self.assertFalse(metric_usable(metric))
        metric["unit"] = "eV"
        metric["method"]["version"] = " UNKNOWN "
        self.assertFalse(metric_usable(metric))
        metric["method"]["version"] = "1"
        metric.update(property="energy_above_hull", reference_set={"id": "unknown", "sha256": "a" * 64})
        self.assertFalse(metric_usable(metric))

    def test_corrupt_old_artifact_does_not_prevent_failure_status_update(self):
        plan = self.service.prepare_plan(self.request)
        job = self.execute(plan)
        Path(job["batches"][0]["records"][0]["path"]).write_text("changed")
        updated = self.service.store.update(job["job_id"], status="failed", batches=job["batches"], error="tampered")
        self.assertEqual(updated["status"], "failed")
        with self.assertRaises(ValueError):
            self.service.get_results(job["job_id"])

    def test_wrapper_identity_is_portable_but_rejects_source_changes(self):
        from matpilot.bridges.diffcsp_runner import verify_wrapper_source
        source = self.root / "wrapper.py"
        canonical = b"# fixture\nvalue = 1\n"
        identity = hashlib.sha256(canonical).hexdigest()
        for content in (canonical, canonical.replace(b"\n", b"\r\n")):
            source.write_bytes(content)
            verify_wrapper_source(source, identity)
        source.write_bytes(canonical.replace(b"1", b"2"))
        with self.assertRaisesRegex(ValueError, "source differs"):
            verify_wrapper_source(source, identity)


if __name__ == "__main__":
    unittest.main()
