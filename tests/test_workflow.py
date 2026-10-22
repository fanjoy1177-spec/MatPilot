import concurrent.futures
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from matpilot.adapters.mattergen import MatterGenAdapter
from matpilot.config import Settings
from matpilot.service import Service
from matpilot.worker import execute_job


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.adapter = MatterGenAdapter()

    def plan(self, conditions=None, **kw):
        return self.adapter.validate({"conditions": conditions or {}, **kw})

    def test_joint_conditions(self):
        p = self.plan({"chemical_system": "Ni-Fe-O", "energy_above_hull": 0.05})
        self.assertEqual(p["state"], "ready")
        self.assertEqual(p["checkpoint"], "chemical_system_energy_above_hull")
        self.assertEqual(p["conditions"]["chemical_system"], "Fe-Ni-O")

    def test_conflict_never_drops_gap(self):
        request = {"chemical_system": "Fe-O", "dft_band_gap": 2}
        p = self.plan(request)
        self.assertEqual(p["state"], "blocked")
        self.assertEqual(p["conditions"], request)

    def test_units(self):
        p = self.plan({"chemical_system": "Fe-O", "energy_above_hull": {"value": 50, "unit": "meV/atom"}})
        self.assertEqual(p["conditions"]["energy_above_hull"], 0.05)

    def test_bounds_require_clarification(self):
        p = self.plan({"dft_band_gap": {"value": 2, "relation": "at_least"}})
        self.assertEqual(p["state"], "blocked")

    def test_formula_not_element_set(self):
        self.assertEqual(self.plan({"chemical_system": "LiFePO4"})["state"], "blocked")

    def test_unconditional_must_be_explicit(self):
        self.assertEqual(self.plan()["state"], "needs_clarification")
        self.assertEqual(self.plan(unconditional=True)["state"], "ready")

    def test_nonfinite_and_boolean_not_numbers(self):
        for value in [True, -1, float("inf"), "low"]:
            with self.subTest(value=value):
                # Invalid nonfinite input is rejected rather than serialized as a valid plan.
                if value == float("inf"):
                    with self.assertRaises(ValueError):
                        self.plan({"dft_band_gap": value})
                else:
                    self.assertEqual(self.plan({"dft_band_gap": value})["state"], "blocked")

    def test_unknown_fields_preserved_for_review(self):
        p = self.plan({"chemical_system": "Na-Cl"}, requested_performance="good catalyst")
        self.assertEqual(p["state"], "blocked")

    def test_qualitative_target_not_invented(self):
        p = self.plan({"chemical_system": "Fe-O"}, clarifications=["较稳定对应哪个条件目标？"])
        self.assertEqual(p["state"], "needs_clarification")
        self.assertNotIn("energy_above_hull", p["conditions"])

    def test_budget_and_fractional_space_group(self):
        self.assertEqual(self.plan({"space_group": 2.5})["state"], "blocked")
        self.assertEqual(self.plan({"space_group": 1}, num_samples=33)["state"], "blocked")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.service = Service(Settings(data_dir=Path(self.temp.name)))
        self.plan = self.service.prepare_plan({"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 3, "batch_size": 2})

    def tearDown(self):
        self.temp.cleanup()

    def create(self, key="test-request-1", **settings):
        values = self.service.settings.snapshot()
        values.update(settings)
        return self.service.store.create_job(self.plan, key, values["backend"], values)[0]

    def execute(self, job):
        claimed = self.service.store.claim(os.getpid())
        self.assertEqual(claimed["job_id"], job["job_id"])
        execute_job(self.service.store, claimed)
        return self.service.get_job(job["job_id"])

    def test_demo_counts_last_short_batch_and_report(self):
        job = self.execute(self.create())
        self.assertEqual(job["status"], "completed")
        self.assertEqual([b["count"] for b in job["batches"]], [2, 1])
        self.assertEqual(job["completed_samples"], 3)
        self.assertEqual(job["provenance"], "demonstration_fixture")
        report = self.service.build_report(job["job_id"])
        content = json.loads(Path(report["evidence_path"]).read_text(encoding="utf-8"))
        self.assertEqual(content["screening"]["count"], 3)

    def test_concurrent_duplicate_submissions(self):
        with patch("matpilot.service.launch_worker") as launch:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                jobs = list(pool.map(lambda _: self.service.submit_job(self.plan["plan_id"], "same-key-123", self.plan["plan_id"]), range(8)))
        self.assertEqual(len({j["job_id"] for j in jobs}), 1)
        self.assertEqual(launch.call_count, 1)

    def test_wrong_approval_and_key_reuse(self):
        with self.assertRaises(ValueError):
            self.service.submit_job(self.plan["plan_id"], "test-request-1", "wrong")
        self.create()
        other = self.service.prepare_plan({"unconditional": True, "num_samples": 1})
        with self.assertRaises(ValueError):
            self.service.store.create_job(other, "test-request-1", "demo", self.service.settings.snapshot())

    def test_plan_integrity_and_blocked_start(self):
        p = self.service.prepare_plan({"conditions": {"chemical_system": "Fe-O", "dft_band_gap": 2}})
        with self.assertRaises(ValueError):
            self.service.submit_job(p["plan_id"], "test-blocked", p["plan_id"])
        with self.service.store.connect() as db:
            altered = dict(self.plan, num_samples=32)
            db.execute("UPDATE plans SET payload=? WHERE id=?", (json.dumps(altered), self.plan["plan_id"]))
        with self.assertRaises(ValueError):
            self.service.store.plan(self.plan["plan_id"])

    def test_backend_change_requires_new_plan(self):
        self.service.settings.backend = "native"
        with self.assertRaises(ValueError):
            self.service.submit_job(self.plan["plan_id"], "changed-backend", self.plan["plan_id"])

    def test_partial_failure_is_not_completed(self):
        from matpilot.structures import demo_archive
        def fail_second(path, count):
            if "batch_0001" in str(path):
                raise RuntimeError("Injected second-batch failure")
            demo_archive(path, count)
        with patch("matpilot.worker.demo_archive", side_effect=fail_second):
            job = self.execute(self.create())
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["completed_samples"], 2)
        # v1.1 specification treats the cap as attempted sampling, not only valid output.
        self.assertEqual(job["sampling_attempts_charged"], 3)
        with self.assertRaisesRegex(ValueError, "budget"):
            self.service.store.control(job["job_id"], "resume")

    def test_tampered_result_blocks_resume_and_report(self):
        job = self.execute(self.create())
        file = Path(job["batches"][0]["records"][0]["path"])
        file.write_text("tampered")
        with self.assertRaises(ValueError):
            self.service.get_results(job["job_id"])

    def test_stop_queued_and_resume_running_forbidden(self):
        job = self.create()
        self.assertEqual(self.service.store.control(job["job_id"], "stop")["status"], "stopped")
        self.service.store.control(job["job_id"], "resume")
        self.service.store.claim(os.getpid())
        with self.assertRaises(ValueError):
            self.service.store.control(job["job_id"], "resume")

    def test_missing_executable_is_failure(self):
        # Exercise Popen's missing-program exception, not the compute policy gate.
        # The only permitted test command is deliberately nonexistent.
        with patch("matpilot.worker.ADAPTERS", {"mattergen": unittest.mock.Mock(
                build_command=unittest.mock.Mock(return_value=["matpilot-nonexistent-generator"]))}):
            job = self.execute(self.create(backend="native", real_compute_enabled=True,
                                           command=["matpilot-nonexistent-generator"]))
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["completed_samples"], 0)
        self.assertEqual(job["sampling_attempts_charged"], 0)
        self.assertNotIn("disabled", job["error"])
        self.assertEqual(job["batches"][0]["command"], ["matpilot-nonexistent-generator"])

    def test_mixed_demo_real_comparison_rejected(self):
        one = self.create()
        two = self.create(key="second-request", backend="native")
        with self.assertRaises(ValueError):
            self.service.compare_runs([one["job_id"], two["job_id"]])

    def test_cif_p1_is_not_phosphorus(self):
        from matpilot.structures import analyze_cif, demo_archive
        import zipfile
        target = Path(self.temp.name) / "test.zip"
        demo_archive(target, 1)
        with zipfile.ZipFile(target) as archive:
            record = analyze_cif(archive.read(archive.namelist()[0]).decode())
        self.assertEqual(set(record["elements"]), {"Na", "Cl"})


if __name__ == "__main__":
    unittest.main()
