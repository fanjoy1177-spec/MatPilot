"""Linux process-group supervisor, also launched from Windows through WSL.

Only the standard library is required in the isolated computation environment.
The direct child starts a new session; shutdown targets only that owned group.
Linux subreaping lets this process confirm/reap descendants after their parent
exits. An uncatchable supervisor SIGKILL or a child deliberately creating another
session is outside this boundary; this is process supervision, not a sandbox.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class CleanupError(RuntimeError):
    """The owned process group could not be confirmed terminated."""


def _enable_subreaper():
    if sys.platform != "linux":
        raise ValueError("Scientific process supervision requires Linux or WSL.")
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_CHILD_SUBREAPER. This affects only this dedicated supervisor process.
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, "Cannot enable Linux child subreaping before launch")


def _reap_group(process):
    process.poll()
    while True:
        try:
            pid, status = os.waitpid(-process.pid, os.WNOHANG)
        except ChildProcessError:
            return
        except InterruptedError:
            continue
        if pid == 0:
            return
        if pid == process.pid:
            process.returncode = os.waitstatus_to_exitcode(status)


def _group_exists(pgid):
    if pgid <= 1 or pgid == os.getpgrp():
        raise CleanupError("Refusing to signal an unowned process group")
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _signal_group(process, sig):
    if not _group_exists(process.pid):
        return
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _wait_group(process, seconds):
    deadline = time.monotonic() + seconds
    while True:
        _reap_group(process)
        if not _group_exists(process.pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _cleanup_group(process, terminate_grace, kill_grace):
    _reap_group(process)
    if not _group_exists(process.pid):
        return False
    _signal_group(process, signal.SIGTERM)
    if _wait_group(process, terminate_grace):
        return False
    # The leader can exit while descendants still ignore SIGTERM. Wait/reap the
    # owned group, then escalate that same group, rather than only waiting Popen.
    _signal_group(process, signal.SIGKILL)
    if not _wait_group(process, kill_grace):
        raise CleanupError("Owned process group still exists after SIGKILL")
    return True


def supervise(command, folder, timeout, *, terminate_grace=10.0, kill_grace=5.0):
    if not command or not all(isinstance(arg, str) and arg for arg in command):
        raise ValueError("A nonempty command argument array is required")
    if any(not math.isfinite(value) or value <= 0 for value in (timeout, terminate_grace, kill_grace)):
        raise ValueError("Supervisor time limits must be finite and positive")
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError("The task control directory must already exist")
    requested_signal = None

    def interrupted(signum, _frame):
        nonlocal requested_signal
        requested_signal = requested_signal or signum

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    process = None
    result, reason, failure, cleanup_failure = 1, "supervisor_error", None, None
    cleaned, escalated = False, False
    try:
        _enable_subreaper()
        try:
            process = subprocess.Popen(command, start_new_session=True)
        except OSError as exc:
            reason = "spawn_failed"
            result = 127 if isinstance(exc, FileNotFoundError) else 126 if isinstance(exc, PermissionError) else 1
            raise
        (folder / "linux_process.pid").write_text(str(process.pid), encoding="utf-8")
        started = time.monotonic()
        while True:
            if requested_signal is not None:
                result, reason = 128 + requested_signal, "supervisor_signal"
                break
            if (folder / "stop.request").exists():
                result, reason = 130, "stop_requested"
                break
            if time.monotonic() - started >= timeout:
                result, reason = 124, "timeout"
                break
            code = process.poll()
            if code is not None:
                result, reason = (code if code >= 0 else 128 - code), "command_exited"
                break
            time.sleep(0.05)
    except BaseException as exc:
        failure = exc
    finally:
        if process is not None:
            try:
                escalated = _cleanup_group(process, terminate_grace, kill_grace)
                cleaned = True
            except BaseException as exc:
                result, cleanup_failure = 125, exc
        else:
            cleaned = True  # No command was started; no scientific attempt exists.
        try:
            evidence = {"format": "matpilot-process-supervision/v1", "command_started": process is not None,
                        "owned_process_group_id": process.pid if process is not None else None, "reason": reason,
                        "command_returncode": process.returncode if process is not None else None,
                        "supervisor_exit_code": result, "process_group_terminated": cleaned,
                        "kill_escalated": escalated, "error": str(failure) if failure else None,
                        "cleanup_error": str(cleanup_failure) if cleanup_failure else None}
            (folder / "linux_supervisor.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            if process is not None and cleaned:
                (folder / "linux_process.exit").write_text(str(process.returncode), encoding="utf-8")
        except OSError as exc:
            failure = failure or exc
            # Without the termination receipt, the worker must keep the state
            # uncertain and must not collect possibly half-written artifacts.
            result = 125
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if cleanup_failure:
        print("Supervisor cleanup unconfirmed: " + str(cleanup_failure), file=sys.stderr)
    if failure:
        print("Supervisor error: " + str(failure), file=sys.stderr)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-dir", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--terminate-grace", type=float, default=10.0)
    parser.add_argument("--kill-grace", type=float, default=5.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return supervise(command, args.control_dir, args.timeout,
                     terminate_grace=args.terminate_grace, kill_grace=args.kill_grace)


if __name__ == "__main__":
    raise SystemExit(main())
