from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from .config import Settings
from .service import Service


def main():
    parser = argparse.ArgumentParser(description="MatPilot materials tools for DeepSeek Harness")
    parser.add_argument("--config", help="Operator configuration JSON; also MATPILOT_CONFIG")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["capabilities", "doctor", "serve", "jobs", "demo"]:
        sub.add_parser(name)
    p = sub.add_parser("plan")
    p.add_argument("request_file")
    p = sub.add_parser("submit")
    p.add_argument("plan_id")
    p.add_argument("--approve-plan", required=True)
    p.add_argument("--key", required=True)
    for name in ["job", "results", "screen", "report", "stop", "resume"]:
        p = sub.add_parser(name)
        p.add_argument("job_id")
    p = sub.add_parser("compare")
    p.add_argument("job_ids", nargs="+")
    p = sub.add_parser("_worker", help=argparse.SUPPRESS)
    p.add_argument("data_dir")
    args = parser.parse_args()
    try:
        if args.command == "_worker":
            from .worker import run_worker
            run_worker(Path(args.data_dir))
            return
        settings = Settings.load(args.config)
        service = Service(settings)
        if args.command == "serve":
            from .server import make_server
            make_server(settings).run(transport="stdio")
            return
        if args.command == "capabilities":
            result = service.get_capabilities()
        elif args.command == "doctor":
            result = service.doctor()
        elif args.command == "plan":
            result = service.prepare_plan(json.loads(Path(args.request_file).read_text(encoding="utf-8-sig")))
        elif args.command == "submit":
            result = service.submit_job(args.plan_id, args.key, args.approve_plan)
        elif args.command == "job":
            result = service.get_job(args.job_id)
        elif args.command == "results":
            result = service.get_results(args.job_id)
        elif args.command == "screen":
            result = service.screen_candidates(args.job_id)
        elif args.command == "report":
            result = service.build_report(args.job_id)
        elif args.command == "compare":
            result = service.compare_runs(args.job_ids)
        elif args.command in {"stop", "resume"}:
            result = service.control_job(args.job_id, args.command)
        elif args.command == "jobs":
            result = [service.get_job(j["job_id"]) for j in service.store.jobs()]
        elif args.command == "demo":
            if settings.backend != "demo":
                raise ValueError("demo requires a demo backend; it never starts real generation.")
            # This is an explicit structured fixture, not a claimed NLP extraction.
            plan = service.prepare_plan({"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 2})
            job = service.submit_job(plan["plan_id"], "demo-" + uuid.uuid4().hex, plan["plan_id"])
            deadline = time.monotonic() + 120
            while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.5)
                job = service.get_job(job["job_id"])
            if job["status"] != "completed":
                raise ValueError(f"Demo did not complete: {job}")
            result = service.build_report(job["job_id"])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
