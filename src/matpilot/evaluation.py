"""Offline study registry: frozen cases, fair schedules, immutable traces and paired analysis.

No model calls or scientific commands are made by this module. A separately
validated Harness driver supplies traces; human review supplies correctness.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics

CATEGORIES = ("control_semantics", "units_language", "missing_information", "condition_conflicts", "candidate_evidence", "partial_failure")
GROUPS = ("U", "U+", "M")
RUBRIC = ("requirements_correct", "actions_correct", "candidate_identity_correct", "conclusions_supported")
FLAGS = ("executable_completed", "false_refusal", "requirement_omission", "wrong_start", "unsupported_conclusion")
ABLATIONS = {"M_without_mapping": ("without_mapping", False, True),
             "M_without_evidence": ("without_evidence", True, False)}
SHARED = ("host_version", "llm_provider", "llm_version", "reasoning_mode", "scientific_capability_hash",
          "replay_fixture_hash", "clarification_policy_hash", "max_turns", "max_output_tokens", "sampling_cap",
          "max_requests", "timeout_ms", "guidance_scale")
COSTS = ("human_active_seconds", "api_cny", "science_cny", "science_seconds", "wall_seconds")
ROW_FIELDS = {"run_id", "task_id", "group", "repeat", "mode", "configuration_hash"}


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,255}", value) is not None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


def audit_suite(suite, formal=False):
    issues = []
    tasks = suite.get("tasks", []) if isinstance(suite, dict) else []
    if not isinstance(tasks, list) or not all(isinstance(t, dict) for t in tasks):
        tasks = []
        issues.append("Tasks must be a list of task objects.")
    if not tasks:
        issues.append("Task set is empty.")
    if len({str(t.get("id")) for t in tasks}) != len(tasks):
        issues.append("Duplicate task IDs.")
    for t in tasks:
        required = {"id", "category", "prompt", "initially_executable", "cross_model", "fixed_clarifications", "gold", "fixture_id"}
        if not required <= set(t) or t.get("category") not in CATEGORIES:
            issues.append("Incomplete or invalid task: " + str(t.get("id")))
            continue
        if not _identifier(t["id"]) or not _identifier(t["fixture_id"]) or not isinstance(t["prompt"], str) or not t["prompt"].strip():
            issues.append("Invalid task identity/prompt: " + str(t["id"]))
        if type(t["initially_executable"]) is not bool or type(t["cross_model"]) is not bool or not isinstance(t["fixed_clarifications"], list):
            issues.append("Invalid task flags/replies: " + str(t["id"]))
        elif any(not isinstance(reply, dict) or not all(isinstance(reply.get(k), str) and reply[k].strip()
                 for k in ("when", "reply")) for reply in t["fixed_clarifications"]):
            issues.append("Invalid fixed clarification: " + str(t["id"]))
        gold = t["gold"]
        if not isinstance(gold, dict) or not all(isinstance(gold.get(k), list) and gold[k] and
                all(isinstance(v, str) and v.strip() for v in gold[k])
                for k in ("required_facts", "acceptable_actions", "forbidden_claims")):
            issues.append("Incomplete gold annotation: " + str(t["id"]))
    counts = Counter(str(t.get("category")) for t in tasks)
    cross = sum(t.get("cross_model") is True for t in tasks)
    executable = sum(t.get("initially_executable") is True for t in tasks)
    if formal:
        if len(tasks) != 36 or any(counts[c] != 6 for c in CATEGORIES):
            issues.append("Formal suite needs 36 tasks, six per category.")
        if cross < 8 or executable < 18:
            issues.append("Formal suite needs at least 8 cross-model and 18 initially executable tasks.")
    return {"valid": not issues, "issues": issues, "count": len(tasks), "category_counts": dict(counts),
            "cross_model_count": cross, "initially_executable_count": executable,
            "independent_semantic_review": suite.get("independent_review", "pending") if isinstance(suite, dict) else "pending",
            "content_hash": digest(suite)}


def validate_frozen(frozen):
    """Validate the reviewed content and attestation, without certifying a human identity."""
    if not isinstance(frozen, dict) or type(frozen.get("schema_version")) is not int or frozen["schema_version"] != 1 or not isinstance(frozen.get("suite"), dict):
        raise ValueError("Expected a reviewed frozen task set, including schema_version and review.")
    audit = audit_suite(frozen["suite"], formal=True)
    if not audit["valid"]:
        raise ValueError("; ".join(audit["issues"]))
    if frozen.get("suite_sha256") != audit["content_hash"]:
        raise ValueError("Frozen task set changed.")
    review = frozen.get("review")
    if not isinstance(review, dict):
        raise ValueError("Independent reviewer approval is missing.")
    if review.get("suite_sha256") != audit["content_hash"] or review.get("approved") is not True or review.get("independent_of_implementation") is not True:
        raise ValueError("Independent reviewer approval must bind to this exact task set.")
    if not all(isinstance(review.get(k), str) and review[k].strip() for k in ("reviewer", "materials_qualification", "reviewed_at", "notes")):
        raise ValueError("Record an actual materials reviewer, qualifications, review time and notes.")
    return {"valid": True, "suite_sha256": audit["content_hash"], "tasks": audit["count"],
            "review_attestation": "operator-supplied; software does not certify reviewer identity"}


def freeze_suite(suite, review, output):
    audit = audit_suite(suite, formal=True)
    frozen = {"schema_version": 1, "suite": suite, "suite_sha256": audit["content_hash"], "review": review,
              "review_attestation": "operator-supplied; software does not certify reviewer identity",
              "rule": "Any content change requires a new freeze and an explicit protocol amendment."}
    validate_frozen(frozen)
    save_new(output, frozen)
    return frozen


def configuration_identity(config):
    """Identity of the configuration being validated, excluding its validation receipt."""
    if not isinstance(config, dict):
        raise ValueError("Driver configuration must be an object.")
    return digest({k: v for k, v in config.items() if k not in {"driver_validated", "driver_validation"}})


def _validate_configuration(config, group):
    if not isinstance(config, dict) or not all(k in config and config[k] is not None for k in SHARED):
        raise ValueError("Missing shared study conditions for " + group)
    for key in ("host_version", "llm_provider", "llm_version", "reasoning_mode", "source_revision"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError("Pin the study condition " + key + " for " + group)
    for key in ("scientific_capability_hash", "replay_fixture_hash", "clarification_policy_hash", "instruction_hash", "driver_sha256"):
        if not _sha(config.get(key)):
            raise ValueError("Expected a SHA256 identity for " + group + ": " + key)
    for key in ("max_turns", "max_output_tokens", "sampling_cap", "max_requests", "timeout_ms"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError("Study budgets must be positive integers: " + key)
    if type(config["guidance_scale"]) not in (int, float) or not math.isfinite(config["guidance_scale"]) or config["guidance_scale"] < 0:
        raise ValueError("Guidance scale must be finite and nonnegative.")
    if group != "U" and not _sha(config.get("baseline_patch_hash")):
        raise ValueError("Publish and identify the U+ fixes before running the comparison.")
    flags = (config.get("requirement_mapping"), config.get("evidence_checks"))
    expected = ABLATIONS[group][1:] if group in ABLATIONS else ((True, True) if group == "M" else (False, False))
    if any(type(flag) is not bool for flag in flags) or flags != expected:
        raise ValueError("Driver feature switches do not implement group " + group)
    expected_ablation = ABLATIONS[group][0] if group in ABLATIONS else None
    if config.get("ablation") != expected_ablation:
        raise ValueError("Ablation identity does not implement group " + group)
    receipt = config.get("driver_validation")
    if config.get("driver_validated") is not True or not isinstance(receipt, dict) or not _sha(receipt.get("report_sha256")) or receipt.get("configuration_sha256") != configuration_identity(config):
        raise ValueError("Each exact driver configuration needs its own bound validation receipt: " + group)


def _validate_environment(environment, ablation_environment, ablation_ids):
    if not isinstance(environment, dict) or set(environment) != set(GROUPS):
        raise ValueError("Provide all U/U+/M configurations.")
    for group in GROUPS:
        _validate_configuration(environment[group], group)
    for key in SHARED:
        if len({digest(environment[g][key]) for g in GROUPS}) != 1:
            raise ValueError("Unfair comparison: differing " + key)
    if environment["U+"]["baseline_patch_hash"] != environment["M"]["baseline_patch_hash"]:
        raise ValueError("M and U+ must share the same local baseline fixes.")
    if not isinstance(ablation_environment, dict) or set(ablation_environment) != (set(ABLATIONS) if ablation_ids else set()):
        raise ValueError("Predeclared ablations need both separately validated driver configurations.")
    for group, config in ablation_environment.items():
        _validate_configuration(config, group)
        for key in (*SHARED, "source_revision", "baseline_patch_hash"):
            if config[key] != environment["M"][key]:
                raise ValueError("Ablation changed a non-ablated study condition: " + key)


def schedule(frozen, environment, seed=20261022, ablation_ids=None, *, ablation_environment=None):
    validate_frozen(frozen)
    suite = frozen["suite"]
    if type(seed) is not int:
        raise ValueError("Randomization seed must be an integer.")
    ablation_ids = [] if ablation_ids is None else ablation_ids
    if not isinstance(ablation_ids, list) or not all(_identifier(tid) for tid in ablation_ids) or len(set(ablation_ids)) != len(ablation_ids) or not set(ablation_ids) <= {t["id"] for t in suite["tasks"]}:
        raise ValueError("Unknown or duplicate predeclared ablation task.")
    ablation_environment = {} if ablation_environment is None else ablation_environment
    _validate_environment(environment, ablation_environment, ablation_ids)
    randomizer = random.Random(seed)
    rows = []
    tasks = list(suite["tasks"])
    randomizer.shuffle(tasks)
    for t in tasks:
        for repeat in range(3):
            order = list(GROUPS)
            randomizer.shuffle(order)
            for group in order:
                rows.append({"run_id": f"{t['id']}--{group.replace('+', 'plus')}--{repeat+1}",
                    "task_id": t["id"], "group": group, "repeat": repeat+1, "mode": "reviewed_replay",
                    "configuration_hash": digest(environment[group])})
    for tid in ablation_ids:
        for group in ABLATIONS:
            for repeat in range(3):
                rows.append({"run_id": f"{tid}--{group}--{repeat+1}", "task_id": tid, "group": group,
                             "repeat": repeat+1, "mode": "reviewed_replay", "configuration_hash": digest(ablation_environment[group])})
    plan = {"schema_version": 2, "suite_sha256": frozen["suite_sha256"], "frozen_sha256": digest(frozen), "environment": environment,
            "ablation_environment": ablation_environment, "ablation_ids": ablation_ids,
            "random_seed": seed, "main_runs": 324, "runs": rows,
            "status": "scheduled_not_executed", "ablations_require_separately_validated_drivers": True}
    plan["plan_sha256"] = digest(plan)
    return json.loads(json.dumps(plan))  # Caller mutation must not silently alter the frozen schedule.


def validate_plan(frozen, plan):
    """Reject altered identity, order, configuration, or any missing/extra repetition."""
    validate_frozen(frozen)
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] != 2:
        raise ValueError("Expected a schema_version=2 study plan; regenerate legacy schedules.")
    if plan.get("plan_sha256") != digest({k: v for k, v in plan.items() if k != "plan_sha256"}):
        raise ValueError("Study plan hash changed.")
    required = {"environment", "random_seed", "ablation_ids", "ablation_environment"}
    if not required <= set(plan):
        raise ValueError("Study plan structure is incomplete.")
    if type(plan.get("main_runs")) is not int or plan["main_runs"] != 324 or not isinstance(plan.get("runs"), list):
        raise ValueError("Study plan needs exactly 324 main runs.")
    for row in plan["runs"]:
        _validate_row(row)
    expected = schedule(frozen, plan["environment"], plan["random_seed"], plan["ablation_ids"],
                        ablation_environment=plan["ablation_environment"])
    if plan != expected:
        raise ValueError("Study plan identity/order/coverage changed: require every task, U/U+/M, exactly three repeats and all predeclared ablations.")
    return {"valid": True, "plan_sha256": plan["plan_sha256"], "main_runs": 324,
            "ablation_runs": len(plan["runs"]) - 324, "planned_runs": len(plan["runs"])}


def _validate_row(row):
    if not isinstance(row, dict) or set(row) != ROW_FIELDS or not _identifier(row.get("run_id")) or not _identifier(row.get("task_id")) or row.get("group") not in (*GROUPS, *ABLATIONS):
        raise ValueError("Invalid run identity or row structure.")
    expected_id = f"{row['task_id']}--{row['group'].replace('+', 'plus')}--{row['repeat']}"
    if type(row["repeat"]) is not int or row["repeat"] not in (1, 2, 3) or row["run_id"] != expected_id or row["mode"] != "reviewed_replay" or not _sha(row["configuration_hash"]):
        raise ValueError("Invalid task/group/repeat/configuration identity.")


def _validate_outcome(row, outcome):
    required = {"status", "wall_seconds", "human_active_seconds", "api_cny", "science_cny", "science_seconds", "failure", "configuration_hash"}
    if not isinstance(outcome, dict) or set(outcome) != required or outcome["status"] not in {"completed", "failed", "timeout", "budget_stopped"}:
        raise ValueError("Record the complete outcome, including failures; never replace failed attempts.")
    if outcome["configuration_hash"] != row["configuration_hash"]:
        raise ValueError("Runtime configuration differs from the scheduled group.")
    for k in ("wall_seconds", "human_active_seconds", "api_cny", "science_cny", "science_seconds"):
        v = outcome[k]
        if v is not None and (type(v) not in (float, int) or v < 0 or not math.isfinite(v)):
            raise ValueError("Time/cost must be a finite nonnegative measurement or null.")
    if outcome["status"] != "completed" and (not isinstance(outcome["failure"], str) or not outcome["failure"].strip()):
        raise ValueError("Failed runs require a reason.")
    if outcome["status"] == "completed" and outcome["failure"] is not None:
        raise ValueError("Completed runs must not have a terminal failure reason.")


def record_run(root, row, outcome, trace, *, frozen=None, plan=None, import_audit=None):
    """Append a run. Pass both frozen/plan for records usable in formal summaries."""
    _validate_row(row)
    _validate_outcome(row, outcome)
    if (frozen is None) != (plan is None):
        raise ValueError("Provide frozen and plan together when binding a run.")
    if plan is not None:
        validate_plan(frozen, plan)
        if row not in plan["runs"]:
            raise ValueError("Run identity is not in the frozen study plan.")
    trace = Path(trace)
    trace_bytes = trace.read_bytes()
    if import_audit is not None and (not isinstance(import_audit, dict)
            or import_audit.get("source_hashes", {}).get("trace.json") != hashlib.sha256(trace_bytes).hexdigest()):
        raise ValueError("Trace changed after its import audit.")
    if not isinstance(json.loads(trace_bytes), (dict, list)):
        raise ValueError("A trace must be a JSON object or list.")
    run_id = row["run_id"]
    if Path(run_id).name != run_id or any(x in run_id for x in ("/", "\\")):
        raise ValueError("Invalid run ID.")
    folder = Path(root) / run_id
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "trace.json").write_bytes(trace_bytes)
    record = {**row, "outcome": outcome, "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
              "plan_sha256": plan["plan_sha256"] if plan else None,
              "correctness": "ungraded", "human_time_status": "measured" if outcome["human_active_seconds"] is not None else "not_measured"}
    if import_audit is not None:
        record["import_audit_sha256"] = digest(import_audit)
        save_new(folder / "audit.json", import_audit)
    record["record_sha256"] = digest(record)
    save_new(folder / "run.json", record)
    return record


def blind_packet(root, run_id, blind_id):
    if not _identifier(run_id) or not _identifier(blind_id):
        raise ValueError("Invalid run/blind identifier.")
    folder = Path(root) / run_id
    record = load(folder / "run.json")
    trace = (folder / "trace.json").read_bytes()
    if hashlib.sha256(trace).hexdigest() != record["trace_sha256"]:
        raise ValueError("Trace changed after recording.")
    _validate_record_integrity(record)
    # Preserve the original for audit; redact explicit labels in the reviewer copy.
    labels = {"group", "run_id", "configuration_hash", "configuration_sha256", "configuration", "environment",
              "plan_sha256", "record_sha256", "driver_validation", "source_revision", "ablation"}
    def redact(value):
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items() if k not in labels}
        if isinstance(value, list):
            return [redact(v) for v in value]
        return value
    blinded_trace = redact(json.loads(trace))
    return {"blind_id": blind_id, "task_id": record["task_id"], "trace": blinded_trace, "trace_sha256": record["trace_sha256"],
            "review_copy_sha256": digest(blinded_trace),
            "rubric": {k: None for k in (*RUBRIC, *FLAGS)}, "evidence_notes": None,
            "blinding_limit": "Explicit identity keys removed recursively; free text and tool behavior may still reveal the system. Inspect reviewer copies and report partial blinding honestly."}


def validate_grade(grade):
    if not isinstance(grade, dict) or not all(type(grade.get(k)) is bool for k in (*RUBRIC, *FLAGS)):
        raise ValueError("Human grade must explicitly score every criterion/flag.")
    if not all(isinstance(grade.get(k), str) and grade[k].strip() for k in ("reviewer", "evidence_notes")):
        raise ValueError("Record reviewer and evidence supporting the grade.")
    for flag, criterion in (("false_refusal", "actions_correct"), ("requirement_omission", "requirements_correct"),
                            ("wrong_start", "actions_correct"), ("unsupported_conclusion", "conclusions_supported")):
        if grade[flag] and grade[criterion]:
            raise ValueError("Contradictory grade: " + flag + " and " + criterion)
    return all(grade[k] for k in RUBRIC)


def _validate_record_integrity(record):
    if not isinstance(record, dict) or not ROW_FIELDS <= set(record):
        raise ValueError("Incomplete run record.")
    _validate_row({k: record[k] for k in ROW_FIELDS})
    _validate_outcome(record, record.get("outcome"))
    if not _sha(record.get("trace_sha256")) or record.get("record_sha256") != digest({k: v for k, v in record.items() if k != "record_sha256"}):
        raise ValueError("Run record/trace identity hash changed or is missing.")
    expected = "measured" if record["outcome"]["human_active_seconds"] is not None else "not_measured"
    if record.get("human_time_status") != expected or record.get("correctness") != "ungraded":
        raise ValueError("Run measurement/scoring metadata is inconsistent.")


def _paired(values, bootstrap_samples, label):
    if not values:
        return None
    rng = random.Random(20261022)
    boot = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(bootstrap_samples))
    return {"unit": "task_cluster", "tasks": len(values), label: statistics.mean(values),
            "percentile_95_interval": [boot[int(.025 * len(boot))], boot[min(len(boot)-1, int(.975 * len(boot)))]],
            "bootstrap_samples": bootstrap_samples}


def summarize(frozen, plan, records, grades, bootstrap_samples=2000):
    validation = validate_plan(frozen, plan)
    if type(bootstrap_samples) is not int or bootstrap_samples < 1:
        raise ValueError("Bootstrap samples must be a positive integer.")
    if not isinstance(records, list) or not isinstance(grades, dict):
        raise ValueError("Provide a list of records and a run-ID keyed grade dictionary.")
    tasks = {t["id"]: t for t in frozen["suite"]["tasks"]}
    for record in records:
        _validate_record_integrity(record)
    by_id = {r["run_id"]: r for r in records}
    expected_ids = {r["run_id"] for r in plan["runs"]}
    if len(by_id) != len(records) or not set(by_id) <= expected_ids or not set(grades) <= set(by_id):
        raise ValueError("Duplicate, unexpected or orphaned run/grade.")
    by_group = defaultdict(list)
    per_task = defaultdict(lambda: defaultdict(list))
    per_task_completion = defaultdict(lambda: defaultdict(list))
    missing, ungraded = [], []
    for row in plan["runs"]:
        rid, group, tid = row["run_id"], row["group"], row["task_id"]
        record, grade = by_id.get(rid), grades.get(rid)
        if record is None:
            missing.append(rid)
        elif any(record.get(k) != row[k] for k in ROW_FIELDS) or record.get("plan_sha256") != plan["plan_sha256"]:
            raise ValueError("Recorded task/group/repeat/configuration/plan identity changed.")
        if record is not None and grade is None:
            ungraded.append(rid)
        correct = validate_grade(grade) if grade is not None else False
        if grade is not None and grade.get("trace_sha256") != record.get("trace_sha256"):
            raise ValueError("Grade is not bound to the recorded trace.")
        if grade is not None and "run_id" in grade and grade["run_id"] != rid:
            raise ValueError("Grade is bound to another run identity.")
        if grade is not None and grade["executable_completed"] and record["outcome"]["status"] != "completed":
            raise ValueError("A failed run cannot be graded as executable completed.")
        if grade is not None and grade["false_refusal"] and not tasks[tid]["initially_executable"]:
            raise ValueError("False refusal must refer to an initially executable task.")
        if record is None or record["outcome"]["status"] != "completed":
            correct = False
        values = {"correct": int(correct), "initially_executable": tasks[tid]["initially_executable"], "grade": grade,
                  "recorded": record is not None, "costs": record["outcome"] if record else {}}
        by_group[group].append(values)
        per_task[group][tid].append(int(correct))
        per_task_completion[group][tid].append(int(bool(grade and grade["executable_completed"])))
    complete = not missing and not ungraded
    summaries = {}
    for group, values in by_group.items():
        executable = [v for v in values if v["initially_executable"]]
        graded = sum(v["grade"] is not None for v in values)
        group_complete = graded == len(values) and all(v["recorded"] for v in values)
        correct_count = sum(v["correct"] for v in values)
        scores = {"planned_runs": len(values), "recorded_runs": sum(v["recorded"] for v in values), "graded_runs": graded,
                  "correct_runs": correct_count, "initially_executable_runs": len(executable),
                  "correct_rate_lower_bound": correct_count / len(values),
                  "complete_task_correct_rate": correct_count / len(values) if group_complete else None,
                  "executable_completion_rate": sum(bool(v["grade"] and v["grade"]["executable_completed"]) for v in executable) / len(executable) if group_complete and executable else None}
        for flag in FLAGS[1:]:
            population = executable if flag == "false_refusal" else values
            scores[flag + "_observed_count"] = sum(bool(v["grade"] and v["grade"][flag]) for v in population)
            scores[flag + "_ungraded_count"] = sum(v["grade"] is None for v in population)
            scores[flag + "_rate"] = scores[flag + "_observed_count"] / len(population) if group_complete and population else None
        for key in COSTS:
            measured = [v["costs"].get(key) for v in values]
            scores[key + "_measured_count"] = sum(x is not None for x in measured)
            total = sum(x for x in measured if x is not None)
            scores[key + "_observed_total"] = total
            scores[key + "_per_correct_task"] = total / correct_count if group_complete and correct_count and all(x is not None for x in measured) else None
        summaries[group] = scores
    # Cluster at task level: three repeats are NOT treated as independent tasks.
    paired = paired_completion = None
    paired_ablations = {}
    if complete:
        differences = [statistics.mean(per_task["M"][tid]) - statistics.mean(per_task["U+"][tid]) for tid in tasks]
        paired = _paired(differences, bootstrap_samples, "difference_M_minus_Uplus")
        completion_differences = [statistics.mean(per_task_completion["M"][tid]) - statistics.mean(per_task_completion["U+"][tid])
                                  for tid in tasks if tasks[tid]["initially_executable"]]
        paired_completion = _paired(completion_differences, bootstrap_samples, "difference_M_minus_Uplus")
        for group in plan["ablation_environment"]:
            differences = [statistics.mean(per_task["M"][tid]) - statistics.mean(per_task[group][tid]) for tid in plan["ablation_ids"]]
            paired_ablations[group] = _paired(differences, bootstrap_samples, "difference_M_minus_ablation")
    efficiency = {}
    for key in COSTS:
        m_cost = summaries["M"][key + "_per_correct_task"]
        u_cost = summaries["U+"][key + "_per_correct_task"]
        efficiency[key + "_per_correct_task_reduction_M_vs_Uplus"] = 1 - m_cost / u_cost if complete and m_cost is not None and u_cost is not None and u_cost > 0 else None
    return {"status": "complete_scored" if complete else "incomplete_do_not_interpret_effectiveness",
            "plan_validation": validation,
            "missing_runs": missing, "ungraded_runs": ungraded, "groups": summaries,
            "paired_correctness": paired, "paired_executable_completion": paired_completion,
            "paired_ablations": paired_ablations, "efficiency": efficiency,
            "limitations": ["Missing/failed runs remain in denominators; ungraded is not a scientific negative result.",
                            "No human-time savings claim without measured human active time.",
                            "Configuration/review receipts validate identity, not independent reviewer identity or actual driver execution.",
                            "Trace bytes must be checked by the importing workspace; this in-memory summary validates their recorded identities.",
                            "Intervals quantify task-set variability, not competition award probability.",
                            "Replay experiments and real model acceptance are different evidence."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("audit"); p.add_argument("suite"); p.add_argument("--formal", action="store_true")
    p = sub.add_parser("freeze"); p.add_argument("suite"); p.add_argument("review"); p.add_argument("output")
    p = sub.add_parser("schedule"); p.add_argument("frozen"); p.add_argument("environment"); p.add_argument("output")
    p = sub.add_parser("summarize", help="Audit archived files before summarizing operator-supplied grades.")
    p.add_argument("frozen"); p.add_argument("plan"); p.add_argument("runs"); p.add_argument("grades"); p.add_argument("output")
    p.add_argument("--bootstrap-samples", type=int, default=2000)
    args = parser.parse_args()
    if args.command == "audit":
        print(json.dumps(audit_suite(load(args.suite), args.formal), ensure_ascii=False, indent=2))
    elif args.command == "freeze":
        freeze_suite(load(args.suite), load(args.review), args.output)
    elif args.command == "schedule":
        save_new(args.output, schedule(load(args.frozen), load(args.environment)))
    else:
        from .study_workspace import summarize_archive
        result = summarize_archive(load(args.frozen), load(args.plan), args.runs, load(args.grades),
                                   bootstrap_samples=args.bootstrap_samples)
        save_new(args.output, result)
        print(json.dumps({"status": result["status"], "missing_runs": len(result["missing_runs"]),
                          "ungraded_runs": len(result["ungraded_runs"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
