"""Audit development Harness records and export a redacted, ungraded review pack.

No provider, material model, formal freeze or human grade is created here. The
report describes recorded execution, measured wall time and accounted API cost;
it never infers task correctness, scientific validity or time savings.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

from .evaluation import digest, save_new
from .study_workspace import _source_manifest, _verified_path, file_hash


FORMAT = "matpilot-development-review/v1"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,159}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
STATUSES = ("completed", "failed", "timeout", "budget_stopped", "missing", "partial", "invalid")
SECRET = re.compile(r"(?i)^(authorization|proxy-authorization|cookie|set-cookie|api[-_]?key|access[-_]?token|refresh[-_]?token|password|secret|client[-_]?secret)$")
OMIT = {"system_prompt", "systemPrompt", "instructions", "normal_instructions", "header", "driver_events",
        "request_headers", "requestHeaders", "session_id", "sessionId", "reasoning", "reasoning_content", "thinking",
        "stream"}


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON number: " + value)))


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(label + " must be a finite nonnegative measurement.")
    return value


def _hash(value, label):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("Missing SHA256 identity: " + label)
    return value


def _validate_pack(path):
    pack = _load(path)
    if pack.get("format") != "matpilot-study-runtime/v1" or pack.get("mode") != "development" or pack.get("real_compute_allowed") is not False:
        raise ValueError("Only a development runtime pack with real computation disabled is accepted.")
    for field in ("plan_sha256", "suite_sha256"):
        _hash(pack.get("provenance", {}).get(field), field)
    base, drivers = Path(path).resolve().parent, pack.get("drivers", {})
    if not {"U", "U+", "M"} <= set(drivers):
        raise ValueError("Development comparator drivers U/U+/M are required.")
    for group, driver in drivers.items():
        _hash(driver.get("configuration_hash"), group + " configuration")
        for name in ("scientific_capability_hash", "replay_fixture_hash", "clarification_policy_hash"):
            if driver.get(name) != pack["shared"].get(name):
                raise ValueError("Driver shared condition differs: " + name)
        resolved = {}
        for key, hash_key in (("module_path", "module_sha256"), ("instruction_path", "instruction_sha256"),
                              ("capability_path", "scientific_capability_hash"), ("source_manifest_path", "source_manifest_sha256"),
                              ("validation_report_path", "validation_report_sha256")):
            resolved[key] = _verified_path(driver, key, _hash(driver.get(hash_key), hash_key), base)
        manifest = _source_manifest(resolved["source_manifest_path"], driver["source_revision"])
        declared = {(Path(resolved["source_manifest_path"]).parent / item["path"]).resolve() for item in manifest["files"]}
        if any(Path(resolved[key]) not in declared for key in ("module_path", "instruction_path")):
            raise ValueError("Source manifest does not bind the driver and its instructions.")
        receipt = _load(resolved["validation_report_path"])
        if receipt.get("passed") is not True or receipt.get("configuration_sha256") != driver.get("validation_configuration_sha256") or not receipt.get("checks"):
            raise ValueError("Driver validation receipt is not bound to this configuration.")
    replay = pack["replay"]
    if replay["manifest_sha256"] != pack["shared"]["replay_fixture_hash"]:
        raise ValueError("Replay identity differs from shared conditions.")
    fixture_path = _verified_path(replay, "manifest_path", replay["manifest_sha256"], base)
    manifest = _load(fixture_path)
    if manifest.get("purpose") != "development":
        raise ValueError("Development report requires development replay fixtures.")
    tasks = {}
    for task in pack.get("tasks", []):
        tid = task.get("id")
        if not isinstance(tid, str) or not SAFE_ID.fullmatch(tid) or tid in tasks or not isinstance(task.get("prompt"), str) or not task["prompt"].strip() or task.get("fixture_id") not in manifest.get("fixtures", {}):
            raise ValueError("Invalid, duplicate or unresolved public task.")
        tasks[tid] = task
    rows = {}
    for row in pack.get("rows", []):
        if set(row) != {"run_id", "task_id", "group", "repeat", "mode", "configuration_hash"}:
            raise ValueError("Invalid development run row fields.")
        rid, group = row["run_id"], row["group"]
        if not isinstance(rid, str) or not SAFE_ID.fullmatch(rid) or rid in rows or row["task_id"] not in tasks or group not in drivers or type(row["repeat"]) is not int or row["repeat"] not in (1, 2, 3):
            raise ValueError("Invalid or duplicate development run identity.")
        if row["mode"] != "development" or row["configuration_hash"] != drivers[group]["configuration_hash"] or rid != f"{row['task_id']}--{group.replace('+', 'plus')}--{row['repeat']}":
            raise ValueError("Development row differs from its driver identity.")
        rows[rid] = row
    if not tasks or not rows:
        raise ValueError("Development tasks and runs must not be empty.")
    return pack, tasks, rows


class Sanitizer:
    """Keep public content complete while removing credentials and local identities."""
    def __init__(self):
        self.paths = {}
        self.redactions = 0

    def _path(self, match):
        value = match.group(0)
        self.redactions += 1
        self.paths.setdefault(value, "[LOCAL_ARTIFACT_" + str(len(self.paths) + 1) + "]")
        return self.paths[value]

    def clean(self, value):
        if isinstance(value, dict):
            if value.get("role") in {"system", "developer"} or value.get("type") in {"thinking", "reasoning"}:
                self.redactions += 1
                return {"omitted": "non-public system/reasoning content"}
            result = {}
            for key, child in value.items():
                if key in OMIT:
                    self.redactions += 1
                    continue
                if SECRET.fullmatch(key):
                    result[key] = "[REDACTED]"
                    self.redactions += 1
                else:
                    result[key] = self.clean(child)
            return result
        if isinstance(value, list):
            return [self.clean(child) for child in value if not isinstance(child, dict) or child.get("type") not in {"thinking", "reasoning"}]
        if not isinstance(value, str):
            return value
        # Tool outputs can be JSON encoded as text; redact its credential fields too.
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
                if isinstance(decoded, (dict, list)):
                    return json.dumps(self.clean(decoded), ensure_ascii=False, indent=2)
            except (ValueError, TypeError):
                pass
        original = value
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
        value = re.sub(r"(?i)\b(api[-_]?key|access[-_]?token|refresh[-_]?token|password|secret)[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+", r"\1=[REDACTED]", value)
        if value != original:
            self.redactions += 1
        # Quoted paths may contain spaces. Unquoted prose must keep the answer
        # following a path instead of greedily dropping the rest of that line.
        value = re.sub(r"(?i)(?P<quote>[\"'])(?P<path>(?:[a-z]:[\\/]|\\\\|/(?!/))[^\r\n\"']+)(?P=quote)",
                       lambda match: match.group('quote') + self._path_value(match.group('path')) + match.group('quote'), value)
        value = re.sub(r"(?i)file://[^\s\"'<>]+|(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\s\"'<>|]+", self._path, value)
        value = re.sub(r"(?<![\w:/])/(?!/)[^\s\"'<>]+", self._path, value)
        return value

    def _path_value(self, value):
        self.redactions += 1
        self.paths.setdefault(value, "[LOCAL_ARTIFACT_" + str(len(self.paths) + 1) + "]")
        return self.paths[value]


def _visible(trace, sanitizer):
    events = []
    allowed = {"user/message", "assistant/message", "message/user", "message/assistant", "tool/call", "tool/result", "turn/end"}
    for event in trace.get("events", []):
        if event.get("type") in allowed:
            events.append(sanitizer.clean({"type": event["type"], "time": event.get("time"), "data": event.get("data")}))
    return {"events": events, "tool_records": sanitizer.clean(trace.get("tools", [])),
            "clarifications": sanitizer.clean(trace.get("clarifications", [])),
            "terminal": sanitizer.clean(trace.get("terminal")),
            "notice": "完整公开消息与工具记录；系统提示、思考内容、驱动内部日志、凭据及本地绝对路径不进入本副本。工具记录可能与会话事件重复，保留以便核对。"}


def _ledger(path):
    ledger = _load(path)
    if not isinstance(ledger.get("records"), list):
        raise ValueError("Existing cumulative ledger must contain records.")
    lookup = {}
    for record in ledger["records"]:
        _number(record.get("accounted_cny"), "ledger accounted_cny")
        if "id" in record:
            if not isinstance(record["id"], str) or record["id"] in lookup:
                raise ValueError("Ledger request IDs must be unique strings.")
            lookup[record["id"]] = record
    total = sum(record["accounted_cny"] for record in ledger["records"])
    if "accounted_upper_bound_cny" in ledger and not math.isclose(ledger["accounted_upper_bound_cny"], total, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("Ledger cumulative amount differs from its records.")
    return ledger, lookup, total


def _cost(trace, row, ledger_records):
    ids = trace.get("usage_record_ids")
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("Trace needs explicit unique usage_record_ids.")
    if any(i not in ledger_records for i in ids):
        raise ValueError("Trace references a missing API ledger record.")
    records = [ledger_records[i] for i in ids]
    sessions = {r.get("session_id") for r in records}
    if records and (len(sessions) != 1 or not all(isinstance(s, str) and s for s in sessions) or any(r.get("run_id") != row["run_id"] for r in records)):
        raise ValueError("API records belong to a different run/session.")
    if records and {i for i, r in ledger_records.items() if r.get("session_id") in sessions} != set(ids):
        raise ValueError("Trace omits API records from this execution's session.")
    durations = []
    for record in records:
        status, pricing = record.get("status"), record.get("pricing", {})
        for key in ("input_cny_per_million", "output_cny_per_million", "max_input_tokens", "max_output_tokens"):
            _number(pricing.get(key), "API pricing " + key)
        if status == "usage_reported":
            usage = record.get("usage", {})
            _number(usage.get("inputTokens"), "inputTokens")
            _number(usage.get("outputTokens"), "outputTokens")
            for key in ("cacheReadTokens", "cacheWriteTokens"):
                _number(usage.get(key, 0), key)
            expected = ((usage["inputTokens"] + usage.get("cacheReadTokens", 0) + usage.get("cacheWriteTokens", 0)) * pricing["input_cny_per_million"] + usage["outputTokens"] * pricing["output_cny_per_million"]) / 1e6
        elif status in {"reserved", "reservation_retained"}:
            expected = (pricing["max_input_tokens"] * pricing["input_cny_per_million"] + pricing["max_output_tokens"] * pricing["output_cny_per_million"]) / 1e6
        else:
            raise ValueError("Unknown study API accounting status.")
        if not math.isclose(record["accounted_cny"], expected, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("API accounted amount differs from retained reservation/reported usage.")
        try:
            seconds = (datetime.fromisoformat(record["ended_at"].replace("Z", "+00:00")) - datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))).total_seconds()
            durations.append(_number(seconds, "request wall time"))
        except (ValueError, TypeError, KeyError):
            durations.append(None)
    return {"api_request_count": len(records), "api_accounted_cny": sum(r["accounted_cny"] for r in records),
            "reservation_retained_requests": sum(r["status"] == "reservation_retained" for r in records),
            "pending_reserved_requests": sum(r["status"] == "reserved" for r in records),
            "llm_request_wall_seconds": sum(durations) if all(d is not None for d in durations) else None,
            "usage_record_ids": ids, "ledger_records_sha256": digest(records),
            "accounting": "reported usage at conservative prices or retained reservation; not provider billing",
            "session_identity": next(iter(sessions)) if sessions else None}


def _trace_identity(trace, row, pack, pack_sha):
    if trace.get("format") != "matpilot-study-trace/v1":
        raise ValueError("Unknown Harness trace format.")
    identity = {k: row[k] for k in ("run_id", "task_id", "group", "repeat", "configuration_hash")}
    if trace.get("run_identity") != identity:
        raise ValueError("Trace run/task/group/repeat/configuration identity differs.")
    driver, provenance = pack["drivers"][row["group"]], trace.get("provenance", {})
    expected = {"plan_sha256": pack["provenance"]["plan_sha256"], "suite_sha256": pack["provenance"]["suite_sha256"],
                "runtime_pack_sha256": pack_sha, **{k: driver[k] for k in ("module_sha256", "source_manifest_sha256", "instruction_sha256")}}
    if any(provenance.get(k) != v for k, v in expected.items()):
        raise ValueError("Trace source/pack provenance differs from the pinned development pack.")
    for field in ("events", "tools", "clarifications"):
        if not isinstance(trace.get(field), list) or any(not isinstance(x, dict) for x in trace[field]):
            raise ValueError("Trace contains invalid event/tool/clarification records.")


def _audit_run(root, row, task, pack, pack_sha, ledger_records, sanitizer):
    folder = (root / "runs" / row["run_id"]).resolve()
    if not folder.is_relative_to(root.resolve()):
        raise ValueError("Run folder escapes the specified runner directory.")
    summary = {**row, "audit_status": "missing", "execution_status": "missing", "issues": [], "source_hashes": {},
               "review_status": "awaiting_user_review", "correctness": None, "human_active_seconds": None}
    visible = {"events": [], "tool_records": [], "clarifications": [], "terminal": None}
    trace = None
    if not folder.exists():
        return summary, visible
    summary.update(audit_status="partial", execution_status="partial")
    try:
        for name in ("result.json", "trace.json", "trace.partial.json", "events.jsonl", "journal.jsonl"):
            path = folder / name
            if path.exists():
                if not path.resolve().is_relative_to(folder):
                    raise ValueError("Run artifact escapes its run directory.")
                summary["source_hashes"][name] = file_hash(path)
        final = (folder / "result.json").exists() and (folder / "trace.json").exists()
        trace_path = folder / ("trace.json" if (folder / "trace.json").exists() else "trace.partial.json")
        if not trace_path.exists():
            summary["issues"].append("No final/partial trace; run folder retained without invented outcome.")
            return summary, visible
        trace = _load(trace_path)
        _trace_identity(trace, row, pack, pack_sha)
        visible = _visible(trace, sanitizer)
        costs = _cost(trace, row, ledger_records)
        summary.update(costs)
        summary.update(tool_call_count=len(trace["tools"]), clarification_count=len(trace["clarifications"]),
                       tool_calls_by_name=dict(Counter(t.get("name", "unknown") for t in trace["tools"])),
                       user_message_count=sum(e.get("type") in {"user/message", "message/user"} for e in trace["events"]),
                       assistant_message_count=sum(e.get("type") in {"assistant/message", "message/assistant"} for e in trace["events"]))
        if not final:
            summary["issues"].append("Missing final result/trace; partial events and accounted requests retained.")
            return summary, visible
        if costs["pending_reserved_requests"]:
            raise ValueError("Final execution still has unsettled API reservations; cost remains provisional.")
        if (folder / "events.jsonl").exists():
            logged_events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            if logged_events != trace["events"]:
                raise ValueError("Final trace events differ from the preserved event journal.")
        result = _load(folder / "result.json")
        if result.get("row") != row or result.get("trace_sha256") != summary["source_hashes"]["trace.json"] or result.get("correctness") != "ungraded":
            raise ValueError("Result row, original trace hash or ungraded status differs.")
        outcome = result.get("outcome", {})
        required = {"status", "failure", "turn_end_reason", "wall_seconds", "human_active_seconds", "api_cny", "science_cny", "science_seconds", "configuration_hash"}
        if not isinstance(outcome, dict) or set(outcome) != required or outcome.get("configuration_hash") != row["configuration_hash"]:
            raise ValueError("Outcome schema/configuration differs from the scheduled run.")
        status = outcome["status"]
        if status not in STATUSES[:4]:
            raise ValueError("Unknown terminal execution status.")
        terminal = trace.get("terminal")
        if not isinstance(terminal, dict) or any(terminal.get(k) != outcome[k] for k in ("status", "failure", "turn_end_reason")):
            raise ValueError("Outcome and terminal trace disagree.")
        last_end = next((e.get("data", {}).get("reason") for e in reversed(trace["events"]) if e.get("type") == "turn/end"), None)
        if terminal.get("turn_end_reason") != last_end or (status == "completed" and (not isinstance(last_end, dict) or last_end.get("kind") != "completed")):
            raise ValueError("Outcome is not supported by the actual final turn/end event.")
        if status == "completed" and outcome["failure"] is not None or status != "completed" and (not isinstance(outcome["failure"], str) or not outcome["failure"].strip()):
            raise ValueError("Terminal failure reason is inconsistent.")
        _number(outcome["wall_seconds"], "run wall time")
        _number(outcome["api_cny"], "run API cost")
        if not math.isclose(outcome["api_cny"], costs["api_accounted_cny"], rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("Outcome API cost differs from this session's full ledger records.")
        if outcome["human_active_seconds"] is not None or outcome["science_cny"] != 0 or outcome["science_seconds"] != 0:
            raise ValueError("This runner measures no human active time and performs no scientific compute.")
        summary.update(audit_status="verified_record", execution_status=status, wall_seconds=outcome["wall_seconds"],
                       failure=sanitizer.clean(outcome["failure"]), turn_end_reason=sanitizer.clean(last_end))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        summary.update(audit_status="invalid", execution_status="invalid")
        summary["issues"].append(sanitizer.clean(str(exc)))
        # Preserve visible evidence for inspection, but never promote its status/cost.
        if isinstance(trace, dict):
            try:
                visible = _visible(trace, sanitizer)
            except (ValueError, TypeError, AttributeError):
                pass
    return summary, visible


def _fence(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    runs = re.findall(r"`+", text)
    marker = "`" * max(3, max((len(run) + 1 for run in runs), default=3))
    return marker + "text\n" + text + "\n" + marker


def _transcript_markdown(row, prompt, visible):
    lines = ["# " + row["run_id"] + "：待用户审核", "", "执行状态仅描述宿主回合，不表示任务正确或材料性质已证实。",
             "", "记录状态：" + row["execution_status"] + "；来源审计：" + row["audit_status"] + "。",
             "", "## 原始公开题干", "", _fence(prompt)]
    if row.get("failure") or row.get("issues"):
        lines.extend(["", "## 已记录的失败或异常", "", _fence({"failure": row.get("failure"), "issues": row.get("issues", [])})])
    final_message = next((e.get("data", {}).get("message", e.get("data", {})) for e in reversed(visible["events"])
                          if e["type"] in {"assistant/message", "message/assistant"}), None)
    if final_message:
        content = final_message.get("content", [])
        text = "\n\n".join(item["text"] for item in content if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)) if isinstance(content, list) else content
        if isinstance(text, str) and text:
            lines.extend(["", "## 最后一次公开答复（未经人工判定）", "", _fence(text)])
    for index, event in enumerate(visible["events"], 1):
        lines.extend(["", "## " + str(index) + " · " + event["type"], "", _fence(event.get("data"))])
    lines.extend(["", "## 完整工具调用与返回记录", "", _fence(visible["tool_records"]),
                  "", "## 固定澄清交互", "", _fence(visible["clarifications"]), ""])
    return "\n".join(lines)


def build_development_report(pack_path, runner_directory, ledger_path, output, *, selected_ids=None):
    """Create-only review artifacts; missing/failed/invalid records never disappear."""
    pack, tasks, rows = _validate_pack(pack_path)
    pack_sha = file_hash(pack_path)
    ledger, ledger_records, ledger_total = _ledger(ledger_path)
    selection = list(rows) if selected_ids is None else selected_ids
    if not isinstance(selection, list) or any(not isinstance(i, str) for i in selection) or len(set(selection)) != len(selection) or not set(selection) <= set(rows):
        raise ValueError("Selected run IDs must be unique members of this runtime pack.")
    selected = set(selection)
    root = Path(runner_directory).resolve(strict=True)
    sanitizer, results, transcripts = Sanitizer(), [], {}
    for rid, row in rows.items():
        if rid not in selected:
            continue
        summary, visible = _audit_run(root, row, tasks[row["task_id"]], pack, pack_sha, ledger_records, sanitizer)
        results.append(summary)
        transcripts[rid] = {"run": summary, "prompt": sanitizer.clean(tasks[row["task_id"]]["prompt"]), "visible": visible}
    sessions = [r.get("session_identity") for r in results if r.get("session_identity")]
    if len(set(sessions)) != len(sessions):
        raise ValueError("The same API execution session is attributed to multiple runs.")
    for result in results:
        result.pop("session_identity", None)
    present = {p.name for p in (root / "runs").iterdir() if p.is_dir()} if (root / "runs").is_dir() else set()
    unexpected = sorted(present - set(rows))
    groups = {}
    for group in pack["drivers"]:
        group_rows = [r for r in results if r["group"] == group]
        statuses = Counter(r["execution_status"] for r in group_rows)
        trusted = [r for r in group_rows if r["audit_status"] in {"verified_record", "partial"} and "api_accounted_cny" in r]
        groups[group] = {"planned_runs": sum(r["group"] == group for r in rows.values()), "selected_runs": len(group_rows),
                         **{key: statuses[key] for key in STATUSES}, "cost_audited_runs": len(trusted),
                         "api_accounted_cny": sum(r["api_accounted_cny"] for r in trusted),
                         "api_request_count": sum(r["api_request_count"] for r in trusted),
                         "reservation_retained_requests": sum(r["reservation_retained_requests"] for r in trusted),
                         "observed_run_wall_seconds": sum(r.get("wall_seconds", 0) for r in trusted),
                         "run_wall_time_measured_count": sum("wall_seconds" in r for r in trusted),
                         "observed_llm_request_wall_seconds": sum(r["llm_request_wall_seconds"] for r in trusted if r["llm_request_wall_seconds"] is not None),
                         "human_active_seconds": None}
    counts = Counter(r["execution_status"] for r in results)
    metrics = {"format": FORMAT, "status": "awaiting_user_review", "mode": "development", "correctness_scored": False,
               "scientific_results_validated": False, "improvement_or_speedup_estimated": False,
               "runtime_pack_sha256": pack_sha, "suite_sha256": pack["provenance"]["suite_sha256"],
               "plan_sha256": pack["provenance"]["plan_sha256"], "ledger_snapshot_sha256": file_hash(ledger_path),
               "ledger_accounted_cumulative_cny": ledger_total, "selection_source": "all_planned" if selected_ids is None else "explicit_run_ids",
               "planned_runs": len(rows), "selected_runs": len(results), "not_selected_runs": len(rows) - len(results),
               **{key: counts[key] for key in STATUSES}, "groups": groups, "runs": results,
               "unselected_recorded_runs": sorted((present & set(rows)) - selected), "unexpected_run_directories": sanitizer.clean(unexpected),
               "redaction_actions": sanitizer.redactions,
               "limitations": ["Public development cases; no independent review, correctness score, formal effect estimate or scientific conclusion.",
                               "Completed means the Harness turn ended normally, not that the task answer was correct.",
                               "Wall time includes host/tool overhead; request duration is observed stream wall time, not pure model inference time.",
                               "API amounts are ledger accounting or retained reservations, not provider invoices. Failed requests remain included.",
                               "Partial records are retained; invalid records remain visible but their costs/statuses are excluded from verified aggregates.",
                               "Hashes validate supplied file identities, not the truth of host/reviewer assertions. Original records are not modified.",
                               "Human active time, correctness and speedup remain unmeasured; user review is still pending."]}
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "runs").mkdir()
    for rid, transcript in transcripts.items():
        save_new(output / "runs" / (rid + ".json"), transcript)
        (output / "runs" / (rid + ".md")).write_text(_transcript_markdown(transcript["run"], transcript["prompt"], transcript["visible"]), encoding="utf-8")
    save_new(output / "metrics.json", metrics)
    lines = ["# MatPilot 开发对照：待用户审核", "", "这里只汇总可核验的执行过程、请求费用与墙钟时间。尚未判断任务正确率、材料性质或提速。",
             "", f"计划 {len(rows)} 次；本次选择 {len(results)} 次；未选择 {len(rows) - len(results)} 次。",
             f"宿主回合正常结束 {counts['completed']} 次；失败 {counts['failed']} 次；超时 {counts['timeout']} 次；预算停止 {counts['budget_stopped']} 次；缺失 {counts['missing']} 次；中断/缺最终记录 {counts['partial']} 次；审计不通过 {counts['invalid']} 次。",
             "", "正常结束不等于任务正确。每条原始失败、缺失和异常均保留，费用上界不冒充实际账单。",
             "", "| 组别 | 计划 | 选择 | 正常结束 | 失败/超时/预算停止 | 缺失/中断/无效 | 已核对 API 金额（元） | 已记录运行墙钟（秒） |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for group, stats in groups.items():
        lines.append(f"| {group} | {stats['planned_runs']} | {stats['selected_runs']} | {stats['completed']} | {stats['failed'] + stats['timeout'] + stats['budget_stopped']} | {stats['missing'] + stats['partial'] + stats['invalid']} | {stats['api_accounted_cny']:.6f} | {stats['observed_run_wall_seconds']:.3f} |")
    lines.extend(["", "## 逐次记录", "", "| 运行 | 执行记录状态 | 待审记录 |", "|---|---|---|"])
    for result in results:
        rid = result["run_id"]
        lines.append(f"| {rid} | {result['execution_status']} | [完整题干、答复与工具记录](runs/{rid}.md) |")
    lines.extend(["", "## 审核边界", "", "请逐题核对需求是否保留、动作是否合理、候选是否对应、结论是否有证据。本包不预填通过意见。",
                  "API 请求历时不替代人工操作时间；运行状态不能用于宣称正确率、材料性能或相对提速。公开副本已排除系统、思考、凭据和本地绝对路径，原始轨迹保持不变。",
                  "", "机器可读计数、身份哈希、异常原因及分组计量见 [metrics.json](metrics.json)。", ""])
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pack", "runner_directory", "ledger", "output"):
        parser.add_argument(name)
    parser.add_argument("--run-id", action="append", dest="selected_ids")
    args = parser.parse_args(argv)
    result = build_development_report(args.pack, args.runner_directory, args.ledger, args.output, selected_ids=args.selected_ids)
    print(json.dumps({k: result[k] for k in ("status", "planned_runs", "selected_runs", *STATUSES)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
