"""DiffCSP++ CSP bridge contract, deliberately limited to reviewed CIF templates."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from .mattergen import MatterGenAdapter, canonical_hash, wsl_path
from ..templates import resolve_template

UPSTREAM_COMMIT = "6afdd465a35088136e4875d7d2fbaefc633f4adf"
WRAPPER_SHA256 = "e4f76fedefc17e983949a97103e75298007667d41b894f82f3f0ade5104269c9"
VERSION = "diffcsp-template-contract-v1"
CHECKPOINTS = ("mp_csp", "perov_csp", "mpts_csp")


class DiffCSPAdapter:
    def describe(self):
        return {"model_id": "diffcsp", "display_name": "DiffCSP++", "adapter_version": VERSION,
                "tasks": ["template_constrained_crystal_prediction"], "checkpoints": list(CHECKPOINTS),
                "required_inputs": ["conditions.chemical_formula", "conditions.space_group", "template_id"],
                "condition_combinations": [["chemical_formula", "space_group", "registered_template"]],
                "condition_semantics": "Composition and symmetry supplied through verified template orbits; every actual output still needs independent checks.",
                "outputs": ["CIF", "generation metadata"],
                "checkers": ["CIF parse", "reduced stoichiometry", "space group", "structure matching"],
                "source": "https://github.com/jiaor17/DiffCSP-PP",
                "wrapper_source": "https://github.com/learningmatter-mit/AtomisticSkills",
                "wrapper_commit": UPSTREAM_COMMIT, "wrapper_sha256": WRAPPER_SHA256,
                "limitations": ["No symmetry guessed from a formula.", "Only registered ordered CIF templates; no arbitrary Wyckoff input from the LLM.",
                                "No property conditioning, no unconditional generation in this adapter.", "Bridge implementation is not real-model acceptance."],
                "license": "Upstream code MIT; verify separate checkpoint terms before deployment."}

    def validate(self, request, max_samples=32, settings=None):
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        # Reuse common budget/review/raw-request validation, without borrowing MatterGen semantics.
        common = {k: v for k, v in request.items() if k not in {"model_id", "checkpoint", "conditions", "template_id", "unconditional"}}
        plan = MatterGenAdapter().validate({**common, "unconditional": True}, max_samples)
        plan.update(model_id="diffcsp", adapter_version=VERSION, conditions={}, template=None,
                    template_id=request.get("template_id"), original_request=request,
                    checkpoint=request.get("checkpoint", "mp_csp"))
        issues = plan["issues"]
        def issue(code, message):
            issues.append({"code": code, "message": message})
        if request.get("unconditional", False) is not False:
            issue("unsupported_mode", "This adapter requires a registered template; unconditional generation is unsupported.")
        if request.get("search_mode", "direct") != "direct":
            issue("unsupported_mode", "Template CSP uses direct mode; do not relabel this as unconstrained search.")
        if plan["checkpoint"] not in CHECKPOINTS:
            issue("unknown_checkpoint", "Choose mp_csp, perov_csp or mpts_csp.")
        incoming = request.get("conditions", {})
        if not isinstance(incoming, dict):
            incoming = {}
            issue("invalid_conditions", "conditions must be an object.")
        for prop in set(incoming) - {"chemical_formula", "space_group"}:
            issue("unsupported_condition", f"DiffCSP++ cannot apply {prop}; retain it and revise the plan explicitly.")
        try:
            from pymatgen.core import Composition
            formula = incoming.get("chemical_formula")
            if not isinstance(formula, str) or not formula:
                raise ValueError("An explicit chemical_formula is required.")
            c = Composition(formula, strict=True)
            if not c or any(x <= 0 for x in c.values()):
                raise ValueError("Invalid chemical formula.")
            plan["conditions"]["chemical_formula"] = c.reduced_formula
        except (ValueError, TypeError) as exc:
            issue("clarification_required", str(exc))
        sg = incoming.get("space_group")
        if type(sg) is not int or not 1 <= sg <= 230:
            issue("clarification_required", "Provide the template's verified integer space_group from 1 to 230.")
        else:
            plan["conditions"]["space_group"] = sg
        if not request.get("template_id") or settings is None:
            issue("clarification_required", "Select a registered, reviewed template; no Wyckoff information will be invented.")
        else:
            try:
                template = resolve_template(request["template_id"], settings)
                plan["template"] = template
                for prop in ("chemical_formula", "space_group"):
                    if plan["conditions"].get(prop) != template[prop]:
                        issue("template_mismatch", f"Template {prop} differs from the requested value; no silent substitution.")
                if template["demo_only"] and settings.backend != "demo":
                    issue("demo_template_only", "This template is a development fixture and cannot be used for scientific execution.")
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                issue("invalid_template", str(exc))
        plan["state"] = "blocked" if any(x["code"] != "clarification_required" for x in issues) else "needs_clarification" if issues else "ready"
        plan["plan_id"] = canonical_hash({k: v for k, v in plan.items() if k != "plan_id"})
        return plan

    def build_command(self, plan, output, count, seed, settings):
        conf = settings.diffcsp
        required = {"python", "upstream_root", "model_root", "model_revision", "weight_manifest"}
        if not required <= set(conf):
            raise ValueError("Configure the isolated DiffCSP++ environment and locked code/weight identities first.")
        template = resolve_template(plan["template_id"], settings)
        if template != plan["template"]:
            raise ValueError("Template changed after plan review.")
        # output is native or WSL; use the accompanying local staging path set by worker.
        payload_path = Path(settings.bridge_staging_dir) / "diffcsp-input.json"
        payload = {"schema_version": 1, "template": template, "checkpoint": plan["checkpoint"],
                   "count": count, "seed": seed, "output": output, "upstream": conf,
                   "real_compute_enabled": settings.real_compute_enabled,
                   "expected_wrapper_commit": UPSTREAM_COMMIT, "expected_wrapper_sha256": WRAPPER_SHA256}
        payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge = Path(__file__).resolve().parents[1] / "bridges" / "diffcsp_runner.py"
        if settings.backend == "wsl":
            prefix = ["wsl.exe", "-d", settings.wsl_distribution]
            if settings.wsl_user:
                prefix += ["-u", settings.wsl_user]
            supervisor = wsl_path(Path(__file__).resolve().parents[1] / "compute_process.py")
            return prefix + ["--exec", "/usr/bin/python3", supervisor, "--control-dir", output,
                             "--timeout", str(settings.batch_timeout_seconds), "--", conf["python"], wsl_path(bridge), "--input", wsl_path(payload_path)]
        if os.name == "nt":
            raise ValueError("DiffCSP native execution requires Linux; configure WSL on Windows.")
        supervisor = str(Path(__file__).resolve().parents[1] / "compute_process.py")
        return [sys.executable, supervisor, "--control-dir", output, "--timeout", str(settings.batch_timeout_seconds), "--", conf["python"], str(bridge), "--input", str(payload_path)]
