"""Tiny synthetic JSON fixtures only; no API, replay engine or scientific work."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ablation_analysis", ROOT / "benchmarks/analyze_development_ablation.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def identity(value):
    return hashlib.sha256(value.encode()).hexdigest()


class AblationAnalysisTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".work/test-temp"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.review = self.root / "review"
        (self.review / "runs").mkdir(parents=True)
        self.protocol = {"format": "matpilot-development-ablation-protocol/v1", "task_ids": ["D1", "D2", "D3", "D4"],
                         "groups": ["M", "M_without_mapping", "M_without_evidence"], "repeats": 1, "planned_runs": 12,
                         "common": {"real_compute_allowed": False}, "runtime_pack_sha256": None,
                         "module_changes": {g: dict(zip(("requirement_mapping", "evidence_checks"), values)) for g, values in MODULE.FLAGS.items()}}
        self.metrics = {"format": "matpilot-development-review/v1", "mode": "development", "planned_runs": 12, "selected_runs": 12, "not_selected_runs": 0,
                        "correctness_scored": False, "scientific_results_validated": False, "improvement_or_speedup_estimated": False,
                        **{k: identity(k) for k in ("runtime_pack_sha256", "suite_sha256", "plan_sha256", "ledger_snapshot_sha256")},
                        "completed": 11, "failed": 1, "timeout": 0, "budget_stopped": 0, "missing": 0, "partial": 0, "invalid": 0,
                        "runs": [], "groups": {g: {"api_request_count": 4, "api_accounted_cny": .04} for g in MODULE.GROUPS}}
        self.documents = {}
        for rid, (task, group, repeat) in MODULE.expected_rows(self.protocol).items():
            checks = self.protocol["module_changes"][group]
            data = {"checks_performed": checks["evidence_checks"], "valid_candidate_count": 1 if checks["evidence_checks"] else None,
                    "unique_valid_candidate_count": 1 if checks["evidence_checks"] else None,
                    "records": [{"candidate_id": "c_b", "structure_version_id": "s_b", "requirement_checks": [{"status": "unknown"}]}],
                    "study_checks": checks}
            tools = [] if task == "D4" else [self.tool("check_candidates", data)]
            if task == "D1":
                tools += [self.tool("prepare_plan", {"plan": {"plan_id": "p", "state": "blocked", "issues": [{"code": "synthetic_issue"}], "requirements": [{"normalized_value": 1.2, "normalized_unit": "eV"}]}, "study_checks": checks}),
                          self.tool("execute_plan", {"upstream_result": {"sampling_attempts": 2, "num_generated": 1}, "replay_status": {"attempts_used": 3, "remaining_budget": 0}, "generator_calls": []}),
                          self.tool("compare_candidate_metrics", {"comparable": False if checks["evidence_checks"] else None, "checks_performed": checks["evidence_checks"],
                                                                 "metrics": [{"metric_id": "m", "candidate_id": "c_b", "structure_version_id": "s_b", "method": {"name": "synthetic-A", "version": "test"}}], "study_checks": checks})]
            failed = task == "D4" and group == "M"
            terminal = {"status": "failed" if failed else "completed", "failure": "Synthetic max-tokens" if failed else None,
                        "turn_end_reason": {"kind": "max-tokens" if failed else "completed"}}
            run = {"run_id": rid, "task_id": task, "group": group, "repeat": repeat, "mode": "development", "configuration_hash": identity(group),
                   "audit_status": "verified_record", "execution_status": terminal["status"], "failure": terminal["failure"], "turn_end_reason": terminal["turn_end_reason"],
                   "correctness": None, "human_active_seconds": None, "api_request_count": 1, "api_accounted_cny": .01, "wall_seconds": 2,
                   "tool_call_count": len(tools), "tool_calls_by_name": dict(MODULE.Counter(t["name"] for t in tools)), "clarification_count": 0,
                   "source_hashes": {"trace.json": identity(rid + "trace"), "result.json": identity(rid + "result")},
                   "usage_record_ids": [rid + "-request"], "ledger_records_sha256": identity(rid + "ledger")}
            self.metrics["runs"].append(run)
            self.documents[rid] = {"run": copy.deepcopy(run), "prompt": "SYNTHETIC TEST ONLY", "visible": {"tool_records": tools, "clarifications": [], "terminal": terminal}}
        self.flush()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def tool(name, data):
        return {"name": name, "arguments": {}, "result": {"isError": False, "value": data, "content": [{"type": "text", "text": json.dumps(data)}]}}

    def flush(self):
        (self.root / "protocol.json").write_text(json.dumps(self.protocol), encoding="utf-8")
        (self.review / "metrics.json").write_text(json.dumps(self.metrics), encoding="utf-8")
        for rid, document in self.documents.items():
            (self.review / "runs" / (rid + ".json")).write_text(json.dumps(document), encoding="utf-8")

    def analyze(self):
        self.flush()
        return MODULE.analyze(self.root / "protocol.json", self.review)

    def test_fixed_matrix_preserves_failed_cost_and_null_vs_not_invoked(self):
        result = self.analyze()
        self.assertEqual((result["planned_runs"], result["observed_runs"]), (12, 12))
        self.assertEqual(result["groups"]["M"]["failed"], 1)
        self.assertEqual(result["groups"]["M"]["api_accounted_cny"], .04)
        by_id = {r["run_id"]: r for r in result["runs"]}
        enabled = by_id["D1--M--1"]["operations"]["compare_candidate_metrics"]["calls"][0]
        disabled = by_id["D1--M_without_evidence--1"]["operations"]["compare_candidate_metrics"]["calls"][0]
        self.assertIs(enabled["comparable"], False)
        self.assertIsNone(disabled["comparable"])
        self.assertIs(disabled["checks_performed"], False)
        self.assertEqual(by_id["D4--M--1"]["operations"]["check_candidates"], {"invoked": False, "calls": []})
        self.assertEqual(enabled["metrics"][0]["method"]["name"], "synthetic-A")
        execution = by_id["D1--M--1"]["operations"]["execute_plan"]["calls"][0]
        self.assertEqual(execution["upstream"], {"sampling_attempts": 2, "num_generated": 1})
        self.assertEqual(execution["replay_status"], {"attempts_used": 3, "remaining_budget": 0})
        self.assertFalse(result["correctness_scored"])
        self.assertFalse(result["improvement_or_speedup_estimated"])

    def test_missing_row_duplicate_row_extra_file_and_bad_protocol_rejected(self):
        original = copy.deepcopy(self.metrics)
        for mutate in (lambda: self.metrics["runs"].pop(), lambda: self.metrics["runs"].append(self.metrics["runs"][0]),
                       lambda: self.metrics.update(selected_runs=11), lambda: self.metrics.update(completed=12)):
            self.metrics = copy.deepcopy(original)
            mutate()
            with self.assertRaises(ValueError): self.analyze()
        self.metrics = original
        (self.review / "runs/unplanned.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "per-run files"): self.analyze()

    def test_identity_original_hash_and_record_copy_mismatch_rejected(self):
        rid = "D1--M--1"
        original = copy.deepcopy(self.documents[rid])
        for mutate in (lambda d: d["run"].update(group="M_without_evidence"), lambda d: d["run"]["source_hashes"].update({"trace.json": "bad"}),
                       lambda d: d["run"].update(audit_status="invalid"), lambda d: d["visible"]["terminal"].update(status="failed")):
            self.documents[rid] = copy.deepcopy(original)
            mutate(self.documents[rid])
            with self.assertRaises(ValueError): self.analyze()

    def test_matching_invalid_hash_in_both_copies_still_rejected(self):
        run = self.metrics["runs"][0]
        run["source_hashes"]["trace.json"] = "not-sha256"
        self.documents[run["run_id"]]["run"] = copy.deepcopy(run)
        with self.assertRaisesRegex(ValueError, "SHA256"): self.analyze()

    def test_tool_counts_dual_response_and_switch_identity_rejected(self):
        rid = "D1--M--1"
        original = copy.deepcopy(self.documents[rid])
        self.documents[rid]["visible"]["tool_records"].pop()
        with self.assertRaisesRegex(ValueError, "Tool counts"): self.analyze()
        self.documents[rid] = copy.deepcopy(original)
        self.documents[rid]["visible"]["tool_records"][0]["result"]["value"]["checks_performed"] = False
        with self.assertRaisesRegex(ValueError, "content and value"): self.analyze()
        self.documents[rid] = copy.deepcopy(original)
        result = self.documents[rid]["visible"]["tool_records"][0]["result"]
        result["value"]["study_checks"]["evidence_checks"] = False
        result["content"][0]["text"] = json.dumps(result["value"])
        with self.assertRaisesRegex(ValueError, "checking flags"): self.analyze()

    def test_output_is_create_only_and_public_sources_stay_unchanged(self):
        original = (self.review / "metrics.json").read_bytes()
        output = self.root / "observations.json"
        args = [str(self.root / "protocol.json"), str(self.review), str(output)]
        MODULE.main(args)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["observed_runs"], 12)
        with self.assertRaises(FileExistsError): MODULE.main(args)
        self.assertEqual(original, (self.review / "metrics.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
