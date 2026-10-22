"""Offline handoff between independent review, the Harness runner and analysis.

This module never calls a provider or a scientific model. Runtime exports contain
only public tasks and controller replies, never the scoring annotations. Review
attestations are recorded, not certified by this software.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import re

from .evaluation import (FLAGS, GROUPS, RUBRIC, SHARED, _validate_environment,
                         audit_suite, blind_packet, digest, load, record_run,
                         save_new, validate_grade, validate_plan)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _new_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    return path


def prepare_review(suite, output, fixture_manifest=None):
    """Create unsigned review materials; never prefill approval or independence."""
    audit = audit_suite(suite, formal=True)
    if not audit["valid"]:
        raise ValueError("; ".join(audit["issues"]))
    fixture = load(fixture_manifest) if fixture_manifest else None
    root = _new_dir(output)
    save_new(root / "suite.for-review.json", suite)
    review = {"suite_sha256": digest(suite), "approved": False,
              "independent_of_implementation": False, "reviewer": "",
              "materials_qualification": "", "reviewed_at": "", "notes": ""}
    save_new(root / "review.to-complete.json", review)
    if fixture is not None:
        (root / "fixtures.for-review.json").write_bytes(Path(fixture_manifest).read_bytes())
        save_new(root / "fixture-review.to-complete.json", {
            "manifest_sha256": file_hash(fixture_manifest), "approved": False,
            "independent_of_implementation": False, "reviewer": "",
            "materials_qualification": "", "reviewed_at": "", "notes": "",
            "fixture_ids": sorted({t["fixture_id"] for t in suite["tasks"]})})
    (root / "REVIEW.txt").write_text(
        "请由真实材料相关复核人员核对题目、可执行性、固定澄清、标准答案、候选身份与回放数据。\n"
        "先修改题库和数据，再重新生成本复核包；最后填写真实复核结论。内容改变后旧哈希作废。\n"
        "approved=false 和空白身份是待填写模板，不能当作已复核证明。\n"
        "本题库曾在开发中被查看，不是天然未见测试集；请审定、替换重叠题，并披露接触历史。\n"
        "科学数值如为合成夹具，只能评估工作流程，不能证明材料性质或真实模型效果。\n", encoding="utf-8")
    return {"suite_sha256": digest(suite), "status": "awaiting_external_review",
            "path": str(root.resolve())}


def _verified_path(inventory, key, expected, base):
    p = Path(inventory[key])
    p = p if p.is_absolute() else Path(base) / p
    p = p.resolve(strict=True)
    if not p.is_file() or file_hash(p) != expected:
        raise ValueError("File hash differs from pinned configuration: " + key)
    return str(p)


def _source_manifest(path, expected_revision):
    manifest = load(path)
    if manifest.get("source_revision") != expected_revision or not manifest.get("files"):
        raise ValueError("Source manifest revision/files do not match the driver.")
    base = Path(path).parent
    seen = set()
    for entry in manifest["files"]:
        rel = Path(entry["path"])
        resolved = (base / rel).resolve(strict=True)
        if rel.is_absolute() or not resolved.is_relative_to(base.resolve()) or str(rel) in seen:
            raise ValueError("Source manifest path escapes its root or is duplicated.")
        seen.add(str(rel))
        if file_hash(resolved) != entry["sha256"]:
            raise ValueError("Driver source dependency changed: " + str(rel))
    return manifest


def _driver(config, inventory, base, *, formal=True):
    module = _verified_path(inventory, "module_path", config["driver_sha256"], base)
    instructions = _verified_path(inventory, "instruction_path", config["instruction_hash"], base)
    capability = _verified_path(inventory, "capability_path", config["scientific_capability_hash"], base)
    validation = _verified_path(inventory, "validation_report_path", config["driver_validation"]["report_sha256"], base)
    report = load(validation)
    if report.get("configuration_sha256") != config["driver_validation"]["configuration_sha256"] or report.get("passed") is not True:
        raise ValueError("Driver validation report does not pass this exact configuration.")
    # A report must describe actual checks; these fields remain operator-supplied.
    if not isinstance(report.get("checks"), list) or not report["checks"] or not report.get("evidence_scope"):
        raise ValueError("Driver validation report needs checks and an evidence scope.")
    if formal and report["evidence_scope"] != "harness_replay_end_to_end":
        raise ValueError("Formal comparison requires actual Harness replay end-to-end validation, not CPU contract tests alone.")
    if inventory["source_manifest_sha256"] != config.get("source_manifest_sha256"):
        raise ValueError("Source manifest is not bound to the validated configuration.")
    manifest_path = _verified_path(inventory, "source_manifest_path", inventory["source_manifest_sha256"], base)
    manifest = _source_manifest(manifest_path, config["source_revision"])
    declared = {(Path(manifest_path).parent / e["path"]).resolve() for e in manifest["files"]}
    if Path(module) not in declared or Path(instructions) not in declared:
        raise ValueError("Source manifest must bind the driver and instructions.")
    options = dict(inventory.get("options", {}))
    if config.get("driver_options_hash") != digest(options):
        raise ValueError("Runtime driver options are not bound to the validated configuration.")
    for key in ("broker_path", "template_config"):
        if key in options:
            option_path = Path(options[key])
            option_path = option_path if option_path.is_absolute() else Path(base) / option_path
            if option_path.resolve(strict=True) not in declared:
                raise ValueError("Executable/config dependency is absent from the pinned source manifest: " + key)
            options[key] = str(option_path.resolve())
    for name in ("requirement_mapping", "evidence_checks", "ablation"):
        if name in options and options[name] != config.get(name):
            raise ValueError("Runtime feature flags differ from scheduled configuration.")
        options[name] = config.get(name)
    return {"module_path": module, "module_sha256": config["driver_sha256"],
            "source_revision": config["source_revision"],
            "instruction_path": instructions, "instruction_sha256": config["instruction_hash"],
            "capability_path": capability, "capability_sha256": config["scientific_capability_hash"],
            "source_manifest_path": manifest_path, "source_manifest_sha256": inventory["source_manifest_sha256"],
            "scientific_capability_hash": config["scientific_capability_hash"],
            "replay_fixture_hash": config["replay_fixture_hash"],
            "clarification_policy_hash": config["clarification_policy_hash"],
            "configuration_hash": digest(config), "driver_validated": True,
            "validation_report_path": validation,
            "validation_report_sha256": config["driver_validation"]["report_sha256"],
            "validation_configuration_sha256": config["driver_validation"]["configuration_sha256"], "options": options}


def _runtime_tasks(suite):
    return [{"id": t["id"], "prompt": t["prompt"], "fixture_id": t["fixture_id"],
             "fixed_clarifications": [{"slot": r["when"], "answer": r["reply"],
                                       "max_uses": r.get("max_uses", 1)} for r in t["fixed_clarifications"]]}
            for t in suite["tasks"]]


def export_runtime(frozen, plan, inventory, output, *, inventory_base="."):
    """Export a formal controller pack after checking the plan and actual files.

The runner must still verify files immediately before executing. Paths are local
absolute paths to preserve module dependencies; this pack is not a public release.
"""
    validate_plan(frozen, plan)
    configs = {**plan["environment"], **plan["ablation_environment"]}
    if set(inventory.get("drivers", {})) != set(configs):
        raise ValueError("Inventory must contain exactly the scheduled drivers.")
    drivers = {g: _driver(c, inventory["drivers"][g], inventory_base) for g, c in configs.items()}
    c = configs["U"]
    fixture_path = _verified_path(inventory, "fixture_manifest_path", c["replay_fixture_hash"], inventory_base)
    fixture_review = Path(inventory["fixture_review_path"])
    if not fixture_review.is_absolute():
        fixture_review = Path(inventory_base) / fixture_review
    fixture_review = fixture_review.resolve(strict=True)
    if file_hash(fixture_review) != inventory["fixture_review_sha256"]:
        raise ValueError("Fixture review file changed.")
    manifest, review = load(fixture_path), load(fixture_review)
    fixtures = manifest.get("fixtures", [])
    available = set(fixtures) if isinstance(fixtures, dict) else {f["id"] for f in fixtures}
    needed = {t["fixture_id"] for t in frozen["suite"]["tasks"]}
    if manifest.get("purpose") != "formal_workflow_study" or not needed <= available:
        raise ValueError("Formal runtime requires reviewed fixtures covering every task.")
    if review.get("manifest_sha256") != c["replay_fixture_hash"] or review.get("approved") is not True or review.get("independent_of_implementation") is not True:
        raise ValueError("Formal fixture review must bind the exact manifest.")
    if not all(isinstance(review.get(k), str) and review[k].strip() for k in ("reviewer", "materials_qualification", "reviewed_at", "notes")) or not available <= set(review.get("fixture_ids", [])):
        raise ValueError("Fixture review lacks identity, notes or complete coverage.")
    shared = {"provider": c["llm_provider"], "model": c["llm_version"],
              "reasoning_effort": c["reasoning_mode"], "host_version": c["host_version"]}
    shared.update({k: c[k] for k in ("max_turns", "max_output_tokens", "max_requests", "timeout_ms", "sampling_cap",
                                     "guidance_scale", "scientific_capability_hash", "replay_fixture_hash", "clarification_policy_hash")})
    shared["scientific_defaults"] = {"guidance_scale": c["guidance_scale"], "step_lr": 1e-5}
    pack = {"format": "matpilot-study-runtime/v1", "mode": "reviewed_replay", "real_compute_allowed": False,
            "provenance": {"plan_sha256": plan["plan_sha256"], "suite_sha256": frozen["suite_sha256"], "exporter_version": 1},
            "shared": shared, "drivers": drivers, "tasks": _runtime_tasks(frozen["suite"]), "rows": plan["runs"],
            "replay": {"manifest_path": fixture_path, "manifest_sha256": c["replay_fixture_hash"],
                       "review_path": str(fixture_review), "review_sha256": inventory["fixture_review_sha256"]},
            "limitations": ["Controller replies are released only through clarification tools.",
                            "File identity and review declarations do not independently certify scientific validity."]}
    save_new(output, pack)
    return pack


def export_development(suite, environment, inventory, output, *, inventory_base=".", repeats=1, seed=20261022):
    """Export public development cases without inventing an independent freeze."""
    audit = audit_suite(suite)
    if not audit["valid"] or suite.get("split") != "development":
        raise ValueError("Development export requires an explicit valid development split.")
    if type(repeats) is not int or repeats not in (1, 2, 3):
        raise ValueError("Development repeats must be 1, 2 or 3.")
    _validate_environment(environment, {}, [])
    if set(inventory.get("drivers", {})) != set(GROUPS):
        raise ValueError("Development export needs all three real comparator implementations.")
    drivers = {g: _driver(c, inventory["drivers"][g], inventory_base, formal=False) for g, c in environment.items()}
    c = environment["U"]
    fixture_path = _verified_path(inventory, "fixture_manifest_path", c["replay_fixture_hash"], inventory_base)
    manifest = load(fixture_path)
    from .study_replay import _validate_manifest
    _validate_manifest(manifest)
    if manifest.get("purpose") != "development" or not {t["fixture_id"] for t in suite["tasks"]} <= set(manifest["fixtures"]):
        raise ValueError("Development fixtures must cover every development task.")
    shared = {"provider": c["llm_provider"], "model": c["llm_version"], "reasoning_effort": c["reasoning_mode"],
              **{k: c[k] for k in SHARED if k not in {"llm_provider", "llm_version", "reasoning_mode"}}}
    shared["scientific_defaults"] = {"guidance_scale": c["guidance_scale"], "step_lr": 1e-5}
    tasks = _runtime_tasks(suite)
    rng = random.Random(seed)
    ordered = list(tasks)
    rng.shuffle(ordered)
    rows = []
    for task in ordered:
        for repeat in range(1, repeats + 1):
            groups = list(GROUPS)
            rng.shuffle(groups)
            for g in groups:
                rows.append({"run_id": task["id"] + "--" + g.replace("+", "plus") + "--" + str(repeat),
                             "task_id": task["id"], "group": g, "repeat": repeat, "mode": "development", "configuration_hash": digest(environment[g])})
    provenance = {"suite_sha256": digest(suite), "plan_sha256": digest({"suite_sha256": digest(suite),
                  "environment": environment, "runs": rows, "seed": seed}), "exporter_version": 1}
    pack = {"format": "matpilot-study-runtime/v1", "mode": "development", "real_compute_allowed": False,
            "provenance": provenance, "shared": shared, "drivers": drivers, "tasks": tasks, "rows": rows,
            "replay": {"manifest_path": fixture_path, "manifest_sha256": c["replay_fixture_hash"]},
            "limitations": ["Public development cases; no independent review or formal effectiveness result.",
                            "Controller replies are released only through clarification tools."]}
    save_new(output, pack)
    return pack


def import_run(frozen, plan, output_root, runner_row, outcome, trace_path):
    """Import one complete runner result, preserving failures and measured costs."""
    validate_plan(frozen, plan)
    if runner_row not in plan["runs"]:
        raise ValueError("Unexpected or altered runner row.")
    trace = load(trace_path)
    if trace.get("format") != "matpilot-study-trace/v1":
        raise ValueError("Unknown runner trace format.")
    identity = {k: runner_row[k] for k in ("run_id", "task_id", "group", "repeat", "configuration_hash")}
    if trace.get("run_identity") != identity:
        raise ValueError("Trace belongs to another run or configuration.")
    if trace.get("provenance", {}).get("plan_sha256") != plan["plan_sha256"] or trace.get("provenance", {}).get("suite_sha256") != frozen["suite_sha256"]:
        raise ValueError("Trace does not bind the frozen plan and suite.")
    config = {**plan["environment"], **plan["ablation_environment"]}[runner_row["group"]]
    for key, expected in (("module_sha256", config["driver_sha256"]), ("instruction_sha256", config["instruction_hash"]),
                          ("source_manifest_sha256", config.get("source_manifest_sha256"))):
        if not expected or trace["provenance"].get(key) != expected:
            raise ValueError("Trace driver provenance does not match scheduled " + key)
    if trace.get("terminal", {}).get("status") != outcome["status"] or trace.get("terminal", {}).get("failure") != outcome["failure"]:
        raise ValueError("Terminal trace and recorded outcome differ.")
    if outcome.get("science_cny") != 0 or outcome.get("science_seconds") != 0:
        raise ValueError("This replay-only study cannot import scientific compute measurements.")
    if "turn_end_reason" in outcome and outcome["turn_end_reason"] != trace.get("terminal", {}).get("turn_end_reason"):
        raise ValueError("Terminal turn reason changed.")
    last_end = next((e.get("data", {}).get("reason") for e in reversed(trace.get("events", [])) if e.get("type") == "turn/end"), None)
    if trace.get("terminal", {}).get("turn_end_reason") != last_end:
        raise ValueError("Terminal reason differs from the full event trace.")
    if outcome["status"] == "completed" and (not isinstance(last_end, dict) or last_end.get("kind") != "completed"):
        raise ValueError("Completed outcome requires an actual completed turn/end event.")
    fields = {"status", "failure", "wall_seconds", "human_active_seconds", "api_cny", "science_cny", "science_seconds", "configuration_hash"}
    if not fields <= set(outcome) or set(outcome) - fields - {"turn_end_reason"}:
        raise ValueError("Runner outcome contains missing or unrecognized measurements.")
    return record_run(output_root, runner_row, {k: outcome[k] for k in fields}, trace_path, frozen=frozen, plan=plan)


def _review_trace(trace, row):
    """A separate partial-blinding view; the full original remains in the registry."""
    omitted = {"group", "run_id", "session_id", "sessionId", "configuration_hash", "configuration_sha256",
               "plan_sha256", "source_revision", "environment", "provenance", "driver_identity",
               "usage_record_ids", "header", "driver_events", "system_prompt", "instructions"}
    replacements = {row["run_id"]: "[RUN]", row["configuration_hash"]: "[CONFIGURATION]"}
    artifacts = {}
    def clean(value):
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in omitted}
        if isinstance(value, str):
            for old, new in replacements.items():
                value = value.replace(old, new)
            # Preserve artifact equality, while removing machine/run path identity.
            def alias(match):
                raw = match.group(0)
                if raw not in artifacts:
                    artifacts[raw] = "[ARTIFACT_" + str(len(artifacts) + 1) + "]"
                return artifacts[raw]
            return re.sub(r"(?i)[a-z]:[\\/][^\s\"'<>]*|(?<![\w:/])/[^\s\"'<>]+", alias, value)
        return value
    if not isinstance(trace, dict):
        return clean(trace)
    # Explicitly omit prompts, headers, driver internals and provider request dumps.
    visible = {"user/message", "assistant/message", "message/user", "message/assistant", "tool/result", "turn/end"}
    events = []
    for event in trace.get("events", []):
        if event.get("type") in visible:
            events.append({"type": event["type"], "data": clean(event.get("data"))})
    return {"events": events, "tools": clean(trace.get("tools", [])),
            "clarifications": clean(trace.get("clarifications", [])), "terminal": clean(trace.get("terminal")),
            "review_scope": "Visible conversation and tool evidence only. System prompts, runtime headers and driver internals omitted. Tool behavior and free text may still reveal the group."}


def create_blind_review(frozen, plan, run_root, output, *, seed=20261022):
    """Keep the private identity mapping separate from the reviewer's directory."""
    validate_plan(frozen, plan)
    rows = list(plan["runs"])
    random.Random(seed).shuffle(rows)
    packets, mapping = [], {}
    tasks = {t["id"]: t for t in frozen["suite"]["tasks"]}
    for row in rows:
        run_file = Path(run_root) / row["run_id"] / "run.json"
        if not run_file.exists():
            continue
        record = load(run_file)
        if any(record.get(k) != v for k, v in row.items()) or record.get("plan_sha256") != plan["plan_sha256"]:
            raise ValueError("Recorded run differs from the review plan.")
        bid = "B" + str(len(packets) + 1).zfill(5)
        packet = blind_packet(run_root, row["run_id"], bid)
        trace = _review_trace(packet["trace"], row)
        packet["trace"] = trace
        packet["review_copy_sha256"] = digest(trace)
        packet["task"] = {"prompt": tasks[row["task_id"]]["prompt"], "gold": tasks[row["task_id"]]["gold"]}
        packet["reviewer"] = ""
        packet["grade"] = {k: None for k in (*RUBRIC, *FLAGS)}
        packets.append(packet)
        mapping[bid] = {"run_id": row["run_id"], "trace_sha256": packet["trace_sha256"],
                        "review_copy_sha256": packet["review_copy_sha256"], "packet_sha256": digest(packet)}
    root = _new_dir(output)
    reviewer_dir = root / "reviewer"
    reviewer_dir.mkdir()
    for packet in packets:
        save_new(reviewer_dir / (packet["blind_id"] + ".json"), packet)
    private = {"format": "matpilot-blind-map/v1", "plan_sha256": plan["plan_sha256"], "runs": mapping,
               "instruction": "Do not give this identity mapping to the reviewer. Review copies provide partial blinding only."}
    private["mapping_sha256"] = digest(private)
    save_new(root / "PRIVATE.mapping.json", private)
    return {"review_packets": len(packets), "missing_runs": len(rows) - len(packets),
            "status": "awaiting_external_grades", "partial_blinding": True}


def collect_grades(mapping, completed_packets):
    if mapping.get("mapping_sha256") != digest({k: v for k, v in mapping.items() if k != "mapping_sha256"}):
        raise ValueError("Private grading map changed.")
    grades, seen = {}, set()
    for packet in completed_packets:
        bid = packet.get("blind_id")
        if bid not in mapping["runs"] or bid in seen:
            raise ValueError("Unknown or duplicate blinded grade.")
        seen.add(bid)
        entry = mapping["runs"][bid]
        if packet.get("trace_sha256") != entry["trace_sha256"] or packet.get("review_copy_sha256") != entry["review_copy_sha256"] or digest(packet.get("trace")) != entry["review_copy_sha256"]:
            raise ValueError("Grade is bound to a different or altered trace.")
        original = {**packet, "reviewer": "", "evidence_notes": None,
                    "grade": {k: None for k in (*RUBRIC, *FLAGS)}}
        if digest(original) != entry["packet_sha256"]:
            raise ValueError("Reviewer changed task or other frozen packet fields.")
        grade = {**packet["grade"], "reviewer": packet["reviewer"], "evidence_notes": packet["evidence_notes"],
                 "trace_sha256": entry["trace_sha256"], "review_copy_sha256": entry["review_copy_sha256"]}
        validate_grade(grade)
        grades[entry["run_id"]] = grade
    return grades


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("review-packet")
    p.add_argument("suite"); p.add_argument("output"); p.add_argument("--fixtures")
    p = commands.add_parser("export-runtime")
    p.add_argument("frozen"); p.add_argument("plan"); p.add_argument("inventory"); p.add_argument("output")
    p = commands.add_parser("export-development")
    p.add_argument("suite"); p.add_argument("environment"); p.add_argument("inventory"); p.add_argument("output")
    p.add_argument("--repeats", type=int, default=1)
    p = commands.add_parser("import-run")
    for name in ("frozen", "plan", "output", "row", "outcome", "trace"):
        p.add_argument(name)
    p = commands.add_parser("blind")
    for name in ("frozen", "plan", "runs", "output"):
        p.add_argument(name)
    p = commands.add_parser("collect-grades")
    p.add_argument("mapping"); p.add_argument("reviewer_directory"); p.add_argument("output")
    args = parser.parse_args()
    if args.command == "review-packet":
        result = prepare_review(load(args.suite), args.output, args.fixtures)
    elif args.command == "export-runtime":
        pack = export_runtime(load(args.frozen), load(args.plan), load(args.inventory), args.output,
                              inventory_base=Path(args.inventory).resolve().parent)
        result = {"status": "exported_not_executed", "rows": len(pack["rows"])}
    elif args.command == "export-development":
        pack = export_development(load(args.suite), load(args.environment), load(args.inventory), args.output,
                                  inventory_base=Path(args.inventory).resolve().parent, repeats=args.repeats)
        result = {"status": "development_exported_not_executed", "rows": len(pack["rows"])}
    elif args.command == "import-run":
        result = import_run(load(args.frozen), load(args.plan), args.output, load(args.row), load(args.outcome), args.trace)
    elif args.command == "blind":
        result = create_blind_review(load(args.frozen), load(args.plan), args.runs, args.output)
    else:
        grades = collect_grades(load(args.mapping), [load(p) for p in sorted(Path(args.reviewer_directory).glob("B*.json"))])
        save_new(args.output, grades)
        result = {"grades": len(grades), "status": "collected_operator_supplied_grades"}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
