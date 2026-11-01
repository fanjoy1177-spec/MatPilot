"""Only synthetic files and injected subprocess results; never import CHGNet."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from pymatgen.core import Lattice, Structure
from pymatgen.io.cif import CifWriter

from matpilot.evidence_ranking import build_evidence_rank_keys
from matpilot.predictors import (
    UNCERTAINTY, execute_static_prediction, prepare_static_prediction,
    predictor_config_schema, validate_predictor_config,
)


class PredictorContractTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        cif = self.root / "nacl.cif"
        cif.write_text(str(CifWriter(Structure(Lattice.cubic(5.64), ["Na", "Cl"], [[0, 0, 0], [.5, .5, .5]]))), encoding="utf-8")
        self.record = {"candidate_id": "synthetic_nacl", "structure_version_id": "synthetic_original",
                       "path": str(cif), "sha256": hashlib.sha256(cif.read_bytes()).hexdigest(),
                       "transformation": "generated", "parent_version_id": None}
        checkpoint = self.root / "synthetic_not_model.pth.tar"
        checkpoint.write_bytes(b"SYNTHETIC ONLY; this is deliberately not executable model data")
        self.config = {"enabled": True, "python_executable": sys.executable,
                       "checkpoint_path": str(checkpoint), "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
                       "checkpoint_source": "test fixture only; not model weights", "training_data_identity": "synthetic_no_training",
                       "scope": {"elements": ["Na", "Cl"], "material_classes": ["ionic_crystal"],
                                 "structure_states": ["original_generated"], "max_atoms": 16},
                       "validation_domain": {"status": "declared", "description": "Software fixtures only, no scientific validation", "source": "test_predictors.py"}}

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, config=None, **changes):
        return prepare_static_prediction(self.record, self.config if config is None else config, self.root / "plans",
                                         material_class=changes.get("material_class", "ionic_crystal"),
                                         structure_state=changes.get("structure_state", "original_generated"))

    def fake_runner(self, argv, **kwargs):
        self.assertFalse(kwargs["shell"])
        self.assertEqual(argv[1], "-I")
        manifest = json.loads(Path(argv[3]).read_text(encoding="utf-8"))
        plan, identity = manifest["input"], manifest["input"]["record"]
        output = {"schema_version": 1, "adapter_id": "chgnet_static_v1", "plan_sha256": manifest["plan_sha256"],
                  "package_version": "0.4.2", "checkpoint_sha256": plan["checkpoint"]["sha256"],
                  "candidate_id": identity["candidate_id"], "structure_version_id": identity["structure_version_id"],
                  "input_sha256": identity["sha256"], "task": "efs", "energy_per_atom": -2.5,
                  "forces": [[.3, .4, 0], [0, 0, .2]], "stress": [[-.8, 0, 0], [0, .1, 0], [0, 0, .4]],
                  "units": {"energy_per_atom": "eV/atom", "forces": "eV/angstrom", "stress": "GPa"},
                  "convergence": "not_applicable", "uncertainty": UNCERTAINTY,
                  "test_fixture_only": True}
        Path(argv[4]).write_text(json.dumps(output), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="synthetic test runner", stderr="")

    def test_default_configuration_is_disabled_without_importing_model(self):
        self.assertFalse(validate_predictor_config()["enabled"])
        with patch("matpilot.predictors.subprocess.run") as run:
            result = self.prepare(config={})
            self.assertEqual(result["status"], "disabled")
            self.assertNotIn("argv", result)
            run.assert_not_called()
        self.assertNotIn("chgnet", sys.modules)

    def test_prepare_records_precise_plan_without_running_subprocess(self):
        with patch("matpilot.predictors.subprocess.run") as run:
            plan = self.prepare()
            run.assert_not_called()
        self.assertEqual(plan["status"], "ready")
        self.assertFalse(plan["real_prediction_performed"])
        self.assertEqual(plan["input_manifest"]["operator_config"]["package_version"], "0.4.2")
        self.assertEqual(plan["argv"][0], str(Path(sys.executable).resolve()))
        self.assertEqual(plan["argv"], self.prepare()["argv"])
        self.assertEqual(plan["plan_sha256"], self.prepare()["plan_sha256"])

    def test_missing_checkpoint_and_interpreter_are_explicit_states(self):
        for config, status in [({**self.config, "checkpoint_path": str(self.root / "missing.pth")}, "weights_missing"),
                               ({**self.config, "python_executable": str(self.root / "python.exe")}, "environment_missing")]:
            with self.subTest(status=status):
                result = self.prepare(config=config)
                self.assertEqual(result["status"], status)
                self.assertNotIn("argv", result)

    def test_checkpoint_hash_mismatch_is_not_replaced_or_downloaded(self):
        result = self.prepare(config={**self.config, "checkpoint_sha256": "0" * 64})
        self.assertEqual(result["status"], "weights_mismatch")

    def test_actual_elements_and_declared_class_state_are_scope_gates(self):
        config = deepcopy(self.config)
        config["scope"]["elements"] = ["Si"]
        self.assertEqual(self.prepare(config=config)["status"], "unsupported")
        self.assertEqual(self.prepare(material_class="metal_alloy")["status"], "unsupported")
        self.assertEqual(self.prepare(structure_state="relaxed")["status"], "unsupported")
        config = deepcopy(self.config)
        config["scope"]["max_atoms"] = 1
        self.assertEqual(self.prepare(config=config)["status"], "unsupported")

    def test_unknown_training_or_validation_domain_requires_review(self):
        config = {**self.config, "training_data_identity": "unknown"}
        self.assertEqual(self.prepare(config=config)["status"], "needs_review")
        config = {**self.config, "validation_domain": {"status": "unknown", "description": "Unknown", "source": "unknown"}}
        self.assertEqual(self.prepare(config=config)["status"], "needs_review")

    def test_arbitrary_commands_unpinned_versions_or_calibrated_claims_are_rejected(self):
        for changes in [{"command": "anything"}, {"python_executable": "python --evil"},
                        {"package_version": "latest"}, {"checkpoint_model_version": "r2scan"},
                        {"uncertainty": {"kind": "calibrated"}}, {"enabled": "true"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_predictor_config({**self.config, **changes})

    def test_synthetic_subprocess_result_produces_version_bound_readiness_metrics(self):
        plan = self.prepare()
        result = execute_static_prediction(plan, self.config, self.root / "plans", runner=self.fake_runner)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["real_prediction_performed"])
        self.assertEqual(result["execution_source"], "injected_runner_contract_test")
        values = {m["property"]: m["value"] for m in result["metrics"]}
        self.assertEqual(values, {"energy_per_atom": -2.5, "max_force": .5, "max_abs_stress": .8})
        for metric in result["metrics"]:
            self.assertEqual(metric["convergence"], "not_applicable")
            self.assertIsNone(metric["reference_set"])
            self.assertEqual(metric["structure_version_id"], self.record["structure_version_id"])
        force = next(m for m in result["metrics"] if m["property"] == "max_force")
        evidence = build_evidence_rank_keys([self.record], {self.record["structure_version_id"]: result["metrics"]},
                                            {"property": "max_force", "unit": "eV/angstrom", "direction": "minimize"},
                                            [{k: force[k] for k in ("candidate_id", "structure_version_id", "metric_id")}])
        self.assertEqual(evidence["ranking_metadata"]["interpretation"], "calculation_readiness_only_not_stability")

    def test_execution_disabled_or_modified_config_never_calls_runner(self):
        plan = self.prepare()
        with patch("matpilot.predictors.subprocess.run") as run:
            for config in [{**self.config, "enabled": False}, {**self.config, "device": "cuda"}]:
                with self.assertRaises(ValueError):
                    execute_static_prediction(plan, config, self.root / "plans")
            run.assert_not_called()

    def test_modified_argv_output_path_or_worker_is_rejected_before_process(self):
        plan = self.prepare()
        for field, value in [("argv", [sys.executable, "-c", "print('bad')"]), ("output_path", str(self.root / "other.json"))]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                execute_static_prediction({**plan, field: value}, self.config, self.root / "plans", runner=self.fake_runner)
        Path(plan["worker_artifact"]["path"]).write_text("raise Exception('tampered')", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "immutable"):
            execute_static_prediction(plan, self.config, self.root / "plans", runner=self.fake_runner)

    def test_changed_input_or_weights_after_preparation_are_rejected(self):
        plan = self.prepare()
        Path(self.config["checkpoint_path"]).write_bytes(b"changed synthetic checkpoint")
        with self.assertRaises(ValueError):
            execute_static_prediction(plan, self.config, self.root / "plans", runner=self.fake_runner)
        Path(self.record["path"]).write_text("changed structure", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            execute_static_prediction(plan, self.config, self.root / "plans", runner=self.fake_runner)

    def test_foreign_output_identity_is_never_registered(self):
        def wrong(argv, **kwargs):
            complete = self.fake_runner(argv, **kwargs)
            path = Path(argv[4])
            data = json.loads(path.read_text(encoding="utf-8"))
            data["candidate_id"] = "different_candidate"
            path.write_text(json.dumps(data), encoding="utf-8")
            return complete
        with self.assertRaisesRegex(ValueError, "identity"):
            execute_static_prediction(self.prepare(), self.config, self.root / "plans", runner=wrong)

    def test_process_failure_and_timeout_are_reported_without_metrics(self):
        plan = self.prepare()
        failed = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="synthetic failure")
        result = execute_static_prediction(plan, self.config, self.root / "plans", runner=failed)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metrics"], [])
        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        result = execute_static_prediction(plan, self.config, self.root / "plans", runner=timeout)
        self.assertEqual(result["status"], "failed")

    def test_existing_output_is_not_overwritten(self):
        plan = self.prepare()
        execute_static_prediction(plan, self.config, self.root / "plans", runner=self.fake_runner)
        with self.assertRaisesRegex(ValueError, "not be overwritten"):
            execute_static_prediction(plan, self.config, self.root / "plans", runner=self.fake_runner)

    def test_contract_does_not_claim_band_gap_or_hull_prediction(self):
        contract = predictor_config_schema()
        self.assertIn("energy_above_hull", contract["not_provided"])
        self.assertIn("band_gap", contract["not_provided"])
        self.assertEqual(contract["uncertainty"]["kind"], "not_calibrated")


if __name__ == "__main__":
    unittest.main()
