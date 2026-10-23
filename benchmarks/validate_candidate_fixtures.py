"""CPU-only candidate-draft coverage audit; never an LLM score or formal approval."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from matpilot.adapters.mattergen import MatterGenAdapter
from matpilot.evaluation import audit_suite, digest, save_new
from matpilot.evidence import compare_metrics
from matpilot.study_replay import ReplaySession, _validate_manifest, verify_trace
from matpilot.structures import analyze_cif


def require(value, message):
    if not value:
        raise ValueError(message)


def validate_bindings(suite, manifest):
    require(suite.get("split") == "candidate_test_not_frozen", "Only the unreviewed candidate draft is accepted.")
    require(manifest.get("purpose") == "development" and manifest.get("review_status") == "pending_independent_materials_review", "Candidate fixtures must remain development and pending review.")
    _validate_manifest(manifest)
    tasks = suite["tasks"]
    require(len(tasks) == 36 and len({t["id"] for t in tasks}) == 36, "The draft must retain all 36 distinct tasks.")
    require(set(manifest.get("task_bindings", {})) == {t["id"] for t in tasks}, "Missing or unexpected task bindings.")
    content = [{k: v for k, v in t.items() if k != "fixture_id"} for t in tasks]
    require(digest(content) == manifest["source_task_content_sha256"], "Task prompts/gold/annotations changed after fixture construction.")
    require({t["fixture_id"] for t in tasks} == set(manifest["fixtures"]), "Unresolved or orphaned candidate fixtures.")
    for task in tasks:
        binding = manifest["task_bindings"][task["id"]]
        require(binding["fixture_id"] == task["fixture_id"], "Task/fixture association differs.")
        require(binding["task_content_sha256"] == digest({k: v for k, v in task.items() if k != "fixture_id"}), "A task binding changed.")
        fixture = manifest["fixtures"][task["fixture_id"]]
        require(binding["route_sha256"] == {k: digest(v) for k, v in fixture["routes"].items()}, "Scientific route differs from its audit binding.")
        require(fixture["provenance"]["kind"] == "synthetic", "Candidate draft must not claim real model provenance.")
    return tasks


def validate_revision(proposal_path, suite_path, manifest_path):
    """Verify a proposed v2 against retained parent bytes, not against scores."""
    path = Path(proposal_path).resolve(strict=True)
    proposal = json.loads(path.read_text(encoding="utf-8"))
    require(proposal.get("format") == "matpilot-candidate-revision-proposal/v1" and proposal.get("draft_version") == 2,
            "Unknown candidate revision proposal.")
    require(proposal.get("approved") is False and proposal.get("independent_review") == "pending"
            and proposal.get("user_review") == "pending", "This tool verifies pending proposals, not review approval.")
    values = {}
    for name in ("source_suite", "source_manifest", "proposed_suite", "proposed_manifest"):
        entry = proposal[name]
        source = (path.parent / entry["path"]).resolve(strict=True)
        require(source.is_relative_to(path.parent), "Revision evidence path escapes its directory.")
        require(hashlib.sha256(source.read_bytes()).hexdigest() == entry["sha256"], "Revision source/proposal bytes changed: " + name)
        values[name] = json.loads(source.read_text(encoding="utf-8"))
        if name in {"proposed_suite", "proposed_manifest"}:
            expected = Path(suite_path if name == "proposed_suite" else manifest_path).resolve(strict=True)
            require(source == expected, "Coverage input is not the revision's proposed file.")
    before, after = values["source_suite"], values["proposed_suite"]
    validate_bindings(before, values["source_manifest"])
    validate_bindings(after, values["proposed_manifest"])
    source_tasks, proposed_tasks = ({t["id"]: t for t in s["tasks"]} for s in (before, after))
    require(set(source_tasks) == set(proposed_tasks), "Revision changed the task population.")
    changes = proposal.get("changes", [])
    require(len(changes) == 3 and {c["task_id"] for c in changes} == {"T0205", "T0305", "T0601"}, "Expected exactly the three disclosed ambiguity revisions.")
    changed = {tid for tid in source_tasks if source_tasks[tid] != proposed_tasks[tid]}
    require(changed == {c["task_id"] for c in changes}, "Undisclosed task edits or unchanged declared revisions.")
    for change in changes:
        tid = change["task_id"]
        original, revised = source_tasks[tid], proposed_tasks[tid]
        require(change["before"] == original and change["after"] == revised, "Revision comparison differs from actual task text.")
        fields = sorted(k for k in original.keys() | revised.keys() if original.get(k) != revised.get(k))
        require(change["changed_fields"] == fields and set(fields) <= {"prompt", "gold", "fixed_clarifications", "fixture_id"}, "Revision changed undisclosed annotation fields.")
        require(all(original[k] == revised[k] for k in ("category", "cross_model", "initially_executable", "review_status")), "Revision changed quotas or approval metadata.")
        require(original["gold"]["forbidden_claims"] == revised["gold"]["forbidden_claims"], "Revision weakened scientific claim boundaries.")
        require(change.get("review_status") == "pending_user_and_material_review" and change.get("reason"), "Revision needs pending status and an explicit reason.")
    old_audit, new_audit = audit_suite(before, formal=True), audit_suite(after, formal=True)
    require(old_audit["valid"] and new_audit["valid"], "Candidate quota/schema validation failed.")
    for label, key in (("tasks", "count"), ("cross_model", "cross_model_count"), ("initially_executable", "initially_executable_count")):
        require(proposal["quota_impact"][label + "_before"] == old_audit[key] and proposal["quota_impact"][label + "_after"] == new_audit[key], "Declared quota impact differs from the actual task population.")
    require(old_audit["category_counts"] == new_audit["category_counts"] and set(new_audit["category_counts"].values()) == {6}, "Revision changed category quotas.")
    return {"proposal_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "changed_tasks": sorted(changed),
            "parent_files_verified": True, "approval": "pending_user_and_material_review", "quota_counts": {
                "tasks": new_audit["count"], "cross_model": new_audit["cross_model_count"], "initially_executable": new_audit["initially_executable_count"]}}


def probe_fixture(session, fixture, binding):
    from pymatgen.core import Composition, Structure
    from pymatgen.analysis.structure_matcher import StructureMatcher
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    probe, observations = binding["probe"], {}
    policy = probe.get("sampling_policy")
    if policy is not None:
        require(policy.get("must_exhaust_budget") is False and policy.get("max_total_attempts") == fixture["sampling_cap"],
                "An upper sampling budget must not be converted into a required exact count.")
        require(type(policy.get("max_batch_size")) is int and policy["max_batch_size"] > 0, "Invalid batch-size upper bound.")
        for batches in [probe.get("batches", []), *policy.get("other_legal_examples", [])]:
            require(isinstance(batches, list) and all(type(n) is int and 0 < n <= policy["max_batch_size"] for n in batches)
                    and sum(batches) <= policy["max_total_attempts"], "A declared partition exceeds its batch/total upper bound.")
        observations["sampling_policy"] = policy
        observations["fixed_cpu_probe_batches"] = probe.get("batches", [])
    for route in fixture["routes"].values():
        if route["engine"] == "mattergen":
            plan = MatterGenAdapter().validate({"checkpoint": route["model_name"], "conditions": route["conditions"],
                "unconditional": not route["conditions"], "num_samples": fixture["sampling_cap"]})
            require(plan["state"] == "ready", "Declared MatterGen route violates its actual adapter contract.")
        else:
            require(route["engine"] == "diffcsp" and route["model_name"] == "mp_csp", "Unknown route engine/model.")
            require(route["conditions"] == {"spacegroup": 221, "wyckoff_letters": ["a", "b"], "atom_types": ["Na", "Cl"]}, "DiffCSP route does not match the registered cubic_nacl context.")
    for batch in probe.get("batches", []):
        session.invoke("sample", {**fixture["routes"]["generate"], "num_samples": batch})
    status = session.invoke("status")
    observations.update(attempts_used=status["attempts_used"], remaining_budget=status["remaining_budget"])
    for key, actual in (("remaining", status["remaining_budget"]), ("attempts_used", status["attempts_used"]),
                        ("initial_outcomes", len(status.get("initial_outcomes", [])))):
        if key in probe:
            require(probe[key] == actual, "Initial execution/budget evidence differs: " + key)
    if "execution_state" in probe:
        execution = status["execution"]
        require(execution["state"] == probe["execution_state"], "Execution state differs.")
        observations["execution"] = {k: execution[k] for k in ("state", "heartbeat_late", "termination_confirmed", "resume_allowed")}
        for key in ("heartbeat_late", "resume_allowed"):
            if key in probe:
                require(execution[key] == probe[key], "Execution observation differs: " + key)
    if probe.get("artifact_changed"):
        evidence = session.invoke("verify_candidate_artifact", {"candidate_id": "c_a"})
        require(evidence["intact"] is False and evidence["expected_sha256"] != evidence["observed_sha256"], "Archive alteration was not detected from actual bytes.")
        observations["artifact_integrity"] = False
        return observations
    records = session.invoke("get_candidates")["records"]
    observations["candidate_count"] = len(records)
    if binding["context_kind"] in {"blocked_request", "ambiguous_request"}:
        require(not fixture["routes"] and not fixture["attempts"], "A blocked/ambiguous request acquired an unauthorized route.")
    if probe.get("batches"):
        require(status["attempts_used"] == sum(probe["batches"]), "The selected CPU partition changed its declared attempt count.")
    if "formula" in probe:
        matches = sum(Composition(r["formula"]).reduced_composition == Composition(probe["formula"]).reduced_composition for r in records)
        observations["formula_matches"] = matches
        require(matches == probe.get("expected_formula_matches", len(records)), "Fixture does not expose the intended composition outcomes.")
    if "space_group" in probe:
        values = [SpacegroupAnalyzer(Structure.from_file(r["path"]), symprec=.01, angle_tolerance=5).get_space_group_number() for r in records]
        require(all(v == probe["space_group"] for v in values), "Actual fixture symmetry differs from the template conditions.")
        observations["space_groups"] = values
    if "element_set" in probe:
        observations["element_matches"] = sum(set(r["elements"]) == set(probe["element_set"]) for r in records)
    if "sanity_only" in probe:
        checks = [analyze_cif(Path(r["path"]).read_text(), set(probe["sanity_only"])) for r in records]
        require(all(c["parse_valid"] and c["basic_geometry_pass"] and c["composition_match"] for c in checks), "Structural sanity fixture is invalid.")
        require(all(not session.invoke("get_metrics", {"candidate_id": r["candidate_id"]})["metrics"] for r in records), "Sanity-only context contains scientific performance evidence.")
        observations["scientific_property_metrics"] = 0
    if "metric_selection" in probe or "version_metric" in probe:
        selected = probe.get("metric_selection", probe.get("version_metric"))
        args = {"candidate_id": selected["candidate_id"]}
        if "version_key" in selected:
            versions = session.invoke("get_versions", args)["records"]
            version = next(v for v in versions if v["version_key"] == selected["version_key"])
            parent = next(v for v in versions if v["version_key"] == selected["parent_version_key"])
            require(version["parent_version_id"] == parent["structure_version_id"], "Parent/current structure identity was lost.")
            args["structure_version_id"] = version["structure_version_id"]
            observations["version_keys"] = [v["version_key"] for v in versions]
        values = session.invoke("get_metrics", args)["metrics"]
        require(len(values) == 1 and values[0]["property"] == selected["property"] and values[0]["value"] == selected["value"], "Selected candidate/version metric differs.")
        observations["selected_metric_value"] = values[0]["value"]
    if probe.get("equivalent_files"):
        require(len(records) == probe["equivalent_files"] and len({r["sha256"] for r in records}) == len(records), "Expected distinct CIF byte identities.")
        matcher = StructureMatcher(ltol=.2, stol=.3, angle_tol=5, primitive_cell=True, scale=True, attempt_supercell=False)
        require(matcher.fit(*[Structure.from_file(r["path"]) for r in records]), "Expected structural equivalence was not reproduced.")
        observations["equivalent_structure_groups"] = 1
    if probe.get("different_methods"):
        values = [session.invoke("get_metrics", {"candidate_id": r["candidate_id"]})["metrics"][0] for r in records]
        result = compare_metrics(values)
        require(result["comparable"] is False, "Method mismatch was not retained.")
        observations["methods_comparable"] = False
    if "database_failure" in probe:
        query = next(q for q in fixture["database_queries"] if q["query_id"] == probe["database_failure"])
        result = session.invoke("query_database", {k: query[k] for k in ("database", "query")})
        require(result["status"] == "failed" and result["novelty"] == "unknown" and result["records"] is None, "Database failure was changed into a novelty claim.")
        observations["database_novelty"] = "unknown"
    return observations


def audit(suite_path, manifest_path, work_directory, *, proposal_path=None):
    suite = json.loads(Path(suite_path).read_text(encoding="utf-8"))
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    tasks = validate_bindings(suite, manifest)
    revision = None
    if suite.get("draft_version", 1) == 2 or manifest.get("draft_version", 1) == 2:
        require(suite.get("draft_version") == manifest.get("draft_version") == 2 and proposal_path is not None,
                "Candidate v2 needs its explicit pending revision proposal before coverage can run.")
        revision = validate_revision(proposal_path, suite_path, manifest_path)
    else:
        require(proposal_path is None, "A v2 proposal cannot be applied to the original draft.")
    work = Path(work_directory)
    work.mkdir(parents=True, exist_ok=False)
    rows = []
    for task in tasks:
        binding = manifest["task_bindings"][task["id"]]
        row = {"task_id": task["id"], "fixture_id": task["fixture_id"], "initially_executable_annotation": task["initially_executable"],
               "context_kind": binding["context_kind"], "inputs_and_state": binding["inputs_and_state"], "review_notes": binding["review_notes"],
               "cpu_status": "not_run", "task_correctness": None, "formal_approval": False}
        try:
            session = ReplaySession(manifest_path, task["fixture_id"], work / task["id"], "coverage_" + task["id"])
            row["observations"] = probe_fixture(session, manifest["fixtures"][task["fixture_id"]], binding)
            row["trace_receipt"] = verify_trace(session.root)
            row["cpu_status"] = "passed"
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, StopIteration) as exc:
            row.update(cpu_status="failed", error=str(exc))
        rows.append(row)
    return {"format": "matpilot-candidate-fixture-coverage/v1", "scope": "synthetic_cpu_context_only_not_ai_effectiveness_or_formal_review",
            "draft_version": suite.get("draft_version", 1), "revision_validation": revision,
            "suite_sha256": hashlib.sha256(Path(suite_path).read_bytes()).hexdigest(), "manifest_sha256": hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
            "tasks": len(tasks), "resolved_fixtures": len({t["fixture_id"] for t in tasks}),
            "cpu_passed": sum(r["cpu_status"] == "passed" for r in rows), "cpu_failed": sum(r["cpu_status"] == "failed" for r in rows),
            "generation_contexts": sum(r["context_kind"] == "generation" for r in rows),
            "unresolved_route_tasks": [r["task_id"] for r in rows if r["context_kind"] == "ambiguous_request"],
            "pending_semantic_adjudications": [r["task_id"] for r in rows if r["review_notes"]],
            "api_requests": 0, "real_model_runs": 0, "formal_ready": False, "rows": rows}


def markdown(report):
    lines = ["# 36题候选夹具覆盖对照（合成、待审核）", "", "本表只核验CPU回放情境。通过不表示AI答题正确、科学性质已证明或正式题库已审定。", "",
             f"候选版本 {report.get('draft_version', 1)}；夹具关联 {report['resolved_fixtures']}/{report['tasks']}；CPU通过 {report['cpu_passed']}；失败 {report['cpu_failed']}。生成路线缺口：{', '.join(report['unresolved_route_tasks']) or '当前定义内未发现'}。", "",
             "| 题号 | 情境 | 输入、路线或状态 | CPU核验 | 待审问题 |", "|---|---|---|---|---|"]
    for row in report["rows"]:
        notes = "；".join(n["detail"] for n in row["review_notes"]) or "仍待独立领域审定"
        lines.append(f"| {row['task_id']} | {row['context_kind']} | {row['inputs_and_state']} | {row['cpu_status']} | {notes} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite"); parser.add_argument("manifest"); parser.add_argument("work_directory"); parser.add_argument("output_json")
    parser.add_argument("--markdown")
    parser.add_argument("--proposal", help="Required for candidate v2; validates retained originals and disclosed revisions.")
    args = parser.parse_args()
    report = audit(args.suite, args.manifest, args.work_directory, proposal_path=args.proposal)
    save_new(args.output_json, report)
    if args.markdown:
        with Path(args.markdown).open("x", encoding="utf-8") as stream:
            stream.write(markdown(report))
    print(json.dumps({k: report[k] for k in ("tasks", "cpu_passed", "cpu_failed", "unresolved_route_tasks", "formal_ready")}))
    return int(bool(report["cpu_failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
