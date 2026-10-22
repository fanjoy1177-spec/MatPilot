"""Production-worker CPU fixtures: no model imports, weights, API or inference."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from matpilot.config import Settings
from matpilot.service import Service
from matpilot.structures import collect_archive, demo_archive
from matpilot.worker import execute_job


class PartialWorkerTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "synthetic-one.zip"
        demo_archive(self.fixture, 1)
        with zipfile.ZipFile(self.fixture) as archive:
            self.cif_bytes = archive.read(archive.namelist()[0])
        cif = self.root / "synthetic.cif"
        cif.write_bytes(self.cif_bytes)
        self.service = Service(Settings(data_dir=self.root / "data", templates={"fixture": {
            "path": str(cif), "sha256": hashlib.sha256(self.cif_bytes).hexdigest(),
            "source": "synthetic CPU workflow fixture", "demo_only": True,
            "review": {"by": "unit test", "date": "2026-10-22", "scope": "not scientific acceptance"}}}))
        self.counter = 0
        # This executable only copies a synthetic archive and exits or sleeps.
        # Extra MatterGen CLI flags are intentionally ignored; no model is imported.
        self.program = self.root / "cpu_fixture_process.py"
        self.program.write_text('''import json, os, pathlib, shutil, sys, time
mode, fixture, directory = sys.argv[1:4]
output = pathlib.Path(directory)
target = output / "generated_crystals_cif.zip"
if mode == "corrupt":
    target.write_bytes(b"not a closed ZIP archive")
else:
    shutil.copyfile(fixture, target)
(output / "fixture.ready").write_text("synthetic CPU fixture only")
if mode == "unconfirmed":
    (output / "linux_supervisor.json").write_text(json.dumps({
        "format": "matpilot-process-supervision/v1", "command_started": True,
        "owned_process_group_id": os.getpid(), "reason": "supervisor_error",
        "command_returncode": 0, "supervisor_exit_code": 125,
        "process_group_terminated": False, "kill_escalated": True,
        "error": "synthetic uncertain termination", "cleanup_error": "synthetic"}))
    sys.exit(125)
if mode == "hang":
    time.sleep(60)
sys.exit(7 if mode in {"fail", "corrupt"} else 0)
''', encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, model, samples=2):
        if model == "diffcsp":
            return {"model_id": model, "template_id": "fixture", "conditions": {"chemical_formula": "NaCl", "space_group": 221},
                    "num_samples": samples, "batch_size": 2}
        return {"model_id": model, "conditions": {"chemical_system": "Na-Cl"}, "num_samples": samples, "batch_size": 2}

    def create(self, model="mattergen", samples=2, **overrides):
        plan = self.service.prepare_plan(self.request(model, samples))
        self.assertEqual(plan["state"], "ready", plan["issues"])
        settings = {**self.service.settings.snapshot(), **overrides}
        self.counter += 1
        return self.service.store.create_job(plan, "synthetic-worker-" + str(self.counter), settings["backend"], settings)[0]

    def execute(self, job):
        claimed = self.service.store.claim(os.getpid())
        self.assertEqual(claimed["job_id"], job["job_id"])
        execute_job(self.service.store, claimed)
        return self.service.get_job(job["job_id"])

    def assert_retained(self, job, count, status="partial"):
        self.assertEqual(job["status"], status)
        self.assertEqual(job["completed_samples"], count)
        result = self.service.get_results(job["job_id"])
        self.assertEqual(result["count"], count)
        for record in result["records"]:
            stored = self.service.store.structure_version(record["structure_version_id"])
            self.assertEqual(stored["candidate_id"], record["candidate_id"])
            self.assertEqual(stored["sha256"], hashlib.sha256(Path(record["path"]).read_bytes()).hexdigest())
        report = self.service.build_report(job["job_id"])
        evidence = json.loads(Path(report["evidence_path"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["screening"]["count"], count)
        self.assertEqual(evidence["job"]["status"], status)
        return result["records"]

    def test_both_models_keep_short_batches_and_charge_full_started_budget(self):
        for model in ("mattergen", "diffcsp"):
            with self.subTest(model=model), patch("matpilot.worker.demo_archive", side_effect=lambda path, count: shutil.copyfile(self.fixture, path)):
                job = self.execute(self.create(model))
            records = self.assert_retained(job, 1)
            self.assertEqual(job["batches"][0]["status"], "partial")
            self.assertEqual(job["sampling_attempts_charged"], 2)
            self.assertEqual(job["sampling_budget_remaining"], 0)
            self.assertEqual(self.service.get_results(job["job_id"])["records"], records)
            with self.assertRaisesRegex(ValueError, "budget"):
                self.service.store.control(job["job_id"], "resume")

    def test_both_models_keep_valid_sibling_and_record_invalid_cif(self):
        def mixed(path, count):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("bad.cif", "invalid CIF")
                archive.writestr("valid.cif", self.cif_bytes)
        for model in ("mattergen", "diffcsp"):
            with self.subTest(model=model), patch("matpilot.worker.demo_archive", side_effect=mixed):
                job = self.execute(self.create(model))
            records = self.assert_retained(job, 1)
            self.assertEqual(records[0]["source_member"], "valid.cif")
            self.assertEqual(job["batches"][0]["collection_errors"][0]["source_member"], "bad.cif")
            self.assertEqual(job["sampling_attempts_charged"], 2)

    def test_all_invalid_and_overproduced_archives_never_become_success(self):
        def invalid(path, count):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("bad.cif", "invalid CIF")
        def excessive(path, count):
            demo_archive(path, count + 1)
        for model in ("mattergen", "diffcsp"):
            for callback in (invalid, excessive):
                with self.subTest(model=model, fixture=callback.__name__), patch("matpilot.worker.demo_archive", side_effect=callback):
                    job = self.execute(self.create(model))
                self.assert_retained(job, 0, "failed")
                self.assertEqual(job["sampling_attempts_charged"], 2)
                self.assertTrue(job["batches"][0]["collection_errors"])
                with self.assertRaisesRegex(ValueError, "budget"):
                    self.service.store.control(job["job_id"], "resume")

    def test_archive_limits_reject_before_writing_even_in_partial_mode(self):
        for case, payloads, expected, message in (
                ("overcount", [self.cif_bytes] * 3, 2, "Expected 2"),
                ("oversize", [self.cif_bytes, b"x" * (4 * 1024 * 1024 + 1)], 2, "4 MiB"),
                ("total", [b"x" * (4 * 1024 * 1024)] * 9, 9, "batch size limit")):
            with self.subTest(case=case):
                archive = self.root / (case + ".zip")
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                    for index, payload in enumerate(payloads):
                        bundle.writestr(str(index) + ".cif", payload)
                output = self.root / case
                with self.assertRaisesRegex(ValueError, message):
                    collect_archive(archive, output, expected, None, allow_partial=True)
                self.assertEqual(list(output.iterdir()), [])

    def external_job(self, mode, samples=2, *, supervised_fixture=False, timeout=10):
        command = [sys.executable, str(self.program), mode, str(self.fixture)]
        job = self.create(samples=samples, backend="wsl" if supervised_fixture and os.name == "nt" else "native",
                          command=command, real_compute_enabled=True, batch_timeout_seconds=timeout)
        # Linux uses the actual adapter/supervisor wrapping the CPU-only command.
        # Windows native scientific execution is unsupported: this test-only
        # adapter supplies an explicit CPU command to exercise worker mechanics.
        override = os.name == "nt" or supervised_fixture
        adapter = Mock(build_command=lambda plan, output, count, seed, settings:
                       command + [settings.bridge_staging_dir])
        return job, patch("matpilot.worker.ADAPTERS", {"mattergen": adapter}) if override else patch.dict("matpilot.worker.ADAPTERS", {})

    def test_nonzero_external_exit_retains_closed_archive_and_bounded_resume(self):
        job, command = self.external_job("fail", samples=4)
        with command:
            first = self.execute(job)
        original = self.assert_retained(first, 1)[0]
        self.assertEqual(first["batches"][0]["exit_code"], 7)
        self.assertEqual(first["sampling_attempts_charged"], 2)
        self.service.store.control(job["job_id"], "resume")
        # Reuse the same CPU-only command and original immutable job settings.
        fake = Mock(build_command=lambda plan, output, count, seed, settings:
                    [sys.executable, str(self.program), "fail", str(self.fixture), settings.bridge_staging_dir])
        with patch("matpilot.worker.ADAPTERS", {"mattergen": fake}) if os.name == "nt" else patch.dict("matpilot.worker.ADAPTERS", {}):
            second = self.execute(job)
        records = self.assert_retained(second, 2)
        self.assertEqual(records[0], original)
        self.assertNotEqual(records[0]["candidate_id"], records[1]["candidate_id"])
        self.assertEqual(len(self.service.store.structure_versions(original["candidate_id"])), 1)
        self.assertEqual(second["sampling_attempts_charged"], 4)
        with self.assertRaisesRegex(ValueError, "budget"):
            self.service.store.control(job["job_id"], "resume")

    def test_corrupt_archive_after_external_failure_is_not_fabricated(self):
        job, command = self.external_job("corrupt")
        with command:
            result = self.execute(job)
        self.assert_retained(result, 0, "failed")
        self.assertEqual(result["sampling_attempts_charged"], 2)
        self.assertEqual(result["batches"][0]["collection_status"], "rejected")

    def test_external_timeout_retains_closed_archive_without_claiming_completion(self):
        job, command = self.external_job("hang", timeout=2)
        with command:
            result = self.execute(job)
        self.assert_retained(result, 1)
        self.assertIn("time limit", result["batches"][0]["error"])
        self.assertEqual(result["sampling_attempts_charged"], 2)

    def test_external_stop_retains_closed_archive_and_preserves_stopped_status(self):
        job, command = self.external_job("hang")
        ready = self.service.store.root / "jobs" / job["job_id"] / "batch_0000/fixture.ready"
        errors = []
        def request_stop():
            try:
                deadline = time.monotonic() + 10
                while not ready.is_file():
                    if time.monotonic() > deadline:
                        raise AssertionError("CPU fixture did not start")
                    time.sleep(.02)
                self.service.store.control(job["job_id"], "stop")
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=request_stop)
        thread.start()
        try:
            with command:
                result = self.execute(job)
        finally:
            thread.join(timeout=12)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assert_retained(result, 1, "stopped")
        self.assertEqual(result["batches"][0]["status"], "stopped")
        self.assertEqual(result["sampling_attempts_charged"], 2)
        with self.assertRaisesRegex(ValueError, "budget"):
            self.service.store.control(job["job_id"], "resume")

    def test_missing_or_unconfirmed_supervisor_receipt_blocks_collection_and_resume(self):
        for mode in ("unconfirmed", "success"):
            # Separate stores: an uncertain execution deliberately blocks the queue.
            self.service = Service(Settings(data_dir=self.root / ("quarantine-" + mode)))
            job, command = self.external_job(mode, supervised_fixture=True)
            with self.subTest(mode=mode), command:
                result = self.execute(job)
            self.assertEqual(result["status"], "running")
            self.assertFalse(result["batches"][0]["termination_confirmed"])
            self.assertEqual(self.service.get_results(job["job_id"])["count"], 0)
            self.assertEqual(result["sampling_attempts_charged"], 2)
            with self.assertRaisesRegex(ValueError, "terminal"):
                self.service.store.control(job["job_id"], "resume")

    def test_worker_update_exception_waits_for_confirmed_stop_before_collecting(self):
        job, command = self.external_job("hang")
        ready = self.service.store.root / "jobs" / job["job_id"] / "batch_0000/fixture.ready"
        original_update, original_popen = self.service.store.update, subprocess.Popen
        injected, processes = [], []
        def fail_once(job_id, **changes):
            if set(changes) == {"heartbeat"} and ready.exists() and not injected:
                injected.append(True)
                raise RuntimeError("Synthetic worker persistence error while process is live")
            return original_update(job_id, **changes)
        def track_process(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process
        with command, patch.object(self.service.store, "update", side_effect=fail_once), patch("matpilot.worker.subprocess.Popen", side_effect=track_process):
            result = self.execute(job)
        self.assertEqual(injected, [True])
        self.assertTrue(processes and all(p.poll() is not None for p in processes))
        self.assert_retained(result, 1)
        self.assertIn("persistence error", result["error"])
        self.assertEqual(result["sampling_attempts_charged"], 2)
        if os.name != "nt":
            self.assertTrue(result["batches"][0]["supervision"]["process_group_terminated"])
            self.assertTrue(result["batches"][0]["termination_confirmed"])

    def test_worker_exception_quarantines_when_supervisor_cannot_confirm_bounded_stop(self):
        # An explicit CPU process stands in for an unresponsive supervisor. The
        # test owns and cleans it up after verifying that worker does not kill a
        # launcher and falsely declare its unseen process group terminated.
        job, command = self.external_job("hang", supervised_fixture=True)
        folder = self.service.store.root / "jobs" / job["job_id"] / "batch_0000"
        original_update, original_popen = self.service.store.update, subprocess.Popen
        injected, processes = [], []
        def fail_once(job_id, **changes):
            if set(changes) == {"heartbeat"} and (folder / "fixture.ready").exists() and not injected:
                injected.append(True)
                raise RuntimeError("Synthetic error before supervisor confirmation")
            return original_update(job_id, **changes)
        def track_process(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process
        try:
            with command, patch.object(self.service.store, "update", side_effect=fail_once), patch("matpilot.worker.subprocess.Popen", side_effect=track_process), patch("matpilot.worker.SUPERVISOR_STOP_TIMEOUT", .1):
                result = self.execute(job)
            self.assertEqual(injected, [True])
            self.assertEqual(result["status"], "running")
            self.assertFalse(result["batches"][0]["termination_confirmed"])
            self.assertTrue((folder / "stop.request").is_file())
            self.assertTrue(processes and processes[0].poll() is None)
            self.assertEqual(self.service.get_results(job["job_id"])["count"], 0)
            self.assertEqual(result["sampling_attempts_charged"], 2)
            with self.assertRaisesRegex(ValueError, "terminal"):
                self.service.store.control(job["job_id"], "resume")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
