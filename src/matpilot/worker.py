"""Detached bounded worker; a tools/call submits work rather than waiting for GPU."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .adapters import ADAPTERS
from .adapters.mattergen import wsl_path
from .config import Settings
from .store import Store
from .structures import collect_archive, demo_archive
from .evidence import candidate_identity

SUPERVISOR_STOP_TIMEOUT = 30


def launch_worker(root: Path) -> int:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (root / "worker.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "matpilot", "_worker", str(root)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags,
            start_new_session=os.name != "nt")
    return process.pid


def verified_records(job: dict) -> list[dict]:
    result = []
    for batch in job["batches"]:
        if batch["status"] not in {"completed", "partial", "failed", "stopped"}:
            continue
        for index, record in enumerate(batch["records"]):
            file = Path(record["path"])
            if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError("Stored output is missing or changed. Restore the original artifact before resuming.")
            # Legacy v0.1 jobs receive stable derived identities without rewriting history.
            result.append(record if record.get("candidate_id") else candidate_identity(job["job_id"], batch["index"], index, record))
    return result


def collect_batch_outputs(job_id: str, plan: dict, batch: dict, batch_dir: Path, started: float) -> list[dict]:
    """Collect only a closed archive after execution ended, including failed runs.

    Collection success does not change the execution outcome. Rejected archives
    never contribute a fabricated candidate; individual invalid CIFs are recorded
    while valid siblings retain their normal candidate/version identities.
    """
    batch["collection_attempted"] = True
    batch["collection_errors"] = []
    batch["records"] = []
    archive = batch_dir / "generated_crystals_cif.zip"
    elements = set(plan["conditions"]["chemical_system"].split("-")) if "chemical_system" in plan["conditions"] else None
    try:
        collected = collect_archive(archive, batch_dir / "candidates", batch["count"], elements,
                                    allow_partial=True, errors=batch["collection_errors"])
        batch["records"] = [candidate_identity(job_id, batch["index"], i, record) for i, record in enumerate(collected)]
        batch["collection_status"] = "complete" if len(collected) == batch["count"] else "partial" if collected else "failed"
    except Exception as exc:
        batch["collection_status"] = "rejected"
        batch["collection_errors"].append({"source_archive": archive.name, "error": str(exc)})
    batch["elapsed_seconds"] = time.monotonic() - started
    return batch["records"]


def supervision_receipt(batch_dir: Path, returncode: int) -> dict:
    """Require positive supervisor evidence before treating GPU work as ended."""
    path = batch_dir / "linux_supervisor.json"
    raw = path.read_bytes()
    receipt = json.loads(raw)
    if (not isinstance(receipt, dict) or receipt.get("format") != "matpilot-process-supervision/v1"
            or type(receipt.get("command_started")) is not bool
            or type(receipt.get("process_group_terminated")) is not bool
            or receipt.get("reason") not in {"spawn_failed", "supervisor_error", "command_exited", "stop_requested", "timeout", "supervisor_signal"}
            or receipt.get("supervisor_exit_code") != returncode
            or (receipt["command_started"] and (type(receipt.get("owned_process_group_id")) is not int
                                                or receipt["owned_process_group_id"] <= 0))
            or (not receipt["command_started"] and receipt.get("owned_process_group_id") is not None)):
        raise ValueError("Supervisor receipt has an invalid execution identity.")
    return {**receipt, "artifact": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}}


def confirm_supervision(batch: dict, batch_dir: Path, returncode: int) -> dict:
    receipt = supervision_receipt(batch_dir, returncode)
    batch["supervision"] = receipt
    if not receipt["command_started"]:
        batch["sampling_charge"] = 0
    if not receipt["process_group_terminated"]:
        raise ValueError("Supervisor could not confirm termination of the owned process group.")
    batch["termination_confirmed"] = True
    return receipt


def stop_and_confirm(process, supervised: bool, batch: dict, batch_dir: Path) -> None:
    """Bounded cleanup; never kill the WSL launcher/supervisor to fake success."""
    if process.poll() is None:
        if supervised:
            (batch_dir / "stop.request").touch()
            # Default supervisor TERM + KILL grace is 15 seconds, so a 10-second
            # wait can expire while safe group cleanup is still taking place.
            process.wait(timeout=SUPERVISOR_STOP_TIMEOUT)
        else:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    if supervised:
        confirm_supervision(batch, batch_dir, process.returncode)
    batch["exit_code"] = process.returncode


def execute_job(store: Store, job: dict):
    job_id = job["job_id"]
    started = time.monotonic()
    process = None
    batch = None
    supervised = False
    termination_unconfirmed = False
    try:
        plan = store.plan(job["plan_id"])
        values = dict(job["settings"])
        values["data_dir"] = Path(values["data_dir"])
        settings = Settings(**values)
        if plan["state"] != "ready":
            raise ValueError("Only a ready immutable plan can execute.")
        if settings.backend != "demo" and not settings.real_compute_enabled:
            raise ValueError("Real scientific computation is disabled by operator policy.")
        records = verified_records(job)
        batches = job["batches"]
        while len(records) < plan["num_samples"]:
            if store.job(job_id)["stop_requested"]:
                store.update(job_id, status="stopped", completed_samples=len(records))
                return
            index = len(batches)
            charged = sum(b.get("sampling_charge", b["count"]) for b in batches)
            remaining = plan["num_samples"] - charged
            if remaining <= 0:
                raise ValueError("Maximum sampling budget exhausted, including failed attempts. A larger budget requires a new reviewed plan.")
            count = min(plan["batch_size"], remaining)
            batch_dir = store.root / "jobs" / job_id / f"batch_{index:04d}"
            batch_dir.mkdir(parents=True, exist_ok=False)
            batch = {"index": index, "count": count, "status": "running", "records": [],
                     "sampling_charge": 0,
                     "seed": (plan["seed"] + index) % (2**31),
                     "seed_applied": settings.seed_supported and settings.backend != "demo"}
            batches.append(batch)
            store.update(job_id, batches=batches, heartbeat=time.time())
            start = time.monotonic()
            archive = batch_dir / "generated_crystals_cif.zip"
            if settings.backend == "demo":
                batch["sampling_charge"] = count
                store.update(job_id, batches=batches)
                demo_archive(archive, count)
                batch["command"] = ["internal:demonstration_fixture"]
            else:
                output = wsl_path(batch_dir) if settings.backend == "wsl" else str(batch_dir)
                settings.bridge_staging_dir = str(batch_dir)
                command = ADAPTERS[plan["model_id"]].build_command(plan, output, count, batch["seed"], settings)
                supervised = settings.backend == "wsl" or os.name != "nt"
                batch["command"] = command
                store.event(job_id, "batch_started", {"index": index, "command": command, "seed_applied": batch["seed_applied"]})
                with (batch_dir / "execution.log").open("wb") as log:
                    process = subprocess.Popen(command, cwd=settings.cwd, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    batch["sampling_charge"] = count
                    store.update(job_id, execution_pid=process.pid, batches=batches)
                    while process.poll() is None:
                        current = store.job(job_id)
                        if current["stop_requested"] or time.monotonic() - start > settings.batch_timeout_seconds:
                            if supervised:
                                # The Linux supervisor terminates and reaps the entire model
                                # process group. Killing only wsl.exe could orphan GPU work.
                                if current["stop_requested"]:
                                    (batch_dir / "stop.request").touch()
                                store.update(job_id, heartbeat=time.time(), diagnostic="Waiting for Linux supervisor to confirm process termination.")
                                time.sleep(0.5)
                                continue
                            process.terminate()
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=10)
                            records.extend(collect_batch_outputs(job_id, plan, batch, batch_dir, start))
                            batch.update(status="stopped" if current["stop_requested"] else "partial" if batch["records"] else "failed",
                                         exit_code=process.returncode,
                                         error="Stopped by user" if current["stop_requested"] else "Batch time limit reached")
                            store.update(job_id, status="stopped" if current["stop_requested"] else "partial" if records else "failed", batches=batches, completed_samples=len(records))
                            store.event(job_id, "batch_interrupted", {"index": index, "status": batch["status"], "retained": len(batch["records"]), "error": batch["error"]})
                            return
                        store.update(job_id, heartbeat=time.time())
                        time.sleep(0.5)
                    supervision = None
                    if supervised:
                        try:
                            supervision = confirm_supervision(batch, batch_dir, process.returncode)
                        except (OSError, ValueError, TypeError) as exc:
                            termination_unconfirmed = True
                            diagnostic = "Execution termination is unconfirmed; do not resume or collect live outputs. " + str(exc)
                            batch.update(exit_code=process.returncode, termination_confirmed=False, error=diagnostic)
                            store.update(job_id, status="running", batches=batches, diagnostic=diagnostic, error=diagnostic)
                            store.event(job_id, "termination_unconfirmed", {"index": index, "message": diagnostic})
                            return
                    if supervised and (supervision["reason"] in {"stop_requested", "timeout"}
                                       or supervision["reason"] == "supervisor_signal" and process.returncode == 130):
                        stopped = supervision["reason"] != "timeout"
                        records.extend(collect_batch_outputs(job_id, plan, batch, batch_dir, start))
                        batch.update(status="stopped" if stopped else "partial" if batch["records"] else "failed",
                                     exit_code=process.returncode,
                                     error="Stopped by user" if stopped else "Batch time limit reached")
                        store.update(job_id, status="stopped" if stopped else "partial" if records else "failed", batches=batches, completed_samples=len(records))
                        store.event(job_id, "batch_interrupted", {"index": index, "status": batch["status"], "retained": len(batch["records"]), "error": batch["error"]})
                        return
                    batch["exit_code"] = process.returncode
                    if process.returncode != 0:
                        raise RuntimeError(f"{plan['model_id']} exited with code {process.returncode}; see batch execution.log.")
                process = None
            batch_records = collect_batch_outputs(job_id, plan, batch, batch_dir, start)
            batch["status"] = "completed" if len(batch_records) == count else "partial" if batch_records else "failed"
            records.extend(batch_records)
            store.update(job_id, batches=batches, completed_samples=len(records), heartbeat=time.time())
            store.event(job_id, "batch_completed" if batch["status"] == "completed" else "batch_incomplete",
                        {"index": index, "count": len(batch_records), "elapsed_seconds": batch["elapsed_seconds"]})
            if len(batch_records) < count:
                error = "Model returned fewer usable candidates than requested; attempted sampling remains charged."
                if batch["collection_status"] == "rejected":
                    error = "Batch output collection rejected: " + batch["collection_errors"][0]["error"]
                batch["error"] = error
                store.update(job_id, status="partial" if records else "failed", batches=batches, error=error)
                store.event(job_id, "incomplete_batch", {"requested": count, "received": len(batch_records)})
                return
        store.update(job_id, status="completed", completed_samples=len(records))
        store.event(job_id, "execution_completed", {"count": len(records), "provenance": job["provenance"]})
    except Exception as exc:
        cleanup_error = None
        if process is not None:
            try:
                stop_and_confirm(process, supervised, batch, batch_dir)
            except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as cleanup_exc:
                termination_unconfirmed = True
                cleanup_error = str(cleanup_exc)
        current = store.job(job_id)
        batches = current["batches"]
        if termination_unconfirmed:
            if batch is not None and batches and batch["index"] == batches[-1]["index"]:
                batch.update(status="running", termination_confirmed=False)
                batches[-1] = batch
            diagnostic = "Execution termination is unconfirmed; no collection or resume is allowed. " + (cleanup_error or str(exc))
            store.update(job_id, status="running", batches=batches, diagnostic=diagnostic, error=str(exc))
            store.event(job_id, "termination_unconfirmed", {"message": diagnostic, "execution_error": str(exc)})
            return
        if batches and batches[-1]["status"] == "running":
            if batch is not None and batch["index"] == batches[-1]["index"]:
                batches[-1] = batch
            active = batches[-1]
            # A completed archive is safe to inspect only once the process has
            # actually exited. Missing executables never consume a sample.
            if (active.get("sampling_charge", 0) > 0 and not active.get("collection_attempted")
                    and (process is None or process.poll() is not None)):
                collect_batch_outputs(job_id, plan, active, batch_dir, start)
            active.update(status="partial" if active["records"] else "failed", error=str(exc))
        completed = sum(len(b["records"]) for b in batches if b["status"] in {"completed", "partial", "failed", "stopped"})
        store.update(job_id, status="partial" if completed else "failed", error=str(exc), batches=batches, completed_samples=completed)
        store.event(job_id, "execution_error", {"message": str(exc)})
    finally:
        # Covers exceptional unwinding too, without retrying a timed-out cleanup
        # or terminating wsl.exe while its owned Linux process group may be live.
        if process is not None and process.poll() is None and not termination_unconfirmed:
            try:
                stop_and_confirm(process, supervised, batch, batch_dir)
            except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
                termination_unconfirmed = True
                batch.update(status="running", termination_confirmed=False)
                current = store.job(job_id)
                current["batches"][-1] = batch
                store.update(job_id, status="running", batches=current["batches"],
                             diagnostic="Execution termination is unconfirmed; no resume is allowed. " + str(exc))
        current = store.job(job_id)
        store.update(job_id, execution_pid=process.pid if termination_unconfirmed and process is not None else None, heartbeat=time.time(),
            execution_seconds=current.get("execution_seconds", 0) + time.monotonic() - started)


def run_worker(root: Path):
    store = Store(root)
    while (job := store.claim(os.getpid())) is not None:
        execute_job(store, job)
