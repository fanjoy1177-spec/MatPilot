"""CPU-only process supervision regressions; no model or API is loaded."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from matpilot.adapters.mattergen import MatterGenAdapter
from matpilot.config import Settings

SUPERVISOR = ROOT / "src" / "matpilot" / "compute_process.py"
FAMILY = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
root, role, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def wait_file(name):
    deadline = time.monotonic() + 5
    while not (root / name).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('fixture child did not start')
        time.sleep(.01)
if role == 'grandchild':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / 'grandchild.pid').write_text(str(os.getpid()))
    time.sleep(60)
elif role == 'child':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, __file__, str(root), 'grandchild', mode])
    wait_file('grandchild.pid')
    (root / 'child.ready').write_text(str(os.getpid()))
    time.sleep(60)
else:
    child = subprocess.Popen([sys.executable, __file__, str(root), 'child', mode])
    wait_file('child.ready')
    data = {'parent': os.getpid(), 'child': child.pid,
            'grandchild': int((root / 'grandchild.pid').read_text()),
            'pgid': os.getpgrp(), 'session': os.getsid(0)}
    (root / 'family.json').write_text(json.dumps(data))
    if mode.startswith('exit'):
        raise SystemExit(int(mode[4:]))
    time.sleep(60)
'''


class NativeCommandTests(unittest.TestCase):
    def test_native_mattergen_uses_owned_supervisor_and_preserves_model_arguments(self):
        adapter = MatterGenAdapter()
        plan = adapter.validate({"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 2})
        settings = Settings(backend="native", command=["/isolated/mattergen-generate"], batch_timeout_seconds=17)
        # Replace this module's OS reference, not global os.name/pathlib behavior.
        with patch("matpilot.adapters.mattergen.os", types.SimpleNamespace(name="posix")):
            command = adapter.build_command(plan, "/owned/task", 2, 7, settings)
        self.assertEqual(command[:2], [sys.executable, str(SUPERVISOR)])
        self.assertEqual(command[2:7], ["--control-dir", "/owned/task", "--timeout", "17", "--"])
        self.assertEqual(command[7:9], ["/isolated/mattergen-generate", "/owned/task"])
        self.assertIn("--batch_size=2", command)
        self.assertIn("--seed=7", command)
        self.assertIn("--pretrained-name=chemical_system", command)

    def test_windows_native_is_rejected_before_starting_any_process(self):
        adapter = MatterGenAdapter()
        plan = adapter.validate({"unconditional": True})
        with patch("matpilot.adapters.mattergen.os", types.SimpleNamespace(name="nt")):
            with self.assertRaisesRegex(ValueError, "configure WSL"):
                adapter.build_command(plan, "C:/owned/task", 1, 0, Settings(backend="native"))


@unittest.skipUnless(sys.platform == "linux", "Real process-group tests run on Linux/WSL only")
class LinuxSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="matpilot-supervisor-cpu-")
        self.root = Path(self.temp.name)
        self.fixture = self.root / "family.py"
        self.fixture.write_text(FAMILY, encoding="utf-8")
        self.supervisor = None
        self.family = None
        # An unrelated owned sentinel proves that cleanup does not signal another group.
        self.sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)

    def tearDown(self):
        # Failure cleanup is restricted to the group created for this test.
        pgid = self.family["pgid"] if self.family else None
        if pgid is None and (self.root / "linux_process.pid").is_file():
            pgid = int((self.root / "linux_process.pid").read_text())
        if pgid is not None and pgid > 1 and pgid != os.getpgrp():
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.supervisor is not None and self.supervisor.poll() is None:
            self.supervisor.terminate()
            try:
                self.supervisor.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                self.supervisor.kill()
                self.supervisor.communicate(timeout=3)
        if self.sentinel.poll() is None:
            self.sentinel.terminate()
        self.sentinel.wait(timeout=3)
        self.temp.cleanup()

    def launch(self, mode="hold", *, timeout=8, fault=None, command=None):
        launcher = [sys.executable, str(SUPERVISOR)]
        if fault:
            # Fault injection changes recording/confirmation only; actual CPU
            # parent/child/grandchild processes and group cleanup still execute.
            code = "import importlib.util,sys,time; from pathlib import Path\n"
            code += "spec=importlib.util.spec_from_file_location('supervisor',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); del sys.argv[1]\n"
            if fault == "pid_record":
                code += "original=Path.write_text\ndef write(p,*a,**kw):\n if p.name=='linux_process.pid':\n  deadline=time.monotonic()+5\n  while not (p.parent/'family.json').exists() and time.monotonic()<deadline: time.sleep(.01)\n  raise OSError('injected PID record failure')\n return original(p,*a,**kw)\nPath.write_text=write\n"
            elif fault == "confirmation":
                code += "original=m._wait_group\ndef uncertain(*a):\n original(*a)\n return False\nm._wait_group=uncertain\n"
            elif fault == "subreaper":
                code += "def unavailable(): raise OSError('injected subreaper failure')\nm._enable_subreaper=unavailable\n"
            code += "raise SystemExit(m.main())\n"
            launcher = [sys.executable, "-c", code, str(SUPERVISOR)]
        args = ["--control-dir", str(self.root), "--timeout", str(timeout), "--terminate-grace", ".15", "--kill-grace", "1", "--"]
        command = command or [sys.executable, str(self.fixture), str(self.root), "parent", mode]
        self.supervisor = subprocess.Popen(launcher + args + command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)

    def ready(self):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                self.family = json.loads((self.root / "family.json").read_text())
                self.assertEqual(self.family["pgid"], self.family["parent"])
                self.assertEqual(self.family["session"], self.family["parent"])
                return
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(.01)
        self.fail("CPU process family did not become ready")

    def finished(self, expected, reason, *, cleaned=True):
        stdout, stderr = self.supervisor.communicate(timeout=6)
        self.assertEqual(self.supervisor.returncode, expected, stdout + stderr)
        evidence = json.loads((self.root / "linux_supervisor.json").read_text())
        self.assertEqual(evidence["format"], "matpilot-process-supervision/v1")
        self.assertEqual(evidence["supervisor_exit_code"], expected)
        self.assertEqual(evidence["reason"], reason)
        self.assertIs(evidence["process_group_terminated"], cleaned)
        self.assertIsNone(self.sentinel.poll(), "Unrelated process group was affected")
        if self.family:
            for key in ["parent", "child", "grandchild"]:
                self.assertFalse(Path("/proc", str(self.family[key])).exists(), key + " survived or was not reaped")
        return evidence

    def test_stop_reaps_descendants_ignoring_term_after_leader_exits(self):
        self.launch()
        self.ready()
        (self.root / "stop.request").touch()
        evidence = self.finished(130, "stop_requested")
        self.assertTrue(evidence["command_started"])
        self.assertTrue(evidence["kill_escalated"])

    def test_timeout_reaps_entire_owned_group(self):
        self.launch(timeout=1)
        self.ready()
        self.assertTrue(self.finished(124, "timeout")["kill_escalated"])

    def test_natural_leader_exit_cleans_remaining_children_and_preserves_exit_code(self):
        self.launch(mode="exit0")
        self.ready()
        evidence = self.finished(0, "command_exited")
        self.assertEqual(evidence["command_returncode"], 0)
        self.assertTrue(evidence["kill_escalated"])

    def test_failed_leader_exit_also_cleans_remaining_children(self):
        self.launch(mode="exit7")
        self.ready()
        self.assertEqual(self.finished(7, "command_exited")["command_returncode"], 7)

    def test_supervisor_sigterm_cleans_child_group(self):
        self.launch()
        self.ready()
        self.supervisor.send_signal(signal.SIGTERM)
        self.finished(143, "supervisor_signal")

    def test_recording_exception_after_start_still_cleans_children(self):
        self.launch(fault="pid_record")
        self.ready()
        evidence = self.finished(1, "supervisor_error")
        self.assertIn("injected PID record failure", evidence["error"])

    def test_unconfirmed_cleanup_returns_125_without_a_success_marker(self):
        self.launch(fault="confirmation")
        self.ready()
        (self.root / "stop.request").touch()
        evidence = self.finished(125, "stop_requested", cleaned=False)
        self.assertIn("still exists", evidence["cleanup_error"])
        self.assertFalse((self.root / "linux_process.exit").exists())

    def test_missing_command_is_explicitly_not_a_started_scientific_attempt(self):
        self.launch(command=[str(self.root / "nonexistent-generator")])
        evidence = self.finished(127, "spawn_failed")
        self.assertFalse(evidence["command_started"])
        self.assertIsNone(evidence["owned_process_group_id"])
        self.assertFalse((self.root / "linux_process.pid").exists())

    def test_subreaper_unavailable_fails_before_launch(self):
        self.launch(fault="subreaper")
        evidence = self.finished(1, "supervisor_error")
        self.assertFalse(evidence["command_started"])
        self.assertFalse((self.root / "family.json").exists())


if __name__ == "__main__":
    unittest.main()
