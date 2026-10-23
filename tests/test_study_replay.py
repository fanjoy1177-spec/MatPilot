"""CPU-only synthetic tests: no API calls, model inference or material claims."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from matpilot.evidence import compare_metrics
from matpilot.study_replay import ReplayError, ReplaySession, request_identity, verify_trace


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks/study/fixtures.development.json"


class StudyReplayTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, fixture="development_control_semantics_0", **kwargs):
        self.counter += 1
        return ReplaySession(MANIFEST, fixture, self.root / f"run-{self.counter}", f"run-{self.counter}", **kwargs)

    def sample(self, session, count, route_id="generate"):
        route = self.manifest["fixtures"][session.fixture_id]["routes"][route_id]
        return session.invoke("sample", {**route, "num_samples": count})

    def write_manifest(self, manifest):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_every_development_task_has_an_explicit_fixture(self):
        tasks = json.loads((ROOT / "benchmarks/study/development_tasks.json").read_text(encoding="utf-8"))["tasks"]
        self.assertEqual({t["fixture_id"] for t in tasks}, set(self.manifest["fixtures"]))
        self.assertEqual(self.manifest["purpose"], "development")
        for fixture in self.manifest["fixtures"].values():
            self.assertEqual(fixture["provenance"]["kind"], "synthetic")

    def test_batch_partition_preserves_order_candidate_ids_and_versions(self):
        a, b = self.session(), self.session()
        whole = self.sample(a, 4)["records"]
        divided = self.sample(b, 1)["records"] + self.sample(b, 3)["records"]
        identity = lambda rows: [(r["candidate_id"], r["structure_version_id"], r["sha256"]) for r in rows]
        self.assertEqual(identity(whole), identity(divided))
        self.assertEqual(a.attempts_used, b.attempts_used)
        self.assertNotEqual(whole[0]["path"], divided[0]["path"])
        self.assertEqual(whole[2]["elements"], ["Fe", "O"])

    def test_actual_conditions_and_parameters_are_bound(self):
        session = self.session()
        route = copy.deepcopy(self.manifest["fixtures"][session.fixture_id]["routes"]["generate"])
        route["conditions"]["chemical_system"] = "Na-Cl"
        with self.assertRaisesRegex(ReplayError, "No recorded route"):
            session.invoke("sample", {**route, "num_samples": 1})
        route = copy.deepcopy(self.manifest["fixtures"][session.fixture_id]["routes"]["generate"])
        route["parameters"]["guidance_scale"] = 1
        with self.assertRaisesRegex(ReplayError, "No recorded route"):
            session.invoke("sample", {**route, "num_samples": 1})
        self.assertEqual(session.attempts_used, 0)
        self.assertEqual(session.requested_samples, 2)
        self.assertEqual(verify_trace(session.root)["events"], 2)

    def test_element_order_normalizes_without_inventing_or_dropping_inputs(self):
        route = self.manifest["fixtures"]["development_control_semantics_0"]["routes"]["generate"]
        reordered = copy.deepcopy(route)
        reordered["conditions"]["chemical_system"] = "O-Ni-Fe"
        self.assertEqual(request_identity(route), request_identity(reordered))
        reordered["conditions"]["chemical_system"] = "Fe-Fe-O"
        with self.assertRaises(ReplayError):
            request_identity(reordered)

    def test_equal_integer_float_scientific_parameters_match_actual_replay(self):
        session = self.session()
        route = copy.deepcopy(self.manifest["fixtures"][session.fixture_id]["routes"]["generate"])
        route["parameters"]["guidance_scale"] = 2.0
        result = session.invoke("sample", {**route, "num_samples": 1})
        self.assertEqual(result["num_generated"], 1)
        self.assertEqual(result["records"][0]["candidate_id"], "c_a")
        route["parameters"]["guidance_scale"] = 2.0000000001
        with self.assertRaisesRegex(ReplayError, "No recorded route"):
            session.invoke("sample", {**route, "num_samples": 1})
        route["parameters"]["guidance_scale"] = True
        with self.assertRaisesRegex(ReplayError, "No recorded route"):
            session.invoke("sample", {**route, "num_samples": 1})
        self.assertEqual(session.attempts_used, 1)

    def test_numeric_normalization_keeps_booleans_strings_and_units_distinct(self):
        base = {"engine": "mattergen", "model_name": "test", "conditions": {"target": [2, 1.25]},
                "parameters": {"nested": {"number": 1, "unit": "eV"}}}
        equivalent = copy.deepcopy(base)
        equivalent["conditions"]["target"][0] = 2.0
        equivalent["parameters"]["nested"]["number"] = 1.0
        self.assertEqual(request_identity(base), request_identity(equivalent))
        equivalent["parameters"]["nested"]["number"] = True
        self.assertIs(request_identity(equivalent)["parameters"]["nested"]["number"], True)
        from matpilot.adapters.mattergen import canonical_hash
        self.assertNotEqual(canonical_hash(request_identity(base)), canonical_hash(request_identity(equivalent)))
        for value in ("1", 1.0000001):
            equivalent["parameters"]["nested"]["number"] = value
            self.assertNotEqual(request_identity(base), request_identity(equivalent))
        equivalent = copy.deepcopy(base)
        equivalent["parameters"]["nested"]["unit"] = "meV"
        self.assertNotEqual(request_identity(base), request_identity(equivalent))

    def test_sequence_is_bound_not_just_membership_in_route_registry(self):
        fixture = self.manifest["fixtures"]["development_control_semantics_0"]
        fixture["routes"]["second"] = copy.deepcopy(fixture["routes"]["generate"])
        fixture["routes"]["second"]["parameters"]["guidance_scale"] = 3
        fixture["attempts"][1]["route_id"] = "second"
        path = self.write_manifest(self.manifest)
        session = ReplaySession(path, "development_control_semantics_0", self.root / "sequence", "sequence")
        with self.assertRaisesRegex(ReplayError, "next approved attempt"):
            self.sample(session, 2)
        self.assertEqual(session.attempts_used, 0)
        self.sample(session, 1)
        self.sample(session, 1, "second")
        self.assertEqual(session.attempts_used, 2)

    def test_hard_budget_denial_does_not_silently_truncate_upstream_overrequest(self):
        session = self.session(sampling_cap=3)
        with self.assertRaisesRegex(ReplayError, "Entire request denied"):
            self.sample(session, 4)
        self.assertEqual(session.attempts_used, 0)
        self.assertEqual(session.requested_samples, 4)
        self.assertEqual(self.sample(session, 3)["attempts_consumed"], 3)
        with self.assertRaises(ReplayError):
            self.sample(session, 1)
        self.assertEqual(session.attempts_used, 3)

    def test_partial_failure_consumes_attempt_and_cannot_be_replenished(self):
        session = self.session("development_partial_failure_0")
        result = self.sample(session, 3)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["num_generated"], 2)
        self.assertEqual(result["errors"][0]["attempt"], 2)
        self.assertEqual(result["budget"]["remaining_budget"], 0)
        with self.assertRaises(ReplayError):
            self.sample(session, 1)

    def test_initial_attempt_state_is_shared_and_visible(self):
        session = self.session("development_partial_failure_1")
        status = session.invoke("status")
        self.assertEqual(status["attempts_used"], 1)
        self.assertEqual(status["remaining_budget"], 0)
        self.assertEqual(status["candidate_ids"], ["c_a"])
        with self.assertRaisesRegex(ReplayError, "Entire request denied"):
            session.invoke("sample", {"engine": "mattergen", "model_name": "mattergen_base", "conditions": {}, "parameters": {"guidance_scale": 2}, "num_samples": 2})

    def test_candidate_selection_is_id_and_version_bound_with_synthetic_labels(self):
        session = self.session("development_candidate_evidence_0")
        candidate = session.invoke("get_candidates", {"candidate_ids": ["c_b"]})["records"][0]
        result = session.invoke("get_metrics", {"candidate_id": "c_b", "structure_version_id": candidate["structure_version_id"]})
        self.assertEqual(result["metrics"][0]["value"], 1.25)
        self.assertEqual(result["metrics"][0]["provenance"]["kind"], "synthetic")
        self.assertEqual(result["metrics"][0]["input_artifact"]["sha256"], candidate["sha256"])
        with self.assertRaisesRegex(ReplayError, "do not match"):
            session.invoke("get_metrics", {"candidate_id": "c_b", "structure_version_id": "wrong"})
        with self.assertRaises(ReplayError):
            session.invoke("get_candidates", {"candidate_ids": ["NaCl"]})

    def test_existing_comparability_logic_rejects_different_methods(self):
        session = self.session("development_candidate_evidence_1")
        metrics = [session.invoke("get_metrics", {"candidate_id": candidate})["metrics"][0] for candidate in ("c_a", "c_b")]
        self.assertFalse(compare_metrics(metrics)["comparable"])

    def test_outputs_cannot_change_state_or_read_arbitrary_files(self):
        session = self.session("development_candidate_evidence_0")
        returned = session.invoke("get_candidates")["records"]
        returned[0]["candidate_id"] = "mutated"
        self.assertEqual(session.invoke("get_candidates")["records"][0]["candidate_id"], "c_a")
        with self.assertRaises(ReplayError):
            session.invoke("read_artifact", {"artifact_id": "../../secret"})
        artifact = session.invoke("read_artifact", {"artifact_id": "c_a.cif"})
        self.assertIn("SYNTHETIC WORKFLOW FIXTURE", artifact["content"])
        Path(artifact["path"]).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ReplayError, "changed"):
            session.invoke("read_artifact", {"artifact_id": "c_a.cif"})
        with self.assertRaises(ReplayError):
            session.invoke("get_candidates")

    def test_trace_receipt_detects_modification_and_tail_deletion(self):
        session = self.session()
        session.invoke("status")
        session.invoke("status")
        receipt = verify_trace(session.root)
        verify_trace(session.root, expected_head_sha256=receipt["head_sha256"], expected_events=2)
        tail = session.root / "events/000001.json"
        tail.unlink()
        with self.assertRaisesRegex(ReplayError, "externally frozen"):
            verify_trace(session.root, expected_head_sha256=receipt["head_sha256"], expected_events=2)
        first = session.root / "events/000000.json"
        first.write_text(first.read_text(encoding="utf-8").replace('"run-1"', '"changed"'), encoding="utf-8")
        with self.assertRaises(ReplayError):
            verify_trace(session.root)
        with self.assertRaises(FileExistsError):
            ReplaySession(MANIFEST, session.fixture_id, session.root, "other")

    def test_formal_replay_requires_external_review_and_exact_hashes(self):
        with self.assertRaisesRegex(ReplayError, "Formal replay requires"):
            self.session(formal=True)
        self.manifest["purpose"] = "formal_workflow_study"
        manifest_path = self.write_manifest(self.manifest)
        manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        review = {"manifest_sha256": manifest_hash, "approved": True, "independent_of_implementation": True,
                  "reviewer": "UNIT TEST metadata only, not a real reviewer", "materials_qualification": "synthetic review schema test",
                  "reviewed_at": "unit test", "notes": "No human review occurred; tests cannot authorize a real study.",
                  "fixture_ids": list(self.manifest["fixtures"])}
        review_path = self.root / "review.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        review_hash = hashlib.sha256(review_path.read_bytes()).hexdigest()
        kwargs = dict(formal=True, expected_manifest_sha256=manifest_hash, review_path=review_path, expected_review_sha256=review_hash)
        session = ReplaySession(manifest_path, "development_control_semantics_0", self.root / "formal", "formal", **kwargs)
        self.assertEqual(session.identity["review_sha256"], review_hash)
        review["approved"] = False
        review_path.write_text(json.dumps(review), encoding="utf-8")
        with self.assertRaisesRegex(ReplayError, "Review hash differs"):
            ReplaySession(manifest_path, "development_control_semantics_0", self.root / "changed", "changed", **kwargs)
        kwargs["expected_review_sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ReplayError, "independently attest"):
            ReplaySession(manifest_path, "development_control_semantics_0", self.root / "unapproved", "unapproved", **kwargs)

    def test_manifest_hash_binding_and_invalid_frame_references(self):
        with self.assertRaisesRegex(ReplayError, "Manifest byte hash"):
            self.session(expected_manifest_sha256="0" * 64)
        self.manifest["fixtures"]["development_control_semantics_0"]["attempts"][0]["candidate_id"] = "missing"
        with self.assertRaisesRegex(ReplayError, "must be declared"):
            ReplaySession(self.write_manifest(self.manifest), "development_control_semantics_0", self.root / "invalid", "invalid")

    def context_fixture(self):
        fixture = copy.deepcopy(self.manifest["fixtures"]["development_candidate_evidence_0"])
        fixture.update(sampling_cap=2, initial_attempts=2,
            initial_outcomes=[{"attempt": 1, "candidate_id": "c_a", "error": None},
                              {"attempt": 2, "candidate_id": "c_b", "error": None}])
        candidate = fixture["candidates"]["c_a"]
        candidate["version_key"] = "s_a1"
        child = copy.deepcopy(candidate)
        child.update(version_key="s_a2", parent_version_key="s_a1", transformation="synthetic_relaxation", metrics=[])
        child["structure"]["lattice"][0][0] += 0.1
        candidate["versions"] = [child]
        candidate["current_version_key"] = "s_a2"
        return fixture

    def context_session(self, fixture):
        manifest = {**self.manifest, "fixtures": {"context": fixture}}
        self.counter += 1
        return ReplaySession(self.write_manifest(manifest), "context", self.root / f"context-{self.counter}", f"context-{self.counter}")

    def execution_context(self, state="stopped", confirmed=True):
        return {"job_id": "synthetic_job", "state": state, "observed_at": 100, "heartbeat_at": 80,
                "heartbeat_timeout_seconds": 10,
                "termination": {"confirmed": confirmed, "reason": "stop_requested" if state == "stopped" else "command_exited" if confirmed else "unconfirmed"},
                "provenance": {"kind": "synthetic", "description": "CPU observation; no process or model was started."}}

    def test_multiple_versions_preserve_parent_and_never_inherit_metrics(self):
        session = self.context_session(self.context_fixture())
        versions = session.invoke("get_versions", {"candidate_id": "c_a"})["records"]
        self.assertEqual([v["version_key"] for v in versions], ["s_a1", "s_a2"])
        parent, child = versions
        self.assertEqual(child["parent_version_id"], parent["structure_version_id"])
        self.assertNotEqual(child["sha256"], parent["sha256"])
        self.assertEqual(session.invoke("get_candidates", {"candidate_ids": ["c_a"]})["records"][0]["structure_version_id"], child["structure_version_id"])
        self.assertEqual(session.invoke("get_metrics", {"candidate_id": "c_a"})["metrics"], [])
        metrics = session.invoke("get_metrics", {"candidate_id": "c_a", "structure_version_id": parent["structure_version_id"]})["metrics"]
        self.assertTrue(metrics)
        self.assertTrue(all(m["input_artifact"]["sha256"] == parent["sha256"] for m in metrics))
        with self.assertRaisesRegex(ReplayError, "do not match"):
            session.invoke("get_metrics", {"candidate_id": "c_b", "structure_version_id": parent["structure_version_id"]})
        with self.assertRaisesRegex(ReplayError, "do not match"):
            session.invoke("get_metrics", {"candidate_id": "c_a", "structure_version_id": "s_a1"})
        parent["version_key"] = "mutated"
        self.assertEqual(session.invoke("get_versions", {"candidate_id": "c_a"})["records"][0]["version_key"], "s_a1")

    def test_version_graph_rejects_duplicate_forward_and_foreign_parents(self):
        for changes in ({"version_key": "s_a1"}, {"parent_version_key": "s_a2"}, {"parent_version_key": "c_b"}):
            fixture = self.context_fixture()
            fixture["candidates"]["c_a"]["versions"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ReplayError, "earlier parent"):
                self.context_session(fixture)

    def test_same_immutable_version_identity_cannot_overwrite_alias_metrics(self):
        fixture = self.context_fixture()
        candidate = fixture["candidates"]["c_a"]
        candidate["versions"][0]["metrics"] = copy.deepcopy(candidate["metrics"])
        alias = copy.deepcopy(candidate["versions"][0])
        alias["version_key"] = "s_a3"
        alias["metrics"][0]["value"] = 99
        candidate["versions"].append(alias)
        with self.assertRaisesRegex(ReplayError, "same immutable structure identity") as raised:
            self.context_session(fixture)
        self.assertEqual(raised.exception.code, "version_identity_conflict")
        # Matching values do not make a second declaration a distinct version.
        alias["metrics"][0]["value"] = candidate["versions"][0]["metrics"][0]["value"]
        with self.assertRaisesRegex(ReplayError, "same immutable structure identity"):
            self.context_session(fixture)

    def test_database_failure_is_exact_query_bound_unknown_with_verifiable_raw_output(self):
        fixture = self.context_fixture()
        fixture["database_queries"] = [{"query_id": "db_failure", "database": "example_database",
            "query": {"formula": "NaCl"}, "error": {"code": "timeout", "message": "Synthetic request timeout"},
            "provenance": {"kind": "synthetic", "description": "No network query performed."}}]
        session = self.context_session(fixture)
        request = {"database": "example_database", "query": {"formula": "NaCl"}}
        result = session.invoke("query_database", request)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["novelty"], "unknown")
        self.assertIsNone(result["records"])
        raw = session.invoke("read_artifact", {"artifact_id": result["raw_output"]["artifact_id"]})
        self.assertEqual(json.loads(raw["content"])["request_sha256"], result["request_sha256"])
        with self.assertRaisesRegex(ReplayError, "exact request"):
            session.invoke("query_database", {**request, "query": {"formula": "LiCl"}})
        Path(result["raw_output"]["path"]).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ReplayError, "changed"):
            session.invoke("query_database", request)

    def test_execution_resume_is_derived_from_termination_budget_and_heartbeat(self):
        fixture = self.context_fixture()
        fixture.update(sampling_cap=3, initial_attempts=2, attempts=[], execution=self.execution_context())
        stopped = self.context_session(fixture).invoke("status")["execution"]
        self.assertTrue(stopped["resume_allowed"])
        self.assertTrue(stopped["heartbeat_late"])
        self.assertTrue(stopped["termination_confirmed"])
        fixture["execution"] = self.execution_context("running", False)
        running_session = self.context_session(fixture)
        running = running_session.invoke("status")["execution"]
        self.assertEqual(running["state"], "running")
        self.assertTrue(running["heartbeat_late"])
        self.assertFalse(running["resume_allowed"])
        with self.assertRaisesRegex(ReplayError, "termination has not been confirmed"):
            running_session.invoke("sample", {"engine": "mattergen", "model_name": "mattergen_base", "conditions": {}, "parameters": {"guidance_scale": 2}, "num_samples": 1})
        self.assertEqual(running_session.attempts_used, 2)
        fixture["execution"] = self.execution_context()
        fixture["sampling_cap"] = 2
        self.assertFalse(self.context_session(fixture).invoke("status")["execution"]["resume_allowed"])

    def test_execution_observation_rejects_contradictions_and_modified_receipt(self):
        fixture = self.context_fixture()
        fixture["execution"] = self.execution_context("running", True)
        with self.assertRaisesRegex(ReplayError, "contradict"):
            self.context_session(fixture)
        fixture["execution"] = self.execution_context()
        session = self.context_session(fixture)
        output = session.invoke("status")["execution"]["raw_output"]
        Path(output["path"]).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ReplayError, "changed"):
            session.invoke("status")

    def test_execution_requires_attempt_evidence_and_consistent_terminal_state(self):
        fixture = self.context_fixture()
        fixture["execution"] = self.execution_context()
        del fixture["initial_outcomes"]
        with self.assertRaisesRegex(ReplayError, "complete initial attempt ledger"):
            self.context_session(fixture)
        fixture = self.context_fixture()
        fixture.update(sampling_cap=3, initial_attempts=3,
                       execution=self.execution_context("completed", True))
        fixture["initial_outcomes"].append({"attempt": 3, "candidate_id": None,
            "error": {"code": "engine_error", "message": "Explicit synthetic failed attempt"}})
        with self.assertRaisesRegex(ReplayError, "terminal state contradicts"):
            self.context_session(fixture)
        fixture["execution"] = self.execution_context("failed", True)
        with self.assertRaisesRegex(ReplayError, "terminal state contradicts"):
            self.context_session(fixture)
        fixture["execution"] = self.execution_context("partial", False)
        with self.assertRaisesRegex(ReplayError, "termination evidence contradict"):
            self.context_session(fixture)
        fixture["execution"] = self.execution_context("partial", True)
        self.assertEqual(self.context_session(fixture).invoke("status")["execution"]["state"], "partial")
        fixture.update(initial_candidates=[], initial_attempts=1,
            initial_outcomes=[{"attempt": 1, "candidate_id": None,
                "error": {"code": "engine_error", "message": "No retained outputs"}}])
        with self.assertRaisesRegex(ReplayError, "terminal state contradicts"):
            self.context_session(fixture)
        fixture["execution"] = self.execution_context("failed", True)
        self.assertEqual(self.context_session(fixture).invoke("status")["execution"]["state"], "failed")

    def test_completed_execution_may_use_less_than_its_upper_sampling_cap(self):
        fixture = self.context_fixture()
        fixture.update(sampling_cap=4, execution=self.execution_context("completed", True))
        result = self.context_session(fixture).invoke("status")
        self.assertEqual(result["remaining_budget"], 2)
        self.assertEqual(result["execution"]["state"], "completed")
        self.assertFalse(result["execution"]["resume_allowed"])
        self.assertEqual(result["execution"]["termination_evidence_scope"], "synthetic_observation_not_live_process_supervision")

    def test_modified_attempt_ledger_blocks_resume_before_charging_new_attempts(self):
        fixture = self.context_fixture()
        fixture.update(sampling_cap=3, execution=self.execution_context())
        session = self.context_session(fixture)
        status = session.invoke("status")
        self.assertTrue(status["execution"]["resume_allowed"])
        artifact = status["execution"]["attempt_ledger_artifact"]
        Path(artifact["path"]).write_text('{"initial_outcomes": []}', encoding="utf-8")
        with self.assertRaisesRegex(ReplayError, "changed"):
            session.invoke("sample", {"engine": "mattergen", "model_name": "mattergen_base", "conditions": {},
                "parameters": {"guidance_scale": 2}, "num_samples": 1})
        self.assertEqual(session.attempts_used, 2)

    def test_archived_cif_changes_remain_detectable_even_when_unparseable(self):
        for content in ({"observed_text": "not a CIF"}, {"observed_append_text": "\n# synthetic modified archived bytes\n"}):
            fixture = self.context_fixture()
            fixture.update(sampling_cap=3, initial_attempts=2, execution=self.execution_context())
            fixture["artifact_observations"] = [{"candidate_id": "c_a", "version_key": "s_a2", **content,
                "provenance": {"kind": "synthetic", "description": "Controlled archive modification."}}]
            session = self.context_session(fixture)
            version = session.invoke("get_versions", {"candidate_id": "c_a"})["records"][1]
            result = session.invoke("verify_candidate_artifact", {"candidate_id": "c_a", "structure_version_id": version["structure_version_id"]})
            self.assertFalse(result["intact"])
            self.assertEqual(result["expected_sha256"], version["sha256"])
            raw = session.invoke("read_artifact", {"artifact_id": result["observed_artifact"]["artifact_id"]})
            self.assertEqual(hashlib.sha256(raw["content"].encode()).hexdigest(), result["observed_sha256"])
            self.assertFalse(session.invoke("status")["execution"]["resume_allowed"])
            with self.assertRaisesRegex(ReplayError, "changed"):
                session.invoke("get_candidates", {"candidate_ids": ["c_a"]})
            with self.assertRaisesRegex(ReplayError, "changed"):
                session.invoke("get_metrics", {"candidate_id": "c_a"})
            self.assertTrue(session.invoke("get_metrics", {"candidate_id": "c_a", "structure_version_id": version["parent_version_id"]})["metrics"])

    def test_initial_outcomes_account_for_failed_attempt_with_raw_evidence(self):
        fixture = self.context_fixture()
        fixture.update(sampling_cap=4, initial_attempts=3, attempts=[],
            initial_outcomes=[{"attempt": 1, "candidate_id": "c_a", "error": None},
                              {"attempt": 2, "candidate_id": "c_b", "error": None},
                              {"attempt": 3, "candidate_id": None, "error": {"code": "engine_error", "message": "Synthetic failed attempt"}}])
        session = self.context_session(fixture)
        status = session.invoke("status")
        self.assertEqual(status["remaining_budget"], 1)
        self.assertEqual(status["initial_outcomes"][2]["error"]["code"], "engine_error")
        raw = session.invoke("read_artifact", {"artifact_id": status["initial_outcomes_artifact"]["artifact_id"]})
        self.assertEqual(json.loads(raw["content"])["initial_outcomes"], status["initial_outcomes"])
        fixture["initial_outcomes"][2]["attempt"] = 4
        with self.assertRaisesRegex(ReplayError, "contiguous"):
            self.context_session(fixture)

    def test_jsonl_transport_uses_only_replay_and_returns_identity(self):
        result = subprocess.run([sys.executable, "-m", "matpilot.study_replay", "--manifest", str(MANIFEST),
                                 "--fixture-id", "development_candidate_evidence_0", "--run-id", "cli",
                                 "--output-dir", str(self.root / "cli")],
                                input=json.dumps({"id": 1, "operation": "get_metrics", "arguments": {"candidate_id": "c_b"}}) + "\n",
                                text=True, capture_output=True, timeout=60, check=True)
        response = json.loads(result.stdout)
        self.assertEqual(response["id"], 1)
        self.assertFalse(response["result"]["replay"]["science_computation_performed"])
        self.assertEqual(response["result"]["metrics"][0]["value"], 1.25)


if __name__ == "__main__":
    unittest.main()
