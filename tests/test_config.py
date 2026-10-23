import json
import tempfile
import unittest
from pathlib import Path

from matpilot.adapters.diffcsp import DiffCSPAdapter
from matpilot.config import Settings
from matpilot.service import Service


class SamplingPolicyTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_direct_configuration_rejects_invalid_or_expanded_caps(self):
        for cap in (0, -1, 33, 1024, True, False, 4.0, "4", None):
            with self.subTest(cap=cap), self.assertRaisesRegex(ValueError, "integer from 1 to 32"):
                Settings(data_dir=self.root, max_samples=cap)

    def test_file_configuration_cannot_expand_cap_or_create_data_directory(self):
        config = self.root / "config.json"
        data_dir = self.root / "not-created"
        for cap in (0, 33, 1024, True, 4.0, "4", None):
            config.write_text(json.dumps({"data_dir": str(data_dir), "max_samples": cap}), encoding="utf-8")
            with self.subTest(cap=cap), self.assertRaisesRegex(ValueError, "integer from 1 to 32"):
                Settings.load(str(config))
            self.assertFalse(data_dir.exists())

    def test_worker_snapshot_cannot_restore_an_expanded_cap(self):
        snapshot = Settings(data_dir=self.root, max_samples=4).snapshot()
        self.assertEqual(Settings(**snapshot).max_samples, 4)
        snapshot["max_samples"] = 1024
        with self.assertRaisesRegex(ValueError, "integer from 1 to 32"):
            Settings(**snapshot)

    def test_default_capability_and_plan_keep_sixteen_with_cap_thirty_two(self):
        service = Service(Settings(data_dir=self.root))
        capability = service.get_capabilities()
        self.assertEqual(capability["max_samples"], 32)
        self.assertEqual(capability["default_samples"], 16)
        plan = service.prepare_plan({"unconditional": True})
        self.assertEqual(plan["state"], "ready", plan["issues"])
        self.assertEqual(plan["num_samples"], 16)
        for count, state in ((32, "ready"), (33, "blocked")):
            plan = service.prepare_plan({"unconditional": True, "num_samples": count})
            self.assertEqual(plan["state"], state, plan["issues"])

    def test_reduced_operator_caps_are_loaded_advertised_and_enforced(self):
        for cap in (1, 4, 32):
            with self.subTest(cap=cap):
                config = self.root / f"config-{cap}.json"
                config.write_text(json.dumps({"data_dir": f"data-{cap}", "max_samples": cap}), encoding="utf-8")
                service = Service(Settings.load(str(config)))
                capability = service.get_capabilities()
                self.assertEqual(capability["max_samples"], cap)
                self.assertEqual(capability["default_samples"], min(16, cap))
                default_plan = service.prepare_plan({"unconditional": True})
                self.assertEqual(default_plan["state"], "ready", default_plan["issues"])
                self.assertEqual(default_plan["num_samples"], capability["default_samples"])
                self.assertEqual(default_plan["batch_size"], min(2, cap))
                request = {"unconditional": True, "num_samples": capability["default_samples"], "batch_size": min(2, cap)}
                plan = service.prepare_plan(request)
                self.assertEqual(plan["state"], "ready", plan["issues"])
                request["num_samples"] = cap + 1
                plan = service.prepare_plan(request)
                self.assertEqual(plan["state"], "blocked")
                self.assertTrue(any(issue["code"] == "invalid_budget" and issue["field"] == "num_samples" for issue in plan["issues"]))

    def test_explicit_batch_size_above_operator_limit_is_not_silently_accepted(self):
        service = Service(Settings(data_dir=self.root, max_samples=1))
        request = {"unconditional": True, "num_samples": 1, "batch_size": 2}
        plan = service.prepare_plan(request)
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["original_request"], request)
        self.assertTrue(any(issue["code"] == "invalid_budget" and issue["field"] == "batch_size" for issue in plan["issues"]))

    def test_diffcsp_uses_the_same_reduced_default_budget_policy(self):
        for cap in (1, 4):
            with self.subTest(cap=cap):
                plan = DiffCSPAdapter().validate({"model_id": "diffcsp", "conditions": {"chemical_formula": "NaCl", "space_group": 221}}, cap)
                # Template clarification is expected; shared budgeting must not add an invalid default.
                self.assertEqual(plan["state"], "needs_clarification")
                self.assertEqual(plan["num_samples"], cap)
                self.assertEqual(plan["batch_size"], min(2, cap))
                self.assertFalse(any(issue["code"] == "invalid_budget" for issue in plan["issues"]))


if __name__ == "__main__":
    unittest.main()
