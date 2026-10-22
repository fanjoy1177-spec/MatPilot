"""Offline study registry: frozen cases, fair schedules, immutable traces and paired analysis.

No model calls or scientific commands are made by this module. A separately
validated Harness driver supplies traces; human review supplies correctness.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import statistics

CATEGORIES = ("control_semantics", "units_language", "missing_information", "condition_conflicts", "candidate_evidence", "partial_failure")
GROUPS = ("U", "U+", "M")
RUBRIC = ("requirements_correct", "actions_correct", "candidate_identity_correct", "conclusions_supported")
FLAGS = ("executable_completed", "false_refusal", "requirement_omission", "wrong_start", "unsupported_conclusion")


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
    tasks = suite.get("tasks", [])
    if len({t.get("id") for t in tasks}) != len(tasks):
        issues.append("Duplicate task IDs.")
    for t in tasks:
        required = {"id", "category", "prompt", "initially_executable", "cross_model", "fixed_clarifications", "gold", "fixture_id"}
        if not required <= set(t) or t.get("category") not in CATEGORIES:
            issues.append("Incomplete or invalid task: " + str(t.get("id")))
            continue
        if type(t["initially_executable"]) is not bool or type(t["cross_model"]) is not bool or not isinstance(t["fixed_clarifications"], list):
            issues.append("Invalid task flags/replies: " + t["id"])
        gold = t["gold"]
        if not isinstance(gold, dict) or not all(gold.get(k) for k in ("required_facts", "acceptable_actions", "forbidden_claims")):
            issues.append("Incomplete gold annotation: " + t["id"])
    counts = Counter(t.get("category") for t in tasks)
    cross = sum(t.get("cross_model") is True for t in tasks)
    executable = sum(t.get("initially_executable") is True for t in tasks)
    if formal:
        if len(tasks) != 36 or any(counts[c] != 6 for c in CATEGORIES):
            issues.append("Formal suite needs 36 tasks, six per category.")
        if cross < 8 or executable < 18:
            issues.append("Formal suite needs at least 8 cross-model and 18 initially executable tasks.")
    return {"valid": not issues, "issues": issues, "count": len(tasks), "category_counts": dict(counts),
            "cross_model_count": cross, "initially_executable_count": executable,
            "independent_semantic_review": suite.get("independent_review", "pending"),
            "content_hash": digest(suite)}


def freeze_suite(suite, review, output):
    audit = audit_suite(suite, formal=True)
    if not audit["valid"]:
        raise ValueError("; ".join(audit["issues"]))
    if review.get("suite_sha256") != audit["content_hash"] or review.get("approved") is not True or review.get("independent_of_implementation") is not True:
        raise ValueError("Independent reviewer approval must bind to this exact task set.")
    if not all(isinstance(review.get(k), str) and review[k].strip() for k in ("reviewer", "materials_qualification", "reviewed_at", "notes")):
        raise ValueError("Record an actual materials reviewer, qualifications, review time and notes.")
    frozen = {"schema_version": 1, "suite": suite, "suite_sha256": audit["content_hash"], "review": review,
              "review_attestation": "operator-supplied; software does not certify reviewer identity",
              "rule": "Any content change requires a new freeze and an explicit protocol amendment."}
    save_new(output, frozen)
    return frozen


def schedule(frozen, environment, seed=20261022, ablation_ids=None):
    suite = frozen["suite"]
    if digest(suite) != frozen["suite_sha256"]:
        raise ValueError("Frozen task set changed.")
    if not audit_suite(suite, formal=True)["valid"]:
        raise ValueError("Invalid formal task set.")
    shared = ("host_version", "llm_provider", "llm_version", "reasoning_mode", "scientific_capability_hash", "replay_fixture_hash", "clarification_policy_hash", "max_turns", "max_output_tokens", "sampling_cap")
    if set(environment) != set(GROUPS):
        raise ValueError("Provide all U/U+/M configurations.")
    for group in GROUPS:
        config = environment[group]
        if not all(k in config and config[k] is not None for k in shared):
            raise ValueError("Missing shared study conditions for " + group)
        if config.get("driver_validated") is not True or not config.get("source_revision") or not config.get("instruction_hash"):
            raise ValueError("Each group needs a validated driver, pinned source and normal instructions.")
        if group in {"U+", "M"} and not config.get("baseline_patch_hash"):
            raise ValueError("Publish and identify the U+ fixes before running the comparison.")
    for key in shared:
        if len({digest(environment[g][key]) for g in GROUPS}) != 1:
            raise ValueError("Unfair comparison: differing " + key)
    if environment["U+"]["baseline_patch_hash"] != environment["M"]["baseline_patch_hash"]:
        raise ValueError("M and U+ must share the same local baseline fixes.")
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
    ablation_ids = ablation_ids or []
    if not set(ablation_ids) <= {t["id"] for t in tasks}:
        raise ValueError("Unknown ablation task.")
    for tid in ablation_ids:
        for group in ("M_without_mapping", "M_without_evidence"):
            for repeat in range(3):
                rows.append({"run_id": f"{tid}--{group}--{repeat+1}", "task_id": tid, "group": group,
                             "repeat": repeat+1, "mode": "reviewed_replay", "configuration_hash": digest(environment["M"])})
    return {"schema_version": 1, "suite_sha256": frozen["suite_sha256"], "environment": environment,
            "random_seed": seed, "main_runs": 324, "runs": rows,
            "status": "scheduled_not_executed", "ablations_require_separately_validated_drivers": True}


def record_run(root, row, outcome, trace):
    required = {"status", "wall_seconds", "human_active_seconds", "api_cny", "science_cny", "science_seconds", "failure", "configuration_hash"}
    if not isinstance(outcome, dict) or set(outcome) != required or outcome["status"] not in {"completed", "failed", "timeout", "budget_stopped"}:
        raise ValueError("Record the complete outcome, including failures; never replace failed attempts.")
    if outcome["configuration_hash"] != row["configuration_hash"]:
        raise ValueError("Runtime configuration differs from the scheduled group.")
    for k in ("wall_seconds", "human_active_seconds", "api_cny", "science_cny", "science_seconds"):
        v = outcome[k]
        if v is not None and (type(v) not in (float, int) or v < 0 or v != v or v == float("inf")):
            raise ValueError("Time/cost must be a finite nonnegative measurement or null.")
    if outcome["status"] != "completed" and not outcome["failure"]:
        raise ValueError("Failed runs require a reason.")
    trace = Path(trace)
    trace_bytes = trace.read_bytes()
    run_id = row["run_id"]
    if Path(run_id).name != run_id or any(x in run_id for x in ("/", "\\")):
        raise ValueError("Invalid run ID.")
    folder = Path(root) / run_id
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "trace.json").write_bytes(trace_bytes)
    record = {**row, "outcome": outcome, "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
              "correctness": "ungraded", "human_time_status": "measured" if outcome["human_active_seconds"] is not None else "not_measured"}
    save_new(folder / "run.json", record)
    return record


def blind_packet(root, run_id, blind_id):
    folder = Path(root) / run_id
    record = load(folder / "run.json")
    trace = (folder / "trace.json").read_bytes()
    if hashlib.sha256(trace).hexdigest() != record["trace_sha256"]:
        raise ValueError("Trace changed after recording.")
    return {"blind_id": blind_id, "task_id": record["task_id"], "trace": json.loads(trace), "trace_sha256": record["trace_sha256"],
            "rubric": {k: None for k in (*RUBRIC, *FLAGS)}, "evidence_notes": None,
            "blinding_limit": "Group/config identifiers omitted; tool behavior may still reveal the system. Report partial blinding honestly."}


def validate_grade(grade):
    if not all(type(grade.get(k)) is bool for k in (*RUBRIC, *FLAGS)):
        raise ValueError("Human grade must explicitly score every criterion/flag.")
    if not all(isinstance(grade.get(k), str) and grade[k].strip() for k in ("reviewer", "evidence_notes")):
        raise ValueError("Record reviewer and evidence supporting the grade.")
    return all(grade[k] for k in RUBRIC)


def summarize(frozen, plan, records, grades, bootstrap_samples=2000):
    if digest(frozen["suite"]) != frozen["suite_sha256"] or plan["suite_sha256"] != frozen["suite_sha256"]:
        raise ValueError("Study/suite identity mismatch.")
    tasks = {t["id"]: t for t in frozen["suite"]["tasks"]}
    by_id = {r["run_id"]: r for r in records}
    expected_ids = {r["run_id"] for r in plan["runs"]}
    if len(by_id) != len(records) or not set(by_id) <= expected_ids or not set(grades) <= set(by_id):
        raise ValueError("Duplicate, unexpected or orphaned run/grade.")
    by_group = defaultdict(list)
    per_task = defaultdict(lambda: defaultdict(list))
    missing, ungraded = [], []
    for row in plan["runs"]:
        rid, group, tid = row["run_id"], row["group"], row["task_id"]
        record, grade = by_id.get(rid), grades.get(rid)
        if record is None:
            missing.append(rid)
        elif record.get("configuration_hash") != row["configuration_hash"]:
            raise ValueError("Recorded group configuration changed.")
        if record is not None and grade is None:
            ungraded.append(rid)
        if grade is not None and grade.get("trace_sha256") != record.get("trace_sha256"):
            raise ValueError("Grade is not bound to the recorded trace.")
        correct = validate_grade(grade) if grade is not None else False
        if record is None or record["outcome"]["status"] != "completed":
            correct = False
        values = {"correct": int(correct), "initially_executable": tasks[tid]["initially_executable"], "grade": grade,
                  "costs": record["outcome"] if record else {}}
        by_group[group].append(values)
        per_task[group][tid].append(int(correct))
    summaries = {}
    for group, values in by_group.items():
        executable = [v for v in values if v["initially_executable"]]
        scores = {"planned_runs": len(values), "complete_task_correct_rate": statistics.mean(v["correct"] for v in values),
                  "executable_completion_rate": sum(bool(v["grade"] and v["grade"]["executable_completed"]) for v in executable) / len(executable) if executable else None}
        for flag in FLAGS[1:]:
            population = executable if flag == "false_refusal" else values
            scores[flag + "_rate"] = sum(bool(v["grade"] and v["grade"][flag]) for v in population) / len(population) if population else None
        for key in ("human_active_seconds", "api_cny", "science_cny", "science_seconds", "wall_seconds"):
            measured = [v["costs"].get(key) for v in values]
            scores[key + "_measured_count"] = sum(x is not None for x in measured)
            total = sum(x for x in measured if x is not None)
            scores[key + "_observed_total"] = total
            correct_count = sum(v["correct"] for v in values)
            scores[key + "_per_correct_task"] = total / correct_count if correct_count and all(x is not None for x in measured) else None
        summaries[group] = scores
    # Cluster at task level: three repeats are NOT treated as independent tasks.
    differences = [statistics.mean(per_task["M"][tid]) - statistics.mean(per_task["U+"][tid]) for tid in tasks]
    rng = random.Random(20261022)
    boot = sorted(statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(bootstrap_samples))
    paired = {"unit": "task_cluster", "tasks": len(differences), "difference_M_minus_Uplus": statistics.mean(differences),
              "percentile_95_interval": [boot[int(.025 * len(boot))], boot[min(len(boot)-1, int(.975 * len(boot)))]],
              "bootstrap_samples": bootstrap_samples}
    complete = not missing and not ungraded
    return {"status": "complete_scored" if complete else "incomplete_do_not_interpret_effectiveness",
            "missing_runs": missing, "ungraded_runs": ungraded, "groups": summaries,
            "paired_correctness": paired if complete else None,
            "limitations": ["Missing/failed runs remain in denominators; ungraded is not a scientific negative result.",
                            "No human-time savings claim without measured human active time.",
                            "Intervals quantify task-set variability, not competition award probability.",
                            "Replay experiments and real model acceptance are different evidence."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("audit"); p.add_argument("suite"); p.add_argument("--formal", action="store_true")
    p = sub.add_parser("freeze"); p.add_argument("suite"); p.add_argument("review"); p.add_argument("output")
    p = sub.add_parser("schedule"); p.add_argument("frozen"); p.add_argument("environment"); p.add_argument("output")
    args = parser.parse_args()
    if args.command == "audit":
        print(json.dumps(audit_suite(load(args.suite), args.formal), ensure_ascii=False, indent=2))
    elif args.command == "freeze":
        freeze_suite(load(args.suite), load(args.review), args.output)
    else:
        save_new(args.output, schedule(load(args.frozen), load(args.environment)))


if __name__ == "__main__":
    main()
