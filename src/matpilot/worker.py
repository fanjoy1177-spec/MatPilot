"""Detached bounded worker; a tools/call submits work rather than waiting for GPU."""
from __future__ import annotations

import hashlib
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
        if batch["status"] != "completed":
            continue
        for index, record in enumerate(batch["records"]):
            file = Path(record["path"])
            if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError("Stored output is missing or changed. Restore the original artifact before resuming.")
            # Legacy v0.1 jobs receive stable derived identities without rewriting history.
            result.append(record if record.get("candidate_id") else candidate_identity(job["job_id"], batch["index"], index, record))
    return result


def execute_job(store: Store, job: dict):
    job_id = job["job_id"]
    started = time.monotonic()
    process = None
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
                supervised = settings.backend == "wsl" or (plan["model_id"] == "diffcsp" and os.name != "nt")
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
                            batch.update(status="stopped" if current["stop_requested"] else "failed", error="Stopped by user" if current["stop_requested"] else "Batch time limit reached")
                            store.update(job_id, status="stopped" if current["stop_requested"] else "partial" if records else "failed", batches=batches, completed_samples=len(records))
                            return
                        store.update(job_id, heartbeat=time.time())
                        time.sleep(0.5)
                    if process.returncode in {124, 130} and supervised:
                        batch.update(status="stopped" if process.returncode == 130 else "failed", error="Stopped by user" if process.returncode == 130 else "Batch time limit reached")
                        store.update(job_id, status="stopped" if process.returncode == 130 else "partial" if records else "failed", batches=batches, completed_samples=len(records))
                        return
                    if process.returncode != 0:
                        raise RuntimeError(f"{plan['model_id']} exited with code {process.returncode}; see batch execution.log.")
                process = None
            expected_elements = set(plan["conditions"]["chemical_system"].split("-")) if "chemical_system" in plan["conditions"] else None
            batch["collection_errors"] = []
            batch_records = collect_archive(archive, batch_dir / "candidates", count, expected_elements, allow_partial=plan["model_id"] == "diffcsp", errors=batch["collection_errors"])
            batch_records = [candidate_identity(job_id, index, i, r) for i, r in enumerate(batch_records)]
            batch.update(status="completed", records=batch_records, elapsed_seconds=time.monotonic() - start)
            records.extend(batch_records)
            store.update(job_id, batches=batches, completed_samples=len(records), heartbeat=time.time())
            store.event(job_id, "batch_completed", {"index": index, "count": len(batch_records), "elapsed_seconds": batch["elapsed_seconds"]})
            if len(batch_records) < count:
                store.update(job_id, status="partial" if records else "failed", error="Model returned fewer usable candidates than requested; attempted sampling remains charged.")
                store.event(job_id, "incomplete_batch", {"requested": count, "received": len(batch_records)})
                return
        store.update(job_id, status="completed", completed_samples=len(records))
        store.event(job_id, "execution_completed", {"count": len(records), "provenance": job["provenance"]})
    except Exception as exc:
        current = store.job(job_id)
        batches = current["batches"]
        if batches and batches[-1]["status"] == "running":
            batches[-1].update(status="failed", error=str(exc))
        store.update(job_id, status="partial" if current["completed_samples"] else "failed", error=str(exc), batches=batches)
        store.event(job_id, "execution_error", {"message": str(exc)})
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        current = store.job(job_id)
        store.update(job_id, execution_pid=None, heartbeat=time.time(),
            execution_seconds=current.get("execution_seconds", 0) + time.monotonic() - started)


def run_worker(root: Path):
    store = Store(root)
    while (job := store.claim(os.getpid())) is not None:
        execute_job(store, job)
