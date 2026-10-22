"""Research-only MatPilot checks over the same ReplaySession used by U/U+.

This is a CPU-verifiable checking layer, not an accepted end-to-end M driver.
It never submits a job, starts a worker, calls an API or runs a material model.
The upstream adapter remains responsible for scientific engine calls. All groups
retain ReplaySession's same candidate, metric and artifact-reading operations.

The mapping ablation injects a normalization-only requirement dependency into a
private copy of the *actual* Service.prepare_plan function. It does not patch
process globals or copy the service workflow. Private normalization helpers are
deliberately source-pinned research dependencies, not a new public product API.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
from pathlib import Path
import re
from types import FunctionType, MethodType

from matpilot import evidence, requirements, structures, templates
from matpilot.config import Settings
from matpilot.service import Service
from matpilot.study_replay import ReplaySession, SCOPE


VERSION = "matpilot-study-checks-v1"
SHARED_READ_OPERATIONS = {"status", "list_artifacts", "read_artifact", "get_candidates", "get_metrics"}


def _normalization_without_mapping(request: dict, plan: dict) -> dict:
    """Keep types/units for post-checks, remove source/mapping/omission decisions.

    No original-text anchoring, lexical coverage, condition-to-requirement
    mapping, or resulting scientific-guarantee gate is executed in this arm.
    Ordinary model input/template/budget validation still runs in Service.
    """
    incoming = request.get("requirements")
    if incoming is None:
        incoming = requirements._structured_records(request)
    normalized, issues, ids = [], [], set()
    if not isinstance(incoming, list) or len(incoming) > 80:
        incoming = []
        issues.append({"code": "invalid_requirement", "message": "Requirements must be a list of at most 80 records."})
    required = {"requirement_id", "source", "kind", "property", "value", "strength", "relation"}
    for item in incoming:
        try:
            if not isinstance(item, dict) or not required <= set(item) or set(item) - required - {"unit", "origin"}:
                raise ValueError("Requirement fields do not match the declared schema.")
            rid = item["requirement_id"]
            if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid) or rid in ids:
                raise ValueError("Requirement IDs must be unique nonempty identifiers.")
            ids.add(rid)
            if item["kind"] not in requirements.KINDS or item["strength"] not in {"hard", "goal"}:
                raise ValueError("Unknown requirement kind or strength.")
            normalized.append({**requirements._normalize(item), "origin": "study_mapping_ablation_unchecked_source"})
        except (ValueError, TypeError, KeyError) as exc:
            issues.append({"code": "invalid_requirement", "message": str(exc)})
    goals = list(dict.fromkeys([*plan.get("review_targets", []),
                              *[r["normalized_value"] for r in normalized if r["kind"] == "research_goal"]]))
    return {"requirements": normalized, "requirement_mappings": [], "review_targets": goals, "issues": issues,
            "coverage": {"scope": "disabled_by_predeclared_mapping_ablation", "proposed_count": len(incoming),
                         "valid_count": len(normalized), "mapped_count": None, "uncovered_signals": None,
                         "program_mapping_coverage": None, "semantic_completeness": "not_verified",
                         "limitations": "Source anchoring, omission detection and requirement-to-action mapping are disabled; type/unit normalization remains for independent result checks."}}


def _injected_prepare(service: Service):
    original = Service.prepare_plan
    context = dict(original.__globals__)
    context["audit_requirements"] = _normalization_without_mapping
    injected = FunctionType(original.__code__, context, original.__name__, original.__defaults__, original.__closure__)
    injected.__kwdefaults__ = original.__kwdefaults__
    return MethodType(injected, service)


class MatPilotChecks:
    def __init__(self, settings: Settings, replay: ReplaySession, *, requirement_mapping: bool = True,
                 evidence_checks: bool = True):
        if type(requirement_mapping) is not bool or type(evidence_checks) is not bool or not (requirement_mapping or evidence_checks):
            raise ValueError("Use M or exactly one predeclared ablation; U/U+ do not use this checking layer.")
        if settings.real_compute_enabled is not False or not isinstance(replay, ReplaySession):
            raise ValueError("Research checks require a shared ReplaySession with real computation disabled.")
        self.replay = replay
        self.requirement_mapping = requirement_mapping
        self.evidence_checks = evidence_checks
        self._service = Service(copy.deepcopy(settings))
        self._prepare = self._service.prepare_plan if requirement_mapping else _injected_prepare(self._service)
        sources = {"checking_layer": Path(__file__), "service": Path(inspect.getfile(Service)),
                   "requirements": Path(requirements.__file__), "evidence": Path(evidence.__file__),
                   "structures": Path(structures.__file__), "templates": Path(templates.__file__)}
        self.identity = {"version": VERSION, "requirement_mapping": requirement_mapping, "evidence_checks": evidence_checks,
                         "source_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()},
                         "evidence_scope": SCOPE, "science_computation_performed": False,
                         "acceptance": "isolated_checking_layer_not_complete_Harness_M_driver"}

    def _result(self, payload: dict) -> dict:
        return {**payload, "study_checks": copy.deepcopy(self.identity), "replay": copy.deepcopy(self.replay.identity)}

    def prepare_plan(self, request: dict) -> dict:
        return self._result({"plan": self._prepare(copy.deepcopy(request))})

    def get_capabilities(self) -> dict:
        capabilities = self._service.get_capabilities()
        return self._result({"capabilities": capabilities,
                             "notice": "Capabilities describe actual production validators. This research layer exposes no submit/control/worker operation; scientific calls must use the same upstream replay adapter as U+."})

    def shared_read(self, operation: str, arguments: dict | None = None) -> dict:
        if operation not in SHARED_READ_OPERATIONS:
            raise ValueError("This layer only forwards shared read operations; no scientific or worker execution endpoint.")
        return self.replay.invoke(operation, arguments)

    def _candidates(self, candidate_ids: list[str]) -> list[dict]:
        if not isinstance(candidate_ids, list) or not candidate_ids:
            raise ValueError("Select explicit existing candidate IDs, not formula or list-position guesses.")
        return self.shared_read("get_candidates", {"candidate_ids": candidate_ids})["records"]

    def _metrics(self, record: dict, *, validate: bool) -> list[dict]:
        returned = self.shared_read("get_metrics", {"candidate_id": record["candidate_id"],
                                                     "structure_version_id": record["structure_version_id"]})["metrics"]
        if not validate:
            return returned
        result = []
        for metric in returned:
            # Replay adds explicit provenance; the production validator has an exact schema.
            metadata = {k: metric[k] for k in ("provenance", "evidence_scope") if k in metric}
            original = {k: v for k, v in metric.items() if k not in {"metric_id", "provenance", "evidence_scope"}}
            checked = evidence.validate_metric(original, record)
            if metric.get("metric_id") != checked["metric_id"]:
                raise ValueError("Metric identity changed after replay materialization.")
            result.append({**checked, **metadata})
        return result

    def check_candidates(self, plan_id: str, candidate_ids: list[str]) -> dict:
        plan = self._service.store.plan(plan_id)
        records = self._candidates(candidate_ids)
        if not self.evidence_checks:
            return self._result({"plan_id": plan_id, "records": records,
                                 "metrics_by_structure_version": {r["structure_version_id"]: self._metrics(r, validate=False) for r in records},
                                 "checks_performed": False, "valid_candidate_count": None, "unique_valid_candidate_count": None,
                                 "notice": "Result-checking ablation: raw shared evidence remains readable; no requirement pass, material quality or ranking is asserted."})
        checked_records, metrics = [], {}
        for record in records:
            evidence.verify_artifact(record)
            # Parse actual returned bytes; do not trust invented geometry flags in fixture metadata.
            analysis = structures.analyze_cif(Path(record["path"]).read_text(encoding="utf-8"))
            if not analysis.get("parse_valid"):
                raise ValueError("Replay candidate CIF cannot be parsed: " + analysis.get("error", "unknown error"))
            checked_records.append({**record, **analysis})
            metrics[record["structure_version_id"]] = self._metrics(record, validate=True)
        screened = evidence.screen_records(plan, checked_records, metrics)
        return self._result({"plan_id": plan_id, "planning_state": plan["state"], **screened,
                             "metrics_by_structure_version": metrics, "checks_performed": True,
                             "notice": "These are real software checks on synthetic/recorded replay inputs, not measured material performance or real-model acceptance."})

    def compare_candidate_metrics(self, candidate_ids: list[str], property_name: str, metric_ids: list[str] | None = None) -> dict:
        records = self._candidates(candidate_ids)
        if len(records) < 2 or not isinstance(property_name, str) or not property_name:
            raise ValueError("Select at least two candidates and an explicit property.")
        metrics = [m for r in records for m in self._metrics(r, validate=self.evidence_checks) if m["property"] == property_name]
        if metric_ids is not None:
            if not isinstance(metric_ids, list) or len(set(metric_ids)) != len(metric_ids) or not set(metric_ids) <= {m["metric_id"] for m in metrics}:
                raise ValueError("Select existing unique metric IDs for the requested candidates/property.")
            metrics = [m for m in metrics if m["metric_id"] in metric_ids]
        if any(sum(m["candidate_id"] == r["candidate_id"] for m in metrics) != 1 for r in records):
            raise ValueError("Choose exactly one existing metric per candidate; no favorable-value selection is automatic.")
        comparison = evidence.compare_metrics(metrics) if self.evidence_checks else {
            "comparable": None, "reasons": ["Result-checking ablation: comparability has not been evaluated."],
            "metric_ids": [m["metric_id"] for m in metrics]}
        return self._result({**comparison, "metrics": metrics, "checks_performed": self.evidence_checks})

    def invoke(self, operation: str, arguments: dict | None = None) -> dict:
        args = {} if arguments is None else arguments
        if not isinstance(args, dict):
            raise ValueError("Arguments must be an object.")
        if operation in SHARED_READ_OPERATIONS:
            return self.shared_read(operation, args)
        if operation == "get_capabilities" and not args:
            return self.get_capabilities()
        if operation == "prepare_plan" and set(args) == {"request"}:
            return self.prepare_plan(args["request"])
        if operation == "check_candidates" and set(args) == {"plan_id", "candidate_ids"}:
            return self.check_candidates(args["plan_id"], args["candidate_ids"])
        if operation == "compare_candidate_metrics" and set(args) in ({"candidate_ids", "property_name"}, {"candidate_ids", "property_name", "metric_ids"}):
            return self.compare_candidate_metrics(**args)
        raise ValueError("Unknown checking operation or invalid arguments; no submit, control or worker endpoint exists.")
