"""Isolated, replay-only U/U+/M JSONL broker for the Harness study bridge.

The selected upstream implementation supplies scientific operations. M adds the
actual MatPilot planning/evidence layer; no code here launches a real worker.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import sys

# Execute the source tree bound by the study manifest, including in a copied pack.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from matpilot.config import Settings
from matpilot.capabilities import cards
from matpilot.study_replay import ReplayError
from matpilot.templates import resolve_template

from matpilot_checks import MatPilotChecks
from upstream_replay import UpstreamWorker

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
GROUPS = {"U", "U+", "M", "M_without_mapping", "M_without_evidence"}
READ_OPS = {"status", "list_artifacts", "read_artifact", "get_candidates", "get_metrics"}


def tool(name, description, properties=None, required=()):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}}


def read_tools():
    string = {"type": "string"}
    return [tool("status", "Read shared replay budget and execution status; no computation."),
            tool("list_artifacts", "List the returned CIF and raw metric artifacts, available equally to all groups."),
            tool("read_artifact", "Read an existing artifact by its explicit ID and verify its bytes.", {"artifact_id": string}, ["artifact_id"]),
            tool("get_candidates", "Read candidate IDs, structure versions and CIF paths; omitted IDs return all currently available candidates.",
                 {"candidate_ids": {"type": "array", "items": string}}),
            tool("get_metrics", "Read metrics for a candidate and optional exact structure version. Synthetic values are workflow data only.",
                 {"candidate_id": string, "structure_version_id": string}, ["candidate_id"])]


class Broker:
    def __init__(self, args):
        self.group = args.group
        if self.group not in GROUPS:
            raise ValueError("Unknown study group.")
        self.guidance_scale, self.step_lr = args.guidance_scale, args.step_lr
        if any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in (self.guidance_scale, self.step_lr)):
            raise ValueError("Scientific defaults must be finite and positive.")
        upstream_args = copy.copy(args)
        upstream_args.group = "U" if self.group == "U" else "U+"
        self.worker = UpstreamWorker(upstream_args)
        self.session = self.worker.session
        self.checks = None
        self.used_plans = set()
        self.plan_context = {}
        config_path = Path(args.template_config).resolve()
        config = json.loads(config_path.read_bytes())
        config.update(data_dir=self.worker.root / "matpilot-checks", backend="demo", real_compute_enabled=False,
                      max_samples=args.sampling_cap or 32, config_path=str(config_path))
        for registration in config.get("templates", {}).values():
            registration["path"] = str((config_path.parent / registration["path"]).resolve())
        self.settings = Settings(**config)
        if self.group.startswith("M"):
            self.checks = MatPilotChecks(self.settings, self.session,
                                       requirement_mapping=self.group != "M_without_mapping",
                                       evidence_checks=self.group != "M_without_evidence")
        self.identity = {"group": self.group, "real_compute_allowed": False,
                         "process_id": os.getpid(),
                         "upstream": self.worker.identity, "checking_layer": self.checks.identity if self.checks else None,
                         "scientific_defaults": {"guidance_scale": self.guidance_scale, "step_lr": self.step_lr},
                         "evidence_scope": "workflow_engineering_only_not_material_property_evidence",
                         "production_validator_backend": "demo", "execution_backend": "shared_condition_bound_replay",
                         "seed_applied": False,
                         "candidate_evidence_scope": "Checks and metrics bind shared engine CIF versions. Upstream serialized CIFs have separate child versions and do not inherit metrics.",
                         "request_scope": "Original task semantics are graded against the frozen prompt. Source anchoring checks the submitted raw_request and cannot by itself establish that the LLM copied the whole original prompt."}

    def describe(self):
        upstream = self.worker.invoke("describe_tools", {})
        for schema in upstream["tools"]:
            schema["description"] += " Study protocol: common scientific defaults are guidance_scale=2.0 and step_lr=1e-5; supplied different values are rejected, not silently changed."
            key = "guidance_scale" if schema["name"] == "generate_structures" else "step_lr"
            schema["parameters"]["properties"][key]["default"] = self.guidance_scale if key == "guidance_scale" else self.step_lr
        tools = read_tools()
        tools.append(tool("get_input_capabilities", "Read the common model/checkpoint input contracts, shared scientific defaults and replay limitations. Available equally to every group."))
        tools.append(tool("list_templates", "Read the same operator-registered and source-checked templates in every group."))
        tools.append(tool("list_upstream_artifacts", "List original upstream output CIF and generation metadata files, available equally to all groups."))
        tools.append(tool("read_upstream_artifact", "Read one listed original upstream output and verify its hash.", {"relative_path": {"type": "string"}}, ["relative_path"]))
        if self.checks:
            string = {"type": "string"}
            strings = {"type": "array", "items": string}
            tools += [tool("get_capabilities", "Read production model input contracts and requirement schema. Real computation stays disabled; execution here uses the shared replay backend."),
                      tool("prepare_plan", "Keep the full user request and individual requirements. Validate and freeze a plan without generating.", {"request": {"type": "object"}}, ["request"]),
                      tool("execute_plan", "Execute an existing ready plan once using the same patched upstream tools and synthetic replay budget. No real science runs.", {"plan_id": string}, ["plan_id"]),
                      tool("check_candidates", "Check explicit candidate identities and each requirement using MatPilot's evidence functions.", {"plan_id": string, "candidate_ids": strings}, ["plan_id", "candidate_ids"]),
                      tool("compare_candidate_metrics", "Compare explicit candidate metrics only when their calculation methods are compatible.",
                           {"candidate_ids": strings, "property_name": string, "metric_ids": strings}, ["candidate_ids", "property_name"])]
        else:
            tools += upstream["tools"]
        return {"tools": tools, "identity": self.identity, "normal_instructions": upstream["normal_instructions"]}

    def execute(self, plan_id):
        if not self.checks or plan_id not in self.plan_context:
            raise ReplayError("unknown_plan", "Prepare this exact plan in the current session first.")
        plan = self.plan_context[plan_id]
        if plan["state"] != "ready":
            raise ReplayError("blocked_plan", "Resolve all plan issues before replay execution.")
        if plan_id in self.used_plans:
            raise ReplayError("already_attempted", "This plan has already been attempted; inspect existing evidence. No automatic retry.")
        if plan["model_id"] == "diffcsp":
            template = resolve_template(plan["template_id"], self.settings)
            if template != plan["template"]:
                raise ReplayError("template_changed", "Registered template changed after planning.")
            args = {"num_samples": plan["num_samples"], "batch_size": plan["batch_size"], "model_name": plan["checkpoint"],
                    "spacegroup": template["space_group"], "wyckoff_letters": ",".join(template["wyckoff_letters"]),
                    "atom_types": ",".join(template["atom_types"]), "step_lr": self.step_lr}
            method = "generate_structures_with_symmetry"
        elif plan["model_id"] == "mattergen":
            conditions = dict(plan["conditions"])
            args = {"num_structures": plan["num_samples"], "batch_size": plan["batch_size"],
                    "model_name": plan["checkpoint"], "guidance_scale": self.guidance_scale, "device": "cpu"}
            if "chemical_system" in conditions:
                args["chemical_system"] = conditions.pop("chemical_system")
            if conditions:
                args["properties"] = conditions
            method = "generate_structures"
        else:
            raise ReplayError("driver_not_ready", "This model is outside the common replay capability.")
        self.used_plans.add(plan_id)
        result = self.worker.invoke(method, args)
        return {"plan_id": plan_id, "actual_upstream_arguments": args, **result}

    def invoke(self, method, arguments):
        if not isinstance(arguments, dict):
            raise ReplayError("invalid_request", "Tool arguments must be an object.")
        if method == "describe_tools" and not arguments:
            return self.describe()
        if method == "trace_receipt" and not arguments:
            return self.worker.invoke(method, arguments)
        if method in READ_OPS:
            return self.session.invoke(method, arguments)
        if method == "get_input_capabilities" and not arguments:
            return {"models": cards(self.settings), "scientific_defaults": self.identity["scientific_defaults"],
                    "execution_backend": "shared_condition_bound_replay", "real_compute_allowed": False,
                    "notice": "Production input contracts are common background knowledge. Replay covers the declared task conditions and registered templates; no real model readiness is established. Hidden route catalogs are not exposed."}
        if method in {"list_upstream_artifacts", "read_upstream_artifact"}:
            return self.worker.invoke(method, arguments)
        if method == "list_templates" and not arguments:
            return {"templates": {name: resolve_template(name, self.settings) for name in self.settings.templates},
                    "scope": "Same registered templates for all groups; no inference from hidden replay routes."}
        if self.checks:
            if method == "execute_plan" and set(arguments) == {"plan_id"}:
                return self.execute(arguments["plan_id"])
            result = self.checks.invoke(method, arguments)
            if method == "prepare_plan":
                self.plan_context[result["plan"]["plan_id"]] = copy.deepcopy(result["plan"])
            if method == "get_capabilities":
                result["capabilities"]["demo_notice"] = "This study executes the same condition-bound replay as U/U+, not the production fixed-NaCl demo. Real science and production submit_job remain unavailable."
                result["capabilities"]["workflow"] = "prepare_plan -> execute_plan -> get_candidates -> check_candidates; original shared artifacts and metrics remain readable"
            result["study_execution"] = {k: self.identity[k] for k in ("production_validator_backend", "execution_backend", "seed_applied", "candidate_evidence_scope", "request_scope")}
            return result
        permitted = {t["name"] for t in self.worker.invoke("describe_tools", {})["tools"]}
        if method in permitted:
            passed = dict(arguments)
            key, value = ("guidance_scale", self.guidance_scale) if method == "generate_structures" else ("step_lr", self.step_lr)
            if key in passed and (type(passed[key]) not in (int, float) or passed[key] != value):
                raise ReplayError("scientific_protocol_mismatch", "This study fixes " + key + "=" + str(value) + " equally across all groups. Explicit alternatives are not rewritten.")
            passed.setdefault(key, value)
            return self.worker.invoke(method, passed)
        raise ReplayError("tool_unavailable", "Tool is not available in this group; direct sample and process execution are not exposed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=sorted(GROUPS), required=True)
    for flag in ("manifest", "fixture-id", "run-id", "output-dir"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--sampling-cap", type=int, default=32)
    parser.add_argument("--guidance-scale", type=float, default=2.0)
    parser.add_argument("--step-lr", type=float, default=1e-5)
    parser.add_argument("--template-config", default=str(ROOT / "examples/config.multimodel.demo.json"))
    parser.add_argument("--formal", action="store_true")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--review")
    parser.add_argument("--expected-review-sha256")
    args = parser.parse_args(argv)
    transport = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    broker = Broker(args)
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"id", "method", "arguments"}:
                raise ReplayError("invalid_request", "Expected id, method and arguments only.")
            response = {"id": request["id"], "result": broker.invoke(request["method"], request["arguments"])}
        except Exception as exc:
            response = {"id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": getattr(exc, "code", "broker_error"), "message": str(exc)}}
        transport.write((json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
