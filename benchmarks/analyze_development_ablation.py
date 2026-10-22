"""Summarize public development ablation records, without scoring their answers.

Usage: python benchmarks/analyze_development_ablation.py PROTOCOL REVIEW_DIR OUTPUT
Only the protocol, metrics.json and public runs/*.json are read. No API, material
model, original private trace or production MatPilot module is imported.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re


GROUPS = {"M", "M_without_mapping", "M_without_evidence"}
FLAGS = {"M": (True, True), "M_without_mapping": (False, True), "M_without_evidence": (True, False)}
TERMINAL = ("completed", "failed", "timeout", "budget_stopped")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,159}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
OPERATIONS = ("prepare_plan", "execute_plan", "check_candidates", "compare_candidate_metrics", "status")


def parse_json(text):
    def reject(value):
        raise ValueError("Nonfinite JSON number: " + value)
    return json.loads(text, parse_constant=reject)


def load(path):
    value = parse_json(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object: " + Path(path).name)
    return value


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError("Invalid or missing SHA256 identity.")


def number(value, name, *, integer=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or integer and type(value) is not int:
        raise ValueError("Invalid nonnegative measurement: " + name)
    return value


def pick(value, keys):
    """Missing keys stay absent; an explicitly recorded null stays null."""
    return {key: value[key] for key in keys if key in value}


def body(tool):
    result = tool.get("result")
    if not isinstance(result, dict):
        raise ValueError("Tool result must be an object.")
    decoded = None
    text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text")
    if text:
        try:
            decoded = parse_json(text)
        except ValueError:
            if result.get("isError") is not True:
                raise ValueError("Non-error tool response has undecodable structured content.")
    if "value" in result:
        if decoded is not None and decoded != result["value"]:
            raise ValueError("Public tool content and value disagree.")
        decoded = result["value"]
    if not isinstance(decoded, dict):
        if result.get("isError") is True:
            return {"error": text}, True
        raise ValueError("Structured tool response must be an object.")
    return decoded, result.get("isError") is True or decoded.get("isError") is True or bool(decoded.get("error"))


def budget(value):
    return pick(value, ("attempts_used", "remaining_budget", "sampling_cap", "requested_samples", "candidate_ids"))


def observe(tool, index):
    data, error = body(tool)
    name = tool["name"]
    item = {"tool_index": index, "arguments": tool["arguments"], "tool_error_returned": error}
    if error:
        item["error"] = data.get("error", data)
        return item, data
    if name == "prepare_plan":
        plan = data.get("plan")
        if not isinstance(plan, dict) or not isinstance(plan.get("issues", []), list):
            raise ValueError("prepare_plan has no structured plan/issues.")
        item.update(pick(plan, ("plan_id", "state", "engine", "model_name", "conditions", "num_samples", "batch_size", "review_targets", "coverage")))
        item["issues"] = [pick(issue, ("code", "requirement_id", "message")) for issue in plan.get("issues", [])]
        item["requirements"] = [pick(r, ("requirement_id", "kind", "property", "value", "unit", "normalized_value", "normalized_unit", "strength", "relation", "origin")) for r in plan.get("requirements", [])]
    elif name == "execute_plan":
        item.update(pick(data, ("plan_id", "actual_upstream_arguments")))
        item["upstream"] = pick(data.get("upstream_result", {}), ("status", "num_requested", "sampling_attempts", "num_generated", "success", "error"))
        item["replay_status"] = budget(data.get("replay_status", {}))
        item["generator_calls"] = [{"actual_engine_request": c.get("actual_engine_request"),
                                    "result": pick(c.get("replay_result", {}), ("status", "requested_samples", "attempts_consumed", "num_generated", "errors")),
                                    "budget": budget(c.get("replay_result", {}).get("budget", {}))} for c in data.get("generator_calls", [])]
    elif name == "check_candidates":
        item.update(pick(data, ("plan_id", "planning_state", "checks_performed", "requirements_audited", "valid_candidate_count", "unique_valid_candidate_count", "equivalence_unknown_count")))
        item["records"] = [pick(r, ("candidate_id", "structure_version_id", "parent_version_id", "basic_checks_pass", "all_hard_requirements_pass", "research_goals_verified", "requirement_checks", "structural_equivalence_status", "equivalent_to_candidate_id", "exact_file_duplicate")) for r in data.get("records", [])]
    elif name == "compare_candidate_metrics":
        item.update(pick(data, ("checks_performed", "comparable", "reasons", "metric_ids", "interpretation")))
        item["metrics"] = [pick(m, ("metric_id", "candidate_id", "structure_version_id", "property", "value", "unit", "method", "convergence", "reference_set")) for m in data.get("metrics", [])]
    elif name == "status":
        item["budget"] = budget(data)
    return item, data


def expected_rows(protocol):
    tasks, groups, repeats = protocol.get("task_ids"), protocol.get("groups"), protocol.get("repeats")
    if protocol.get("format") != "matpilot-development-ablation-protocol/v1" or protocol.get("common", {}).get("real_compute_allowed") is not False:
        raise ValueError("A development ablation protocol with real computation disabled is required.")
    if not isinstance(tasks, list) or not tasks or any(not isinstance(t, str) or not IDENTIFIER.fullmatch(t) for t in tasks) or len(set(tasks)) != len(tasks):
        raise ValueError("Invalid or duplicate prespecified tasks.")
    if not isinstance(groups, list) or len(groups) != 3 or set(groups) != GROUPS or type(repeats) is not int or repeats not in (1, 2, 3):
        raise ValueError("The three ablation groups and explicit repeats are required.")
    for group in groups:
        declared = protocol.get("module_changes", {}).get(group, {})
        if tuple(declared.get(k) for k in ("requirement_mapping", "evidence_checks")) != FLAGS[group] or any(type(declared.get(k)) is not bool for k in ("requirement_mapping", "evidence_checks")):
            raise ValueError("Protocol switches do not implement the declared ablation.")
    rows = {f"{task}--{group}--{repeat}": (task, group, repeat) for task in tasks for group in groups for repeat in range(1, repeats + 1)}
    if protocol.get("planned_runs") != len(rows):
        raise ValueError("Protocol planned_runs does not match its fixed task/group/repeat matrix.")
    return rows


def analyze(protocol_path, review_directory):
    protocol = load(protocol_path)
    expected = expected_rows(protocol)
    review = Path(review_directory).resolve(strict=True)
    metrics_path = review / "metrics.json"
    metrics = load(metrics_path)
    if metrics.get("format") != "matpilot-development-review/v1" or metrics.get("mode") != "development" or any(metrics.get(k) is not False for k in ("correctness_scored", "scientific_results_validated", "improvement_or_speedup_estimated")):
        raise ValueError("Only an ungraded development review is accepted.")
    for key in ("runtime_pack_sha256", "suite_sha256", "plan_sha256", "ledger_snapshot_sha256"):
        check_hash(metrics.get(key))
    if protocol.get("runtime_pack_sha256") is not None and protocol["runtime_pack_sha256"] != metrics["runtime_pack_sha256"]:
        raise ValueError("Protocol and review runtime-pack hashes disagree.")
    if metrics.get("planned_runs") != len(expected) or metrics.get("selected_runs") != len(expected) or metrics.get("not_selected_runs") != 0:
        raise ValueError("Review must retain the entire prespecified matrix without selection or omission.")
    indexed = {}
    for run in metrics.get("runs", []):
        rid = run.get("run_id")
        if rid in indexed or rid not in expected:
            raise ValueError("Duplicate or unexpected run in metrics.")
        indexed[rid] = run
    if set(indexed) != set(expected):
        raise ValueError("Missing prespecified run in metrics.")
    if {p.stem for p in (review / "runs").glob("*.json")} != set(expected):
        raise ValueError("Public per-run files do not match the prespecified matrix.")
    observed, hashes, configurations, usage_ids = [], set(), {}, set()
    for rid, (task, group, repeat) in expected.items():
        path = review / "runs" / (rid + ".json")
        if not path.resolve(strict=True).is_relative_to(review):
            raise ValueError("Public run file escapes the review directory.")
        document, run = load(path), indexed[rid]
        if document.get("run") != run or (run.get("task_id"), run.get("group"), run.get("repeat")) != (task, group, repeat) or type(run.get("repeat")) is not int or run.get("mode") != "development":
            raise ValueError("Public run identity or metrics copy disagrees: " + rid)
        if run.get("audit_status") != "verified_record" or run.get("execution_status") not in TERMINAL or run.get("correctness") is not None or run.get("human_active_seconds") is not None:
            raise ValueError("Run must have a verified terminal record and remain ungraded: " + rid)
        sources = run.get("source_hashes", {})
        if not isinstance(sources, dict) or not {"trace.json", "result.json"} <= set(sources):
            raise ValueError("Missing original trace/result hash identity: " + rid)
        for value in sources.values(): check_hash(value)
        if sources["trace.json"] in hashes:
            raise ValueError("Two run identities reuse the same original trace hash.")
        hashes.add(sources["trace.json"])
        check_hash(run.get("configuration_hash"))
        configurations.setdefault(group, set()).add(run["configuration_hash"])
        visible = document.get("visible", {})
        terminal = visible.get("terminal", {})
        if terminal.get("status") != run["execution_status"] or terminal.get("failure") != run.get("failure") or terminal.get("turn_end_reason") != run.get("turn_end_reason"):
            raise ValueError("Public terminal differs from the audited run: " + rid)
        tools = visible.get("tool_records")
        if not isinstance(tools, list) or any(not isinstance(t, dict) or not isinstance(t.get("name"), str) or not isinstance(t.get("arguments"), dict) for t in tools):
            raise ValueError("Malformed public tool records: " + rid)
        tool_names = dict(Counter(t["name"] for t in tools))
        if run.get("tool_call_count") != len(tools) or run.get("tool_calls_by_name") != tool_names:
            raise ValueError("Tool counts disagree with the public records: " + rid)
        if run.get("clarification_count") != len(visible.get("clarifications", [])):
            raise ValueError("Clarification counts disagree: " + rid)
        for key in ("api_request_count", "api_accounted_cny", "wall_seconds"):
            number(run.get(key), key, integer=key == "api_request_count")
        ids = run.get("usage_record_ids")
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids) or len(ids) != run["api_request_count"] or set(ids) & usage_ids:
            raise ValueError("API request identities/counts duplicate or disagree: " + rid)
        usage_ids.update(ids)
        check_hash(run.get("ledger_records_sha256"))
        calls = {name: {"invoked": False, "calls": []} for name in OPERATIONS}
        switches = []
        for index, tool in enumerate(tools, 1):
            if tool["name"] not in OPERATIONS:
                continue
            item, data = observe(tool, index)
            checks = data.get("study_checks")
            if checks is not None:
                pair = tuple(checks.get(k) for k in ("requirement_mapping", "evidence_checks"))
                if pair != FLAGS[group] or any(type(v) is not bool for v in pair):
                    raise ValueError("Observed checking flags differ from the protocol: " + rid)
                switches.append({"tool_index": index, **pick(checks, ("requirement_mapping", "evidence_checks", "version", "source_sha256"))})
            calls[tool["name"]]["invoked"] = True
            calls[tool["name"]]["calls"].append(item)
        observed.append({**pick(run, ("run_id", "task_id", "group", "repeat", "configuration_hash", "audit_status", "execution_status", "failure", "turn_end_reason", "api_request_count", "api_accounted_cny", "wall_seconds", "tool_call_count", "clarification_count", "source_hashes", "ledger_records_sha256")),
                         "public_record_sha256": sha(path), "correctness": "ungraded", "human_active_seconds": None,
                         "tool_calls_by_name": tool_names, "observed_checking_switches": switches, "operations": calls})
    if any(len(values) != 1 for values in configurations.values()) or len(set(next(iter(values)) for values in configurations.values())) != len(GROUPS):
        raise ValueError("Each ablation needs one distinct configuration identity.")
    counts = Counter(r["execution_status"] for r in observed)
    for state in (*TERMINAL, "missing", "partial", "invalid"):
        if metrics.get(state) != counts[state]:
            raise ValueError("Review terminal totals disagree with retained rows.")
    groups = {}
    for group in protocol["groups"]:
        rows = [r for r in observed if r["group"] == group]
        groups[group] = {"runs": len(rows), **{state: sum(r["execution_status"] == state for r in rows) for state in TERMINAL},
                         **{key: sum(r[key] for r in rows) for key in ("api_request_count", "api_accounted_cny", "wall_seconds", "tool_call_count", "clarification_count")}}
        declared = metrics.get("groups", {}).get(group, {})
        for key in ("api_request_count", "api_accounted_cny"):
            number(declared.get(key), key)
            if not math.isclose(declared[key], groups[group][key], rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("Review group accounting disagrees with retained rows.")
    return {"format": "matpilot-development-ablation-observations/v1", "status": "awaiting_user_review", "mode": "development",
            "protocol_sha256": sha(protocol_path), "public_review_metrics_sha256": sha(metrics_path),
            **pick(metrics, ("runtime_pack_sha256", "suite_sha256", "plan_sha256", "ledger_snapshot_sha256")),
            "planned_runs": len(expected), "observed_runs": len(observed), "groups": groups, "runs": observed,
            "correctness_scored": False, "improvement_or_speedup_estimated": False,
            "limitations": ["Known public development diagnostics; no generalization or causal effectiveness estimate.",
                            "Not invoked is represented by invoked=false and an empty calls list. Missing return fields stay absent; recorded null/false stay distinct.",
                            "A tool check, blocked plan or normal controller termination is not a task correctness grade.",
                            "Sampling attempts, returned structures, valid candidates and unique candidates are separate observed quantities.",
                            "Original private source hashes are checked for format and consistency with the audited public review; original bytes are not available to this script.",
                            "Public file hashes are recomputed. A protocol with null runtime_pack_sha256 needs its separate launch receipt to establish pre-run binding.",
                            "Costs are recorded accounting, not invoices. Wall time is not human time or scientific compute speed. Failed terminal runs remain included."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("protocol", "review_directory", "output"):
        parser.add_argument(name)
    args = parser.parse_args(argv)
    result = analyze(args.protocol, args.review_directory)
    with Path(args.output).open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"status": result["status"], "observed_runs": result["observed_runs"]}))


if __name__ == "__main__":
    main()
