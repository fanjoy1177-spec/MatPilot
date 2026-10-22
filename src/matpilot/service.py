"""The same deterministic service is used by Harness, CLI and evaluations."""
from __future__ import annotations

import importlib.metadata
import json
import platform
import shutil
import time
from pathlib import Path

from . import __version__
from .adapters import ADAPTERS
from .adapters.mattergen import canonical_hash
from .config import Settings
from .store import Store
from .worker import launch_worker, verified_records
from .requirements import audit_requirements, contract as requirement_contract
from .evidence import screen_records
from .capabilities import cards
from .repairs import repair_options


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.data_dir)

    def get_capabilities(self) -> dict:
        return {"product": "MatPilot", "version": __version__, "models": cards(self.settings),
                "backend": self.settings.backend, "max_samples": self.settings.max_samples,
                "default_samples": min(16, self.settings.max_samples), "seed_supported": self.settings.seed_supported,
                "requirements_contract": requirement_contract(),
                "request_example": {"raw_request": "Fe-Ni-O，最多4个",
                    "model_id": "mattergen", "conditions": {"chemical_system": "Fe-Ni-O"},
                    "requirements": requirement_contract()["example"]["requirements"], "review_targets": [],
                    "num_samples": min(4, self.settings.max_samples), "batch_size": min(2, self.settings.max_samples),
                    "seed": 42, "unconditional": False, "clarifications": []},
                "input_format": {"chemical_system": "A hyphen-separated string such as Fe-Ni-O, never an array or fixed formula.",
                    "numeric_condition": {"value": 1200, "unit": "meV", "relation": "target"},
                    "review_targets": "A list of strings; these are unverified research goals, not generation conditions."},
                "planning_rules": [
                    "Copy the complete user request into raw_request; retain every constraint and research goal.",
                    "An unverified research goal may stay in review_targets without blocking an otherwise valid plan, unless the user requires it as a guaranteed condition.",
                    "Use clarifications only for genuinely missing, conflicting or ambiguous required information. Do not ask for optional numerical targets that the user did not request.",
                    "Only query a job_id actually returned by submit_job. A plan_id is not a job_id; a failed submission creates no usable job identifier."],
                "demo_notice": "Demo uses fixed NaCl fixtures, not MatterGen or AI-generated results." if self.settings.backend == "demo" else None,
                "workflow": "prepare_plan -> show conditions, review_targets and budget -> submit_job with explicit user authorization -> get_job -> screen_candidates -> build_report"}

    def prepare_plan(self, request: dict) -> dict:
        model_id = request.get("model_id", "mattergen") if isinstance(request, dict) else "mattergen"
        if model_id not in ADAPTERS:
            raise ValueError(f"Unknown model {model_id!r}; installed: {list(ADAPTERS)}")
        plan = ADAPTERS[model_id].validate(request, self.settings.max_samples, self.settings)
        requirement_audit = audit_requirements(request, plan)
        plan.update({k: v for k, v in requirement_audit.items() if k != "issues"})
        plan["issues"].extend(requirement_audit["issues"])
        plan["schema_version"] = 2
        plan["state"] = "blocked" if any(i["code"] != "clarification_required" for i in plan["issues"]) else "needs_clarification" if plan["issues"] else "ready"
        plan["sampling_budget"] = {"max_samples": plan["num_samples"], "valid_candidates_target": plan.get("valid_candidates_target"), "target_guaranteed": False}
        plan["execution_context"] = {"backend": self.settings.backend,
            "config_hash": canonical_hash(self.settings.snapshot()),
            "seed_will_be_applied": self.settings.seed_supported and self.settings.backend != "demo",
            "notice": "Fixed demonstration fixtures; no generation." if self.settings.backend == "demo" else
                      "Installed MatterGen does not accept a seed; exact structure reproduction is not promised." if not self.settings.seed_supported else
                      "Seed applied; reproducibility still depends on software, hardware and weights."}
        plan["repair_options"] = repair_options(plan)
        plan["execution_context"]["real_compute_enabled"] = self.settings.real_compute_enabled
        plan["plan_id"] = canonical_hash({k: v for k, v in plan.items() if k != "plan_id"})
        return self.store.put_plan(plan)

    def submit_job(self, plan_id: str, idempotency_key: str, approved_plan_id: str) -> dict:
        plan = self.store.plan(plan_id)
        if plan["state"] != "ready":
            raise ValueError("Resolve all plan issues before submitting. No conditions are discarded automatically.")
        if approved_plan_id != plan_id:
            raise ValueError("Approval must refer to this exact plan_id.")
        if plan.get("execution_context", {}).get("config_hash") != canonical_hash(self.settings.snapshot()):
            raise ValueError("Execution configuration changed. Prepare and review a new plan before submitting.")
        if self.settings.backend != "demo" and not self.settings.real_compute_enabled:
            raise ValueError("Real scientific computation is disabled; planning and demo checks remain available.")
        if plan["model_id"] == "diffcsp" and plan.get("template"):
            from .templates import resolve_template
            if resolve_template(plan["template_id"], self.settings) != plan["template"]:
                raise ValueError("Template changed after approval. Prepare and review a new plan.")
        job, created = self.store.create_job(plan, idempotency_key, self.settings.backend, self.settings.snapshot())
        if created:
            try:
                launch_worker(self.store.root)
            except OSError as exc:
                self.store.update(job["job_id"], status="failed", error=f"Worker could not start: {exc}")
                raise ValueError("Worker could not start. Inspect the job and resume after fixing the environment.") from exc
        return {**self.get_job(job["job_id"]), "deduplicated": not created}

    def get_job(self, job_id: str) -> dict:
        job = self.store.job(job_id)
        public = {k: v for k, v in job.items() if k != "settings"}
        if job["status"] == "running" and time.time() - job.get("heartbeat", job["updated_at"]) > 30:
            public["diagnostic"] = "Worker heartbeat is late. Execution status is uncertain; do not start a duplicate job. Inspect worker.log and the process before recovery."
        public["scientific_status"] = "unvalidated_candidates"
        public["target_samples"] = self.store.plan(job["plan_id"])["num_samples"]
        public["sampling_attempts_charged"] = sum(b.get("sampling_charge", b["count"]) for b in job["batches"])
        public["sampling_budget_remaining"] = max(0, public["target_samples"] - public["sampling_attempts_charged"])
        return public

    def control_job(self, job_id: str, action: str) -> dict:
        if action == "resume" and self.store.job(job_id)["backend"] != "demo" and not self.settings.real_compute_enabled:
            raise ValueError("Real scientific computation is disabled.")
        if action == "resume":
            verified_records(self.store.job(job_id))
        self.store.control(job_id, action)
        if action == "resume":
            try:
                launch_worker(self.store.root)
            except OSError as exc:
                self.store.update(job_id, status="failed", error=f"Worker could not start: {exc}")
                raise
        return self.get_job(job_id)

    def get_results(self, job_id: str) -> dict:
        job = self.store.job(job_id)
        records = verified_records(job)
        return {"job_id": job_id, "status": job["status"], "provenance": job["provenance"],
                "records": records, "count": len(records),
                "structure_versions": {r["candidate_id"]: self.store.structure_versions(r["candidate_id"]) for r in records},
                "scientific_status": "structural_sanity_only; properties not measured"}

    def screen_candidates(self, job_id: str) -> dict:
        result = self.get_results(job_id)
        plan = self.store.plan(self.store.job(job_id)["plan_id"])
        metrics = {r["structure_version_id"]: self.store.metrics(r["structure_version_id"]) for r in result["records"]}
        screened = screen_records(plan, result["records"], metrics)
        target = plan.get("valid_candidates_target")
        return {**result, **screened, "metrics_by_structure_version": metrics,
                "valid_candidates_target": target,
                "valid_target_met": screened["unique_valid_candidate_count"] >= target if target is not None else None,
                "method": "CIF parsing, per-requirement three-valued checks, SHA256 identity and tolerance-defined pymatgen structure matching"}

    def compare_runs(self, job_ids: list[str]) -> dict:
        if not 2 <= len(job_ids) <= 10 or len(set(job_ids)) != len(job_ids):
            raise ValueError("Provide 2 to 10 distinct job ids.")
        jobs = [self.store.job(x) for x in job_ids]
        plans = [self.store.plan(x["plan_id"]) for x in jobs]
        if len({x["provenance"] for x in jobs}) != 1:
            raise ValueError("Demo and real generation must not be compared as model performance.")
        comparable = all((p["model_id"], p["conditions"], p["num_samples"], p["checkpoint"], p.get("template"), p.get("requirements")) ==
                         (plans[0]["model_id"], plans[0]["conditions"], plans[0]["num_samples"], plans[0]["checkpoint"], plans[0].get("template"), plans[0].get("requirements")) for p in plans)
        rows = []
        for job in jobs:
            results = self.screen_candidates(job["job_id"])
            rows.append({"job_id": job["job_id"], "status": job["status"], "count": results["count"],
                "basic_checks_pass": sum(x["basic_checks_pass"] for x in results["records"]),
                "execution_seconds": job.get("execution_seconds")})
        return {"same_task": comparable, "rows": rows,
                "interpretation": "Descriptive workflow comparison only. Different hardware, versions, seeds and checkpoints confound speed and quality comparisons; this is not evidence of AI benefit."}

    def build_report(self, job_id: str) -> dict:
        job = self.store.job(job_id)
        plan = self.store.plan(job["plan_id"])
        result = self.screen_candidates(job_id)
        folder = self.store.root / "reports" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        packages = {}
        for name in ["matpilot-core", "mcp", "pymatgen"]:
            try:
                packages[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                packages[name] = "unknown"
        evidence = {"schema_version": 2, "plan": plan, "job": self.get_job(job_id), "screening": result,
                    "events": self.store.events(job_id), "environment": {"python": platform.python_version(), "platform": platform.platform(), "packages": packages},
                    "limitations": ["Execution completion is not validation of material performance.", "AI effectiveness and user time savings require a separate controlled study."]}
        json_path = folder / "evidence.json"
        json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# 材策 MatPilot 任务报告", "", f"任务编号：{job_id}", f"执行状态：{job['status']}",
                 f"数据来源：{job['provenance']}", "", "## 研究需求", str(plan["raw_request"]), "",
                 "## 执行条件", f"模型：{plan['model_id']} / {plan['checkpoint']}",
                 "```json", json.dumps(plan["conditions"], ensure_ascii=False, indent=2), "```", "",
                 f"已收集 {result['count']} 个可解析结构；最大采样预算 {plan['num_samples']}。",
                 f"计入预算的采样尝试：{self.get_job(job_id)['sampling_attempts_charged']}（含已启动但失败的批次）。",
                 f"通过基础检查和全部硬性结果要求：{result['valid_candidate_count']} 个文件；结构匹配去重后：{result['unique_valid_candidate_count']} 个。",
                 f"结构等价检查结果未知：{result['equivalence_unknown_count']} 个。",
                 f"合格候选目标：{result.get('valid_candidates_target')}；目标是否达到：{result.get('valid_target_met')}。", "",
                 "## 仍需验证的研究目标"]
        lines += [f"- {x}" for x in plan["review_targets"]] or ["未另行指定。"]
        lines += ["", "## 结果边界", "本报告只说明任务执行和结构基本检查情况。生成条件是目标值，不是测得的材料性质；未进行热力学、催化、耐腐蚀或实验性能验证。",
                  "示例模式使用固定样本，不构成真实生成或 AI 效果证据。人工操作时间与 AI 增益须通过独立对照实验评估。", "",
                  "## 候选结构", "", "| 候选 ID | 结构版本 | 文件 | 化学式 | 硬性要求均通过 | 等价候选 ID |", "|---|---|---|---|---|---|"]
        for r in result["records"]:
            lines.append(f"| {r['candidate_id']} | {r['structure_version_id']} | {Path(r['path']).name} | {r['formula']} | {r['all_hard_requirements_pass']} | {r['equivalent_to_candidate_id'] or '-'} |")
        lines += ["", "## 逐项要求与证据", "", "程序映射覆盖不等于原文语义完整；需独立复核。未配置评价方法的性质保持 unknown。",
                  "", "| 候选 ID | 要求 ID | 判定 | 观察值 | 方法或未知原因 |", "|---|---|---|---|---|"]
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ")
        for r in result["records"]:
            for check in r["requirement_checks"]:
                lines.append("| " + " | ".join(cell(check[k]) for k in ("candidate_id", "requirement_id", "status", "observed")) + " | " + cell(check["method"] or check["reason"]) + " |")
        md_path = folder / "report.md"
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"job_id": job_id, "report_path": str(md_path), "evidence_path": str(json_path),
                "execution_status": job["status"], "scientific_status": "unvalidated_candidates", "provenance": job["provenance"]}

    def doctor(self) -> dict:
        executable = "wsl.exe" if self.settings.backend == "wsl" else self.settings.command[0]
        return {"backend": self.settings.backend, "data_dir": str(self.store.root),
                "command_available": shutil.which(executable) is not None or Path(executable).is_file(),
                "readiness": "demo_available" if self.settings.backend == "demo" else "requires_generation_smoke_test",
                "seed_supported": self.settings.seed_supported, "models": cards(self.settings),
                "notice": "Executable discovery does not prove weights, CUDA or server readiness. API credentials belong to Harness, not this MCP service."}
