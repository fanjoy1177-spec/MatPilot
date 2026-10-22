"""Offline handoff between independent review, the Harness runner and analysis.

This module never calls a provider or a scientific model. Runtime exports contain
only public tasks and controller replies, never the scoring annotations. Review
attestations are recorded, not certified by this software.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random

from .evaluation import (ABLATIONS, FLAGS, GROUPS, RUBRIC, SHARED, _validate_configuration, _validate_environment,
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


def _formal_runtime(frozen, plan, inventory, *, inventory_base="."):
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
    from .study_replay import _validate_manifest
    _validate_manifest(manifest)
    fixtures = manifest.get("fixtures", [])
    available = set(fixtures) if isinstance(fixtures, dict) else {f["id"] for f in fixtures}
    needed = {t["fixture_id"] for t in frozen["suite"]["tasks"]}
    if manifest.get("purpose") != "formal_workflow_study" or not needed <= available:
        raise ValueError("Formal runtime requires reviewed fixtures covering every task.")
    if review.get("manifest_sha256") != c["replay_fixture_hash"] or review.get("approved") is not True or review.get("independent_of_implementation") is not True:
        raise ValueError("Formal fixture review must bind the exact manifest.")
    if (not all(isinstance(review.get(k), str) and review[k].strip() for k in ("reviewer", "materials_qualification", "reviewed_at", "notes"))
            or available != set(review.get("fixture_ids", [])) or review["reviewer"] == manifest["author"]):
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
    # Preserve the original, pre-resolution options so their scheduled hash can
    # be checked again during import. This is a private local controller pack.
    pack["source_inventory"] = {"base_path": str(Path(inventory_base).resolve()), "inventory": inventory}
    return pack


def export_runtime(frozen, plan, inventory, output, *, inventory_base="."):
    """Create a reviewed replay pack; never run a provider or scientific model."""
    pack = _formal_runtime(frozen, plan, inventory, inventory_base=inventory_base)
    save_new(output, pack)
    return pack


def validate_runtime(frozen, plan, runtime_path):
    """Recheck exact formal plan, review, dependencies, options and public tasks."""
    pack = load(runtime_path)
    if pack.get("mode") != "reviewed_replay" or pack.get("real_compute_allowed") is not False:
        raise ValueError("Formal import requires a reviewed replay runtime; development is not a formal result.")
    inventory = pack.get("source_inventory", {})
    if set(inventory) != {"base_path", "inventory"}:
        raise ValueError("Runtime lacks the original bound source inventory; re-export it before running.")
    expected = _formal_runtime(frozen, plan, inventory["inventory"], inventory_base=inventory["base_path"])
    if pack != expected:
        raise ValueError("Runtime differs from the frozen plan, reviewed fixtures or validated driver inventory.")
    return pack


def development_selection(suite, task_ids=None, groups=None):
    """Validate an explicit public subset before any preparation side effects."""
    audit = audit_suite(suite)
    if not audit["valid"] or suite.get("split") != "development":
        raise ValueError("Development export requires an explicit valid development split.")
    available = [task["id"] for task in suite["tasks"]]
    selected_ids = available if task_ids is None else task_ids
    selected_groups = list(GROUPS) if groups is None else groups
    for label, selected, allowed in (("task_ids", selected_ids, available),
                                     ("groups", selected_groups, (*GROUPS, *ABLATIONS))):
        if (not isinstance(selected, (list, tuple)) or not selected
                or not all(isinstance(value, str) for value in selected)
                or len(set(selected)) != len(selected) or not set(selected) <= set(allowed)):
            raise ValueError("Development " + label + " must be a nonempty selection without unknown or duplicate values.")
    if any(group in ABLATIONS for group in selected_groups) and "M" not in selected_groups:
        raise ValueError("Development ablations require the matched M control in selected groups.")
    return list(selected_ids), list(selected_groups)


def export_development(suite, environment, inventory, output, *, inventory_base=".", repeats=1, seed=20261022,
                       task_ids=None, groups=None):
    """Export public development cases without inventing an independent freeze."""
    selected_ids, selected_groups = development_selection(suite, task_ids, groups)
    if type(repeats) is not int or repeats not in (1, 2, 3):
        raise ValueError("Development repeats must be 1, 2 or 3.")
    if type(seed) is not int:
        raise ValueError("Development randomization seed must be an integer.")
    required_groups = set(GROUPS) | set(selected_groups)
    if (not isinstance(environment, dict) or set(environment) != required_groups
            or set(inventory.get("drivers", {})) != required_groups):
        raise ValueError("Development export needs all three comparator definitions and exactly the selected ablation drivers.")
    _validate_environment({g: environment[g] for g in GROUPS}, {}, [])
    for group in selected_groups:
        if group not in ABLATIONS:
            continue
        config = environment[group]
        _validate_configuration(config, group)
        # A component ablation changes executed switches, not instructions, model or source.
        for key in (*SHARED, "source_revision", "source_manifest_sha256", "baseline_patch_hash",
                    "driver_sha256", "instruction_hash"):
            if config.get(key) != environment["M"].get(key):
                raise ValueError("Development ablation changed a non-ablated condition: " + key)
        options = inventory["drivers"][group].get("options", {})
        reference = inventory["drivers"]["M"].get("options", {})
        flags = {"implementation_group", "requirement_mapping", "evidence_checks", "ablation"}
        if {k: v for k, v in options.items() if k not in flags} != {k: v for k, v in reference.items() if k not in flags}:
            raise ValueError("Development ablation changed a non-ablated driver option.")
        for name in ("M", group):
            bound = inventory["drivers"][name].get("options", {})
            if bound.get("implementation_group") != name or any(
                    key not in bound or bound[key] != environment[name][key]
                    for key in ("requirement_mapping", "evidence_checks", "ablation")):
                raise ValueError("Development ablation requires explicitly bound implementation and feature options.")
    drivers = {g: _driver(c, inventory["drivers"][g], inventory_base, formal=False) for g, c in environment.items()}
    c = environment["U"]
    fixture_path = _verified_path(inventory, "fixture_manifest_path", c["replay_fixture_hash"], inventory_base)
    manifest = load(fixture_path)
    from .study_replay import _validate_manifest
    _validate_manifest(manifest)
    selected_suite = {**suite, "tasks": [next(t for t in suite["tasks"] if t["id"] == tid) for tid in selected_ids]}
    if manifest.get("purpose") != "development" or not {t["fixture_id"] for t in selected_suite["tasks"]} <= set(manifest["fixtures"]):
        raise ValueError("Development fixtures must cover every development task.")
    shared = {"provider": c["llm_provider"], "model": c["llm_version"], "reasoning_effort": c["reasoning_mode"],
              **{k: c[k] for k in SHARED if k not in {"llm_provider", "llm_version", "reasoning_mode"}}}
    shared["scientific_defaults"] = {"guidance_scale": c["guidance_scale"], "step_lr": 1e-5}
    tasks = _runtime_tasks(selected_suite)
    rng = random.Random(seed)
    ordered = list(tasks)
    rng.shuffle(ordered)
    rows = []
    for task in ordered:
        for repeat in range(1, repeats + 1):
            order = list(selected_groups)
            rng.shuffle(order)
            for g in order:
                rows.append({"run_id": task["id"] + "--" + g.replace("+", "plus") + "--" + str(repeat),
                             "task_id": task["id"], "group": g, "repeat": repeat, "mode": "development", "configuration_hash": digest(environment[g])})
    selection = {"task_ids": selected_ids, "groups": selected_groups, "repeats": repeats,
                 "random_seed": seed, "available_task_count": len(suite["tasks"]),
                 "scope": "public_development_subset_not_formal_effectiveness"}
    provenance = {"suite_sha256": digest(suite), "selection_sha256": digest(selection),
                  "plan_sha256": digest({"suite_sha256": digest(suite), "selection": selection,
                  "environment": environment, "runs": rows, "seed": seed}), "exporter_version": 2}
    pack = {"format": "matpilot-study-runtime/v1", "mode": "development", "real_compute_allowed": False,
            "provenance": provenance, "selection": selection, "shared": shared, "drivers": drivers, "tasks": tasks, "rows": rows,
            "replay": {"manifest_path": fixture_path, "manifest_sha256": c["replay_fixture_hash"]},
            "limitations": ["Public development cases; no independent review or formal effectiveness result.",
                            "Controller replies are released only through clarification tools."]}
    save_new(output, pack)
    return pack


def _audit_import(row, pack, runtime_path, ledger_path, result_path, trace_path, events_path):
    """Audit original artifacts before any registry mutation. No grades inferred."""
    from .development_report import _cost, _ledger, _load, _number, _trace_identity
    paths = {"runtime.json": Path(runtime_path), "result.json": Path(result_path),
             "trace.json": Path(trace_path), "events.jsonl": Path(events_path)}
    payloads = {name: path.read_bytes() for name, path in paths.items()}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}
    if _load(runtime_path) != pack:
        raise ValueError("Runtime changed during import.")
    trace, result = _load(trace_path), _load(result_path)
    if not isinstance(trace, dict) or not isinstance(result, dict):
        raise ValueError("Original trace and result must be JSON objects.")
    _trace_identity(trace, row, pack, hashes["runtime.json"])
    events = [json.loads(line) for line in payloads["events.jsonl"].decode("utf-8").splitlines() if line.strip()]
    if events != trace["events"]:
        raise ValueError("Final trace events differ from the preserved event journal.")
    if result.get("row") != row or result.get("trace_sha256") != hashes["trace.json"] or result.get("correctness") != "ungraded":
        raise ValueError("Result row, original trace hash or ungraded status differs.")
    outcome = result.get("outcome", {})
    fields = {"status", "failure", "turn_end_reason", "wall_seconds", "human_active_seconds", "api_cny", "science_cny", "science_seconds", "configuration_hash"}
    if not isinstance(outcome, dict) or set(outcome) != fields or outcome.get("configuration_hash") != row["configuration_hash"]:
        raise ValueError("Runner outcome contains missing or unrecognized measurements/configuration.")
    from .evaluation import _validate_outcome
    _validate_outcome(row, {k: v for k, v in outcome.items() if k != "turn_end_reason"})
    terminal = trace.get("terminal", {})
    if not isinstance(terminal, dict) or any(terminal.get(key) != outcome[key] for key in ("status", "failure", "turn_end_reason")):
        raise ValueError("Terminal trace and recorded outcome differ.")
    last_end = next((e.get("data", {}).get("reason") for e in reversed(events) if e.get("type") == "turn/end"), None)
    if terminal.get("turn_end_reason") != last_end:
        raise ValueError("Terminal reason differs from the full event trace.")
    if outcome["status"] == "completed" and (not isinstance(last_end, dict) or last_end.get("kind") != "completed"):
        raise ValueError("Completed outcome requires an actual completed turn/end event.")
    if outcome["human_active_seconds"] is not None or outcome["science_cny"] != 0 or outcome["science_seconds"] != 0:
        raise ValueError("This replay runner measures no human active time and performs no scientific computation.")
    _number(outcome["wall_seconds"], "wall_seconds")
    _number(outcome["api_cny"], "api_cny")
    ledger_sha = file_hash(ledger_path)
    _, lookup, _ = _ledger(ledger_path)
    cost = _cost(trace, row, lookup)
    # The current runner trace has no independent allocation/session receipt.
    # With no IDs, a pre-response failure cannot be distinguished from omitted
    # charged requests in a previous session with the same scheduled run ID.
    # Fail closed instead of accepting an invented zero-cost result.
    if not cost["usage_record_ids"]:
        raise ValueError("Formal import cannot certify zero requests without independent session/allocation evidence; empty usage_record_ids are retained as invalid.")
    if cost["pending_reserved_requests"]:
        raise ValueError("Final execution still has unsettled API reservations.")
    if not math.isclose(outcome["api_cny"], cost["api_accounted_cny"], rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("Outcome API cost differs from this session's complete ledger records.")
    records = [lookup[key] for key in trace["usage_record_ids"]]
    if records and [key for key, item in lookup.items() if item.get("session_id") == cost["session_identity"]] != trace["usage_record_ids"]:
        raise ValueError("API ledger order differs from the trace request sequence.")
    # Reported assistant usage must appear in order in the ledger. Failed
    # requests may incur a retained reservation or usage without a final message.
    cursor = 0
    token_keys = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
    reported = [item["usage"] for item in records if item["status"] == "usage_reported"]
    for event in events:
        if event.get("type") not in {"assistant/message", "message/assistant"}:
            continue
        if not records:
            raise ValueError("Assistant messages cannot have an empty API request ledger.")
        usage = event.get("data", {}).get("usage")
        if usage is None:
            continue
        if not isinstance(usage, dict) or any(type(usage.get(key, 0)) is not int or usage.get(key, 0) < 0 for key in token_keys):
            raise ValueError("Assistant usage must contain nonnegative integer token counts.")
        while cursor < len(reported) and any(usage.get(key, 0) != reported[cursor].get(key, 0) for key in token_keys):
            cursor += 1
        if cursor == len(reported):
            raise ValueError("Assistant usage is absent from the ordered API ledger.")
        cursor += 1
    if file_hash(ledger_path) != ledger_sha or any(file_hash(path) != hashes[name] for name, path in paths.items()):
        raise ValueError("Source artifacts changed during import; retry after the writer stops.")
    payloads["ledger-records.json"] = (json.dumps({"records": records}, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    hashes["ledger-records.json"] = hashlib.sha256(payloads["ledger-records.json"]).hexdigest()
    audit = {"format": "matpilot-formal-import-audit/v1", "plan_sha256": pack["provenance"]["plan_sha256"],
             "suite_sha256": pack["provenance"]["suite_sha256"], "run_id": row["run_id"],
             "source_hashes": hashes, "source_ledger_sha256": ledger_sha, "cost": cost,
             "review_status": "ungraded", "scope": "artifact_identity_and_accounting_not_scientific_validity"}
    return outcome, audit, payloads


def _archive_import(frozen, plan, output_root, row, outcome, audit, payloads, trace_path):
    record = record_run(output_root, row, {k: v for k, v in outcome.items() if k != "turn_end_reason"},
                        trace_path, frozen=frozen, plan=plan, import_audit=audit)
    folder = Path(output_root) / row["run_id"]
    for name, data in payloads.items():
        if name != "trace.json":
            with (folder / name).open("xb") as stream:
                stream.write(data)
    return record


def import_run(frozen, plan, output_root, runner_row, outcome, trace_path, *, runtime_path=None,
               ledger_path=None, result_path=None, events_path=None):
    """Import one audited result. Original pack/result/journal/ledger are required.

    The old row/outcome arguments remain as cross-checks, not trusted measurements.
    Calls omitting provenance files now fail closed; use import-runs for a batch.
    """
    if any(p is None for p in (runtime_path, ledger_path, result_path, events_path)):
        raise ValueError("Formal import requires runtime_path, ledger_path, result_path and events_path.")
    pack = validate_runtime(frozen, plan, runtime_path)
    if runner_row not in plan["runs"]:
        raise ValueError("Unexpected or altered runner row.")
    audited, audit, payloads = _audit_import(runner_row, pack, runtime_path, ledger_path, result_path, trace_path, events_path)
    if audited != outcome:
        raise ValueError("Supplied outcome differs from the original audited result.")
    return _archive_import(frozen, plan, output_root, runner_row, audited, audit, payloads, trace_path)


def import_runs(frozen, plan, runtime_path, runner_directory, ledger_path, output_root):
    """Create a complete planned-run audit index; retain failures and missing rows.

    Invalid/partial rows remain explicit in import-index.json and in their source
    directory. They are never converted to zero-cost successful registry records.
    """
    pack = validate_runtime(frozen, plan, runtime_path)
    source = Path(runner_directory).resolve(strict=True)
    root = Path(output_root)
    if root.exists():
        raise FileExistsError(root)
    staged, entries, sessions = [], [], set()
    for row in plan["runs"]:
        folder = (source / "runs" / row["run_id"]).resolve()
        if not folder.is_relative_to(source):
            raise ValueError("Run directory escapes the runner root.")
        names = ("result.json", "trace.json", "events.jsonl")
        entry = {"run_id": row["run_id"], "status": "missing", "issues": [], "source_hashes": {}}
        try:
            for name in names:
                path = folder / name
                if path.exists():
                    if not path.resolve().is_relative_to(folder):
                        raise ValueError("Run artifact escapes its directory.")
                    entry["source_hashes"][name] = file_hash(path)
            if len(entry["source_hashes"]) == len(names):
                outcome, audit, payloads = _audit_import(row, pack, runtime_path, ledger_path,
                                                       folder / names[0], folder / names[1], folder / names[2])
                session = audit["cost"]["session_identity"]
                if session and session in sessions:
                    raise ValueError("One API session is attributed to multiple runs.")
                if session:
                    sessions.add(session)
                staged.append((row, outcome, audit, payloads, folder / "trace.json"))
                entry.update(status="verified_record", execution_status=outcome["status"], api_cny=outcome["api_cny"])
            elif folder.exists():
                entry.update(status="partial", issues=["Final result, trace or event journal is missing; no outcome invented."])
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
            entry.update(status="invalid", issues=[str(exc)])
        entries.append(entry)
    present = {p.name for p in (source / "runs").iterdir() if p.is_dir()} if (source / "runs").exists() else set()
    unexpected = sorted(present - {row["run_id"] for row in plan["runs"]})
    # No results are written until every supplied row has been inspected.
    root.mkdir(parents=True, exist_ok=False)
    for args in staged:
        _archive_import(frozen, plan, root, *args)
    index = {"format": "matpilot-formal-import-index/v1", "plan_sha256": plan["plan_sha256"],
             "runtime_pack_sha256": file_hash(runtime_path), "planned_runs": len(plan["runs"]),
             "imported_runs": len(staged), "runs": entries, "unexpected_source_directories": unexpected,
             "status": "imported_ungraded" if len(staged) == len(plan["runs"]) and not unexpected else "incomplete_do_not_interpret_effectiveness"}
    index["index_sha256"] = digest(index)
    save_new(root / "import-index.json", index)
    return index


def _review_trace(trace, row):
    """A separate partial-blinding view; the full original remains in the registry."""
    omitted = {"group", "run_id", "session_id", "sessionId", "configuration_hash", "configuration_sha256",
               "plan_sha256", "source_revision", "environment", "provenance", "driver_identity",
               "usage_record_ids", "header", "driver_events", "system_prompt", "instructions"}
    from .development_report import Sanitizer
    sanitizer = Sanitizer()
    replacements = {row["run_id"]: "[RUN]", row["configuration_hash"]: "[CONFIGURATION]"}
    def clean(value):
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in omitted}
        if isinstance(value, str):
            for old, new in replacements.items():
                value = value.replace(old, new)
            # Encoded tool JSON must undergo identity removal as well as secret
            # redaction. Retain the text representation expected by reviewers.
            if value.lstrip().startswith(("{", "[")):
                try:
                    decoded = json.loads(value)
                    if isinstance(decoded, (dict, list)):
                        return json.dumps(clean(decoded), ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
            return value
        return value
    if not isinstance(trace, dict):
        return clean(sanitizer.clean(trace))
    # Explicitly omit prompts, headers, driver internals and provider request dumps.
    visible = {"user/message", "assistant/message", "message/user", "message/assistant", "tool/call", "tool/result", "turn/end"}
    events = []
    for event in trace.get("events", []):
        if event.get("type") in visible:
            events.append({"type": event["type"], "data": clean(sanitizer.clean(event.get("data")))})
    return {"events": events, "tools": clean(sanitizer.clean(trace.get("tools", []))),
            "clarifications": clean(sanitizer.clean(trace.get("clarifications", []))), "terminal": clean(sanitizer.clean(trace.get("terminal"))),
            "review_scope": "Visible conversation and tool evidence only. System prompts, runtime headers and driver internals omitted. Tool behavior and free text may still reveal the group."}


def _validated_archive_records(frozen, plan, run_root):
    """Read-only verification of copied evidence, never trust run.json alone."""
    from .evaluation import _validate_record_integrity
    validate_plan(frozen, plan)
    root = Path(run_root).resolve(strict=True)
    rows = {r["run_id"]: r for r in plan["runs"]}
    folders = {p.name: p for p in root.iterdir() if p.is_dir()}
    if set(folders) - set(rows):
        raise ValueError("Unexpected run directory in the formal registry.")
    records, packs, sessions = [], {}, set()
    for rid, folder in folders.items():
        row = rows[rid]
        record = load(folder / "run.json")
        _validate_record_integrity(record)
        if any(record.get(k) != v for k, v in row.items()) or record.get("plan_sha256") != plan["plan_sha256"]:
            raise ValueError("Archived run differs from the planned identity.")
        audit = load(folder / "audit.json")
        if (audit.get("format") != "matpilot-formal-import-audit/v1" or record.get("import_audit_sha256") != digest(audit)
                or audit.get("run_id") != rid or audit.get("plan_sha256") != plan["plan_sha256"]
                or audit.get("suite_sha256") != frozen["suite_sha256"]):
            raise ValueError("Formal import audit is missing, changed or mismatched.")
        names = {"runtime.json", "result.json", "trace.json", "events.jsonl", "ledger-records.json"}
        if set(audit.get("source_hashes", {})) != names:
            raise ValueError("Formal import audit has incomplete source evidence.")
        for name, expected in audit["source_hashes"].items():
            path = folder / name
            if not path.resolve(strict=True).is_relative_to(root) or file_hash(path) != expected:
                raise ValueError("Archived evidence changed: " + name)
        pack_sha = audit["source_hashes"]["runtime.json"]
        if pack_sha not in packs:
            packs[pack_sha] = validate_runtime(frozen, plan, folder / "runtime.json")
        outcome, verified, _ = _audit_import(row, packs[pack_sha], folder / "runtime.json", folder / "ledger-records.json",
                                             folder / "result.json", folder / "trace.json", folder / "events.jsonl")
        if (record["trace_sha256"] != audit["source_hashes"]["trace.json"]
                or record["outcome"] != {k: v for k, v in outcome.items() if k != "turn_end_reason"}
                or verified["cost"] != audit["cost"]):
            raise ValueError("Archived outcome, trace or ledger cost differs from its audit.")
        session = audit["cost"]["session_identity"]
        if session and session in sessions:
            raise ValueError("The same API session is archived more than once.")
        if session:
            sessions.add(session)
        records.append(record)
    index_path = root / "import-index.json"
    if index_path.exists():
        index = load(index_path)
        entries = index.get("runs", [])
        if (index.get("index_sha256") != digest({k: v for k, v in index.items() if k != "index_sha256"})
                or index.get("plan_sha256") != plan["plan_sha256"] or index.get("planned_runs") != len(rows)
                or len(entries) != len(rows) or {r.get("run_id") for r in entries} != set(rows)
                or {r["run_id"] for r in entries if r.get("status") == "verified_record"} != set(folders)
                or index.get("imported_runs") != len(records)):
            raise ValueError("Batch import index changed or no longer matches archived records.")
        if index.get("unexpected_source_directories"):
            raise ValueError("Unexpected source runs require an explicit provenance review before formal analysis.")
    return records


def summarize_archive(frozen, plan, run_root, grades, *, bootstrap_samples=2000):
    """File-backed analysis; missing and ungraded runs retain the plan denominator."""
    from .evaluation import summarize
    records = _validated_archive_records(frozen, plan, run_root)
    result = summarize(frozen, plan, records, grades, bootstrap_samples=bootstrap_samples)
    result["archive_audit"] = {"verified_records": len(records), "original_artifacts_rechecked": True,
                               "grades": "operator_supplied_not_generated_by_this_command"}
    return result


def create_blind_review(frozen, plan, run_root, output, *, seed=20261022):
    """Keep the private identity mapping separate from the reviewer's directory."""
    validate_plan(frozen, plan)
    checked = {r["run_id"]: r for r in _validated_archive_records(frozen, plan, run_root)}
    rows = list(plan["runs"])
    random.Random(seed).shuffle(rows)
    packets, mapping = [], {}
    tasks = {t["id"]: t for t in frozen["suite"]["tasks"]}
    for row in rows:
        if row["run_id"] not in checked:
            continue
        record = checked[row["run_id"]]
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
    p.add_argument("--task-id", "--task-ids", dest="task_ids", nargs="+", action="extend")
    p.add_argument("--group", "--groups", dest="groups", nargs="+", action="extend", choices=(*GROUPS, *ABLATIONS))
    p = commands.add_parser("import-run")
    for name in ("frozen", "plan", "output", "row", "outcome", "trace"):
        p.add_argument(name)
    for name in ("runtime", "ledger", "result", "events"):
        p.add_argument("--" + name, required=True)
    p = commands.add_parser("import-runs", help="Audit every planned Harness result and retain missing/invalid entries.")
    for name in ("frozen", "plan", "runtime", "runner_directory", "ledger", "output"):
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
                                  inventory_base=Path(args.inventory).resolve().parent, repeats=args.repeats,
                                  task_ids=args.task_ids, groups=args.groups)
        result = {"status": "development_exported_not_executed", "rows": len(pack["rows"])}
    elif args.command == "import-run":
        result = import_run(load(args.frozen), load(args.plan), args.output, load(args.row), load(args.outcome), args.trace,
                            runtime_path=args.runtime, ledger_path=args.ledger, result_path=args.result, events_path=args.events)
    elif args.command == "import-runs":
        index = import_runs(load(args.frozen), load(args.plan), args.runtime, args.runner_directory, args.ledger, args.output)
        result = {k: index[k] for k in ("status", "planned_runs", "imported_runs", "unexpected_source_directories")}
    elif args.command == "blind":
        result = create_blind_review(load(args.frozen), load(args.plan), args.runs, args.output)
    else:
        grades = collect_grades(load(args.mapping), [load(p) for p in sorted(Path(args.reviewer_directory).glob("B*.json"))])
        save_new(args.output, grades)
        result = {"grades": len(grades), "status": "collected_operator_supplied_grades"}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
