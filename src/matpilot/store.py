"""Transactional plans and durable jobs, shared by short-lived MCP processes."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .adapters.mattergen import canonical_hash


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "matpilot.sqlite3"
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
              plan_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL, created_at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requirement_records (plan_id TEXT NOT NULL, requirement_id TEXT NOT NULL,
              payload TEXT NOT NULL, PRIMARY KEY(plan_id, requirement_id));
            CREATE TABLE IF NOT EXISTS structure_versions (id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
              job_id TEXT NOT NULL, parent_id TEXT, payload TEXT NOT NULL, integrity TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metrics (id TEXT PRIMARY KEY, version_id TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def put_plan(self, plan: dict) -> dict:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO plans VALUES (?, ?)", (plan["plan_id"], json.dumps(plan, ensure_ascii=False)))
            for requirement in plan.get("requirements", []):
                db.execute("INSERT OR IGNORE INTO requirement_records VALUES (?, ?, ?)",
                           (plan["plan_id"], requirement["requirement_id"], json.dumps(requirement, ensure_ascii=False)))
        return plan

    def plan(self, plan_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise ValueError("Unknown plan_id. Prepare a plan first.")
        plan = json.loads(row[0])
        if plan_id != canonical_hash({k: v for k, v in plan.items() if k != "plan_id"}):
            raise ValueError("Plan integrity check failed.")
        return plan

    def create_job(self, plan: dict, request_key: str, backend: str, settings: dict) -> tuple[dict, bool]:
        if not isinstance(request_key, str) or not 8 <= len(request_key) <= 128:
            raise ValueError("idempotency_key must contain 8 to 128 characters.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM jobs WHERE request_key=?", (request_key,)).fetchone()
            if row:
                old = json.loads(row[0])
                if old["plan_id"] != plan["plan_id"] or old["backend"] != backend:
                    raise ValueError("Idempotency key already belongs to a different plan or backend.")
                return old, False
            now = time.time()
            job = {"job_id": uuid.uuid4().hex, "plan_id": plan["plan_id"], "status": "queued",
                   "backend": backend, "provenance": "demonstration_fixture" if backend == "demo" else f"{plan['model_id']}_execution",
                   "created_at": now, "updated_at": now, "completed_samples": 0, "batches": [],
                   "stop_requested": False, "settings": settings, "attempt": 1}
            db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?)", (job["job_id"], request_key, job["plan_id"], job["status"], json.dumps(job)))
        self.event(job["job_id"], "submitted", {"plan_id": plan["plan_id"], "backend": backend})
        return job, True

    def job(self, job_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Unknown job_id.")
        return json.loads(row[0])

    def update(self, job_id: str, **changes) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Unknown job_id.")
            job = json.loads(row[0])
            job.update(changes, updated_at=time.time())
            for batch in job["batches"] if "batches" in changes else []:
                if batch["status"] in {"completed", "partial", "failed", "stopped"}:
                    for record in batch["records"]:
                        if record.get("structure_version_id"):
                            self._put_structure(db, job_id, record)
            db.execute("UPDATE jobs SET status=?, payload=? WHERE id=?", (job["status"], json.dumps(job), job_id))
        return job

    def claim(self, worker_pid: int) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone():
                return None
            row = db.execute("SELECT payload FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job.update(status="running", worker_pid=worker_pid, heartbeat=time.time(), updated_at=time.time())
            db.execute("UPDATE jobs SET status=?,payload=? WHERE id=?", (job["status"], json.dumps(job), job["job_id"]))
        return job

    def control(self, job_id: str, action: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Unknown job_id.")
            job = json.loads(row[0])
            if action == "stop":
                if job["status"] == "queued":
                    job["status"] = "stopped"
                elif job["status"] != "running":
                    raise ValueError("Only queued or running jobs can be stopped.")
                job["stop_requested"] = True
            elif action == "resume":
                if job["status"] not in {"stopped", "failed", "partial"}:
                    raise ValueError("Resume requires a terminal incomplete job; a late heartbeat is not proof of termination.")
                plan_row = db.execute("SELECT payload FROM plans WHERE id=?", (job["plan_id"],)).fetchone()
                cap = json.loads(plan_row[0])["num_samples"]
                if sum(b.get("sampling_charge", b["count"]) for b in job["batches"]) >= cap:
                    raise ValueError("Sampling budget is exhausted, including failed attempts. Prepare and review a new plan to increase it.")
                job.update(status="queued", stop_requested=False, attempt=job["attempt"] + 1)
            else:
                raise ValueError("action must be stop or resume")
            job["updated_at"] = time.time()
            db.execute("UPDATE jobs SET status=?,payload=? WHERE id=?", (job["status"], json.dumps(job), job_id))
        self.event(job_id, action, {"status": job["status"]})
        return job

    def event(self, job_id: str, kind: str, payload: dict):
        with self.connect() as db:
            db.execute("INSERT INTO events(job_id,created_at,kind,payload) VALUES (?,?,?,?)", (job_id, time.time(), kind, json.dumps(payload, ensure_ascii=False)))

    def events(self, job_id: str) -> list[dict]:
        self.job(job_id)
        with self.connect() as db:
            rows = db.execute("SELECT sequence,created_at,kind,payload FROM events WHERE job_id=? ORDER BY sequence", (job_id,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def jobs(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM jobs ORDER BY rowid DESC LIMIT 100").fetchall()
        return [json.loads(r[0]) for r in rows]

    def _put_structure(self, db, job_id: str, record: dict):
        from .evidence import verify_artifact
        cid, vid = record["candidate_id"], record["structure_version_id"]
        parent = record.get("parent_version_id")
        if vid != "s_" + canonical_hash([cid, record["sha256"], parent, record["transformation"]]):
            raise ValueError("Structure version identity is inconsistent.")
        if parent:
            row = db.execute("SELECT candidate_id,job_id FROM structure_versions WHERE id=?", (parent,)).fetchone()
            if not row or row[0] != cid or row[1] != job_id:
                raise ValueError("Parent structure belongs to another candidate/job or is missing.")
        payload = {**record, "job_id": job_id}
        digest = canonical_hash(payload)
        existing = db.execute("SELECT integrity FROM structure_versions WHERE id=?", (vid,)).fetchone()
        if existing and existing[0] != digest:
            raise ValueError("Immutable structure version metadata changed.")
        if not existing:
            verify_artifact(record)
        db.execute("INSERT OR IGNORE INTO structure_versions VALUES (?,?,?,?,?,?)",
                   (vid, cid, job_id, parent, json.dumps(payload, ensure_ascii=False), digest))

    def structure_version(self, version_id: str) -> dict:
        from .evidence import verify_artifact
        with self.connect() as db:
            row = db.execute("SELECT payload,integrity FROM structure_versions WHERE id=?", (version_id,)).fetchone()
        if not row:
            raise ValueError("Unknown structure_version_id.")
        result = json.loads(row[0])
        if canonical_hash(result) != row[1]:
            raise ValueError("Structure metadata integrity check failed.")
        verify_artifact(result)
        return result

    def structure_versions(self, candidate_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id FROM structure_versions WHERE candidate_id=? ORDER BY rowid", (candidate_id,)).fetchall()
        return [self.structure_version(row[0]) for row in rows]

    def add_structure_version(self, parent_version_id: str, path: Path, transformation: str, metadata: dict) -> dict:
        """Operator/evaluator API; transformations never overwrite generated structures."""
        from .structures import analyze_cif
        import hashlib
        parent = self.structure_version(parent_version_id)
        if not isinstance(transformation, str) or not transformation.strip() or not isinstance(metadata, dict):
            raise ValueError("Record the transformation and its metadata explicitly.")
        content = path.read_bytes()
        plan = self.plan(self.job(parent["job_id"])["plan_id"])
        expected = set(plan["conditions"]["chemical_system"].split("-")) if "chemical_system" in plan["conditions"] else None
        parsed = analyze_cif(content.decode("utf-8"), expected)
        if not parsed["parse_valid"]:
            raise ValueError("Transformed structure is not a valid CIF.")
        sha = hashlib.sha256(content).hexdigest()
        record = {**parsed, "candidate_id": parent["candidate_id"], "sha256": sha, "path": str(path.resolve()),
                  "parent_version_id": parent_version_id, "transformation": transformation, "transformation_metadata": metadata,
                  "structure_version_id": "s_" + canonical_hash([parent["candidate_id"], sha, parent_version_id, transformation])}
        with self.connect() as db:
            self._put_structure(db, parent["job_id"], record)
        return self.structure_version(record["structure_version_id"])

    def record_metric(self, metric: dict) -> dict:
        """Operator/evaluator API. Missing scientific metadata is never inferred."""
        from .evidence import validate_metric
        version = self.structure_version(metric["structure_version_id"])
        result = validate_metric(metric, version)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO metrics VALUES (?,?,?)", (result["metric_id"], result["structure_version_id"], json.dumps(result, ensure_ascii=False)))
        return result

    def metrics(self, version_id: str) -> list[dict]:
        from .evidence import validate_metric
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM metrics WHERE version_id=? ORDER BY rowid", (version_id,)).fetchall()
        if not rows:
            return []
        version = self.structure_version(version_id)
        result = []
        for row in rows:
            metric = json.loads(row[0])
            expected = metric.pop("metric_id")
            checked = validate_metric(metric, version)
            if checked["metric_id"] != expected:
                raise ValueError("Metric integrity check failed.")
            result.append(checked)
        return result
