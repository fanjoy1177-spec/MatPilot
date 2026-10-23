"""Synthetic candidate-context checks only; no LLM grading or scientific runs."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("candidate_fixture_coverage", ROOT / "benchmarks/validate_candidate_fixtures.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SUITE = ROOT / "benchmarks/study/test_candidates.draft.json"
MANIFEST = ROOT / "benchmarks/study/fixtures.candidates.draft.json"
SUITE_V2 = ROOT / "benchmarks/study/test_candidates.draft.v2.json"
MANIFEST_V2 = ROOT / "benchmarks/study/fixtures.candidates.draft.v2.json"
PROPOSAL_V2 = ROOT / "benchmarks/study/candidate_revision_proposal.v2.json"


class CandidateFixtureTests(unittest.TestCase):
    def setUp(self):
        self.suite = json.loads(SUITE.read_text(encoding="utf-8"))
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_original_task_semantics_are_unchanged_and_review_is_pending(self):
        self.assertEqual(self.manifest["source_task_content_sha256"], "6a0f2b419d7dfb2a56205c0500b45bd61a14ef4ae934de7b5425d26b448454fd")
        self.assertEqual(len(MODULE.validate_bindings(self.suite, self.manifest)), 36)
        self.assertEqual(self.manifest["purpose"], "development")

    def test_prompt_route_or_fixture_deletion_is_not_silently_accepted(self):
        changed = copy.deepcopy(self.suite)
        changed["tasks"][0]["prompt"] += " modified"
        with self.assertRaises(ValueError):
            MODULE.validate_bindings(changed, self.manifest)
        changed = copy.deepcopy(self.manifest)
        del changed["fixtures"]["candidate_t0101"]
        with self.assertRaises(ValueError):
            MODULE.validate_bindings(self.suite, changed)
        changed = copy.deepcopy(self.manifest)
        changed["fixtures"]["candidate_t0201"]["routes"]["generate"]["conditions"]["dft_band_gap"] = 1200
        with self.assertRaises(ValueError):
            MODULE.validate_bindings(self.suite, changed)

    def test_candidate_contexts_cannot_be_marked_formal_by_changing_purpose(self):
        self.manifest["purpose"] = "formal_workflow_study"
        with self.assertRaisesRegex(ValueError, "development and pending"):
            MODULE.validate_bindings(self.suite, self.manifest)

    def test_all_actual_cpu_contexts_preserve_unresolved_generation_and_unknown_results(self):
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            report = MODULE.audit(SUITE, MANIFEST, Path(temporary) / "audit")
        self.assertEqual(report["cpu_passed"], 36, report)
        self.assertEqual(report["cpu_failed"], 0)
        self.assertEqual(report["generation_contexts"], 12)
        self.assertEqual(report["unresolved_route_tasks"], ["T0601"])
        self.assertEqual(report["pending_semantic_adjudications"], ["T0205", "T0305", "T0601"])
        self.assertFalse(report["formal_ready"])
        self.assertTrue(all(row["task_correctness"] is None for row in report["rows"]))
        rows = {row["task_id"]: row for row in report["rows"]}
        self.assertEqual(rows["T0502"]["observations"]["selected_metric_value"], 1.6)
        self.assertEqual(rows["T0505"]["observations"]["database_novelty"], "unknown")
        self.assertFalse(rows["T0605"]["observations"]["execution"]["resume_allowed"])
        self.assertFalse(rows["T0606"]["observations"]["artifact_integrity"])


class CandidateRevisionTests(unittest.TestCase):
    def setUp(self):
        self.base = ROOT / ".work/test-temp"
        self.base.mkdir(parents=True, exist_ok=True)

    def test_three_disclosed_revisions_keep_quota_and_parent_files(self):
        result = MODULE.validate_revision(PROPOSAL_V2, SUITE_V2, MANIFEST_V2)
        self.assertEqual(result["changed_tasks"], ["T0205", "T0305", "T0601"])
        self.assertEqual(result["quota_counts"], {"tasks": 36, "cross_model": 9, "initially_executable": 18})
        self.assertEqual(result["approval"], "pending_user_and_material_review")
        self.assertTrue(result["parent_files_verified"])
        before = json.loads(SUITE.read_text(encoding="utf-8"))
        after = json.loads(SUITE_V2.read_text(encoding="utf-8"))
        a, b = ({t["id"]: t for t in s["tasks"]} for s in (before, after))
        self.assertEqual(a["T0205"]["prompt"], b["T0205"]["prompt"])
        self.assertNotIn("mattergen_base", a["T0601"]["prompt"])
        self.assertIn("mattergen_base", b["T0601"]["prompt"])
        self.assertEqual(a["T0305"]["initially_executable"], b["T0305"]["initially_executable"])

    def test_v2_cannot_skip_revision_proof_or_relabel_original_as_v2(self):
        with tempfile.TemporaryDirectory(dir=self.base) as temporary:
            with self.assertRaisesRegex(ValueError, "explicit pending revision"):
                MODULE.audit(SUITE_V2, MANIFEST_V2, Path(temporary) / "missing-proof")
            with self.assertRaisesRegex(ValueError, "original draft"):
                MODULE.audit(SUITE, MANIFEST, Path(temporary) / "wrong-proof", proposal_path=PROPOSAL_V2)
            self.assertFalse((Path(temporary) / "missing-proof").exists())

    def test_proposal_rejects_forged_approval_omitted_edit_and_incorrect_quota(self):
        with tempfile.TemporaryDirectory(dir=self.base) as temporary:
            directory = Path(temporary)
            for original in (SUITE, MANIFEST, SUITE_V2, MANIFEST_V2, PROPOSAL_V2):
                (directory / original.name).write_bytes(original.read_bytes())
            original = json.loads(PROPOSAL_V2.read_text(encoding="utf-8"))
            changes = [lambda p: p.update(approved=True), lambda p: p["changes"].pop(),
                       lambda p: p["quota_impact"].update(initially_executable_after=19),
                       lambda p: p["changes"][0]["after"].update(prompt="Different from the saved revised task")]
            for change in changes:
                proposal = copy.deepcopy(original)
                change(proposal)
                (directory / PROPOSAL_V2.name).write_text(json.dumps(proposal), encoding="utf-8")
                with self.assertRaises(ValueError):
                    MODULE.validate_revision(directory / PROPOSAL_V2.name, directory / SUITE_V2.name, directory / MANIFEST_V2.name)

    def test_actual_v2_contexts_add_only_explicit_budget_route_and_keep_review_pending(self):
        with tempfile.TemporaryDirectory(dir=self.base) as temporary:
            result = MODULE.audit(SUITE_V2, MANIFEST_V2, Path(temporary) / "audit", proposal_path=PROPOSAL_V2)
        self.assertEqual(result["cpu_passed"], 36, result)
        self.assertEqual(result["cpu_failed"], 0)
        self.assertEqual(result["generation_contexts"], 13)
        self.assertEqual(result["unresolved_route_tasks"], [])
        self.assertEqual(result["pending_semantic_adjudications"], ["T0205", "T0305", "T0601"])
        self.assertFalse(result["formal_ready"])
        self.assertTrue(all(row["task_correctness"] is None for row in result["rows"]))
        row = next(row for row in result["rows"] if row["task_id"] == "T0601")
        self.assertEqual(row["observations"]["attempts_used"], 3)
        self.assertEqual(row["observations"]["remaining_budget"], 0)

    def test_budget_is_an_upper_bound_and_alternative_partitions_remain_available(self):
        manifest = json.loads(MANIFEST_V2.read_text(encoding="utf-8"))
        binding = manifest["task_bindings"]["T0601"]
        fixture = manifest["fixtures"][binding["fixture_id"]]
        self.assertFalse(binding["probe"]["sampling_policy"]["must_exhaust_budget"])
        with tempfile.TemporaryDirectory(dir=self.base) as temporary:
            for index, batches in enumerate(([2, 1], [1, 1, 1], [1, 2], [2], [1])):
                session = MODULE.ReplaySession(MANIFEST_V2, binding["fixture_id"], Path(temporary) / str(index), "partition_" + str(index))
                for count in batches:
                    result = session.invoke("sample", {**fixture["routes"]["generate"], "num_samples": count})
                self.assertEqual(result["budget"]["attempts_used"], sum(batches))
                self.assertEqual(result["budget"]["remaining_budget"], 3 - sum(batches))
        proposed = next(t for t in json.loads(SUITE_V2.read_text(encoding="utf-8"))["tasks"] if t["id"] == "T0601")
        self.assertIn("不强制用尽", proposed["gold"]["required_facts"][0])


if __name__ == "__main__":
    unittest.main()
