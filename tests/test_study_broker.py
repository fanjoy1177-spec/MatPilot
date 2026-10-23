"""Real isolated JSONL broker tests using bundled sources and CPU replay only."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from study_context_fixture import write_context_fixture


ROOT = Path(__file__).resolve().parents[1]
BROKER = ROOT / "benchmarks/study_drivers/broker.py"
MANIFEST = ROOT / "benchmarks/study/fixtures.development.json"
READ_TOOLS = {"status", "list_artifacts", "read_artifact", "get_candidates", "get_metrics",
              "list_templates", "list_upstream_artifacts", "read_upstream_artifact",
              "get_versions", "query_database", "verify_candidate_artifact"}


class BrokerClient:
    """Exercise stdin/stdout, including dynamic plan IDs, without importing the broker."""
    def __init__(self, folder, group, fixture, cap, manifest=MANIFEST):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
        environment["PYTHONIOENCODING"] = "utf-8"
        self.process = subprocess.Popen(
            [sys.executable, str(BROKER), "--group", group, "--manifest", str(manifest),
             "--fixture-id", fixture, "--run-id", folder.name, "--output-dir", str(folder),
             "--sampling-cap", str(cap)], cwd=ROOT, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1)
        self.responses = queue.Queue()
        self.errors = []
        self.index = 0

        def stdout_reader():
            try:
                for line in self.process.stdout:
                    try:
                        self.responses.put(json.loads(line))
                    except json.JSONDecodeError:
                        self.responses.put({"transport_error": line})
            finally:
                self.responses.put(None)

        def stderr_reader():
            for line in self.process.stderr:
                self.errors.append(line)

        self.threads = [threading.Thread(target=stdout_reader, daemon=True), threading.Thread(target=stderr_reader, daemon=True)]
        for thread in self.threads:
            thread.start()

    def call(self, method, arguments=None):
        self.index += 1
        request = {"id": self.index, "method": method, "arguments": arguments or {}}
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        try:
            response = self.responses.get(timeout=45)
        except queue.Empty as exc:
            raise AssertionError("Broker did not reply: " + "".join(self.errors[-30:])) from exc
        if response is None or "transport_error" in response:
            self.threads[1].join(timeout=1)
            raise AssertionError(f"Invalid/closed broker transport: {response}; stderr={''.join(self.errors[-30:])}")
        if response.get("id") != self.index:
            raise AssertionError("Broker response identity mismatch: " + repr(response))
        return response

    def result(self, method, arguments=None):
        response = self.call(method, arguments)
        if "error" in response:
            raise AssertionError(f"Unexpected {method} failure: {response['error']}")
        return response["result"]

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        for thread in self.threads:
            thread.join(timeout=2)
        for stream in (self.process.stdout, self.process.stderr):
            if stream:
                stream.close()


class StudyBrokerTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.tmp.cleanup()

    def broker(self, group="M", fixture="development_control_semantics_0", cap=4, manifest=MANIFEST):
        client = BrokerClient(self.root / f"broker-{len(self.clients)}", group, fixture, cap, manifest)
        self.clients.append(client)
        return client

    def plan(self, client, request=None):
        request = request or {"conditions": {"chemical_system": "Fe-Ni-O"}, "num_samples": 3, "batch_size": 2}
        return client.result("prepare_plan", {"request": request})["plan"]

    def test_group_tools_differ_but_all_read_shared_artifacts_and_templates(self):
        descriptions = {}
        artifact_hashes, template_hashes = [], []
        read_schemas = []
        for group in ("U", "U+", "M"):
            client = self.broker(group, "development_candidate_evidence_0")
            description = client.result("describe_tools")
            descriptions[group] = description
            tools = {t["name"]: t for t in description["tools"]}
            self.assertTrue(READ_TOOLS <= set(tools))
            read_schemas.append({name: tools[name] for name in READ_TOOLS})
            self.assertFalse(description["identity"]["real_compute_allowed"])
            self.assertFalse(description["identity"]["upstream"]["science_computation_performed"])
            self.assertTrue(description["normal_instructions"])
            self.assertNotIn("sample", tools)
            candidates = client.result("get_candidates", {"candidate_ids": ["c_b"]})["records"]
            self.assertEqual(candidates[0]["candidate_id"], "c_b")
            artifacts = client.result("list_artifacts")["artifacts"]
            self.assertIn("c_b.cif", {a["artifact_id"] for a in artifacts})
            artifact = client.result("read_artifact", {"artifact_id": "c_b.cif"})
            self.assertIn("SYNTHETIC WORKFLOW FIXTURE", artifact["content"])
            artifact_hashes.append(artifact["sha256"])
            metric = client.result("get_metrics", {"candidate_id": "c_b", "structure_version_id": candidates[0]["structure_version_id"]})["metrics"][0]
            self.assertEqual(metric["value"], 1.25)
            templates = client.result("list_templates")["templates"]
            template_hashes.append(templates["cubic_nacl"]["sha256"])
            self.assertIn("error", client.call("sample", {}))
        self.assertEqual(len(set(artifact_hashes)), 1)
        self.assertEqual(len(set(template_hashes)), 1)
        self.assertEqual(read_schemas[0], read_schemas[1])
        self.assertEqual(read_schemas[0], read_schemas[2])
        for group in ("U", "U+"):
            names = {t["name"] for t in descriptions[group]["tools"]}
            self.assertTrue({"generate_structures", "generate_structures_with_symmetry"} <= names)
            self.assertNotIn("prepare_plan", names)
        names = {t["name"] for t in descriptions["M"]["tools"]}
        self.assertTrue({"prepare_plan", "execute_plan", "check_candidates"} <= names)
        self.assertNotIn("generate_structures", names)
        self.assertIsNone(descriptions["U"]["identity"]["upstream"]["applied_patch_sha256"])
        self.assertEqual(descriptions["U+"]["identity"]["upstream"]["applied_patch_sha256"],
                         descriptions["M"]["identity"]["upstream"]["applied_patch_sha256"])

    def test_mattergen_plan_executes_actual_patched_upstream_then_checks_candidates(self):
        client = self.broker()
        plan = self.plan(client)
        self.assertEqual(plan["state"], "ready", plan["issues"])
        result = client.result("execute_plan", {"plan_id": plan["plan_id"]})
        self.assertNotIn("error", result["upstream_result"])
        self.assertEqual(result["identity"]["group"], "U+")
        self.assertIsNotNone(result["identity"]["applied_patch_sha256"])
        self.assertIn("wrapper.generate_structures", result["identity"]["executed_function_hashes"])
        calls = result["generator_calls"]
        self.assertEqual([c["actual_engine_request"]["num_samples"] for c in calls], [2, 1])
        self.assertTrue(all(c["actual_engine_request"]["engine"] == "mattergen" for c in calls))
        self.assertEqual(result["replay_status"]["attempts_used"], 3)
        records = client.result("get_candidates")["records"]
        self.assertEqual(len(records), 3)
        mapping = result["candidate_mapping"]
        self.assertEqual({m["candidate_id"] for m in mapping}, {r["candidate_id"] for r in records})
        self.assertTrue(all(m["metric_inheritance"].startswith("none") for m in mapping))
        checked = client.result("check_candidates", {"plan_id": plan["plan_id"], "candidate_ids": [r["candidate_id"] for r in records]})
        self.assertTrue(checked["checks_performed"])
        self.assertFalse(next(r for r in checked["records"] if r["candidate_id"] == "c_c")["all_hard_requirements_pass"])
        self.assertEqual(checked["valid_candidate_count"], 2)
        upstream_artifacts = client.result("list_upstream_artifacts")["artifacts"]
        self.assertTrue(upstream_artifacts)
        artifact = client.result("read_upstream_artifact", {"relative_path": upstream_artifacts[0]["relative_path"]})
        self.assertEqual(artifact["sha256"], upstream_artifacts[0]["sha256"])
        duplicate = client.call("execute_plan", {"plan_id": plan["plan_id"]})
        self.assertEqual(duplicate["error"]["code"], "already_attempted")
        for name in ("sample", "generate_structures", "replay", "submit_job"):
            self.assertIn("error", client.call(name, {}))
        self.assertEqual(client.result("status")["attempts_used"], 3)

    def test_diffcsp_template_plan_executes_same_upstream_and_checks_symmetry(self):
        client = self.broker(fixture="development_missing_information_0", cap=1)
        template = client.result("list_templates")["templates"]["cubic_nacl"]
        plan = self.plan(client, {"model_id": "diffcsp", "template_id": "cubic_nacl",
                                 "conditions": {"chemical_formula": template["chemical_formula"], "space_group": template["space_group"]},
                                 "num_samples": 1, "batch_size": 1})
        self.assertEqual(plan["state"], "ready", plan["issues"])
        result = client.result("execute_plan", {"plan_id": plan["plan_id"]})
        self.assertNotIn("error", result["upstream_result"])
        self.assertIn("diffcsp.wrapper.generate_with_symmetry", result["identity"]["executed_function_hashes"])
        self.assertEqual(result["identity"]["group"], "U+")
        self.assertEqual(result["actual_upstream_arguments"]["step_lr"], 1e-5)
        self.assertEqual(result["generator_calls"][0]["actual_engine_request"]["engine"], "diffcsp")
        self.assertEqual(result["replay_status"]["attempts_used"], 1)
        records = client.result("get_candidates")["records"]
        self.assertEqual(len(records), 1)
        checked = client.result("check_candidates", {"plan_id": plan["plan_id"], "candidate_ids": [records[0]["candidate_id"]]})
        self.assertEqual(checked["unique_valid_candidate_count"], 1)
        symmetry = next(c for c in checked["records"][0]["requirement_checks"] if c["property"] == "space_group")
        self.assertEqual((symmetry["status"], symmetry["observed"]), ("pass", 221))

    def test_blocked_or_unknown_plan_never_starts_the_engine(self):
        client = self.broker()
        plan = self.plan(client, {"raw_request": "Fe-Ni-O，最多4个", "conditions": {"chemical_system": "Fe-Ni-O"},
                                 "num_samples": 4})
        self.assertNotEqual(plan["state"], "ready")
        blocked = client.call("execute_plan", {"plan_id": plan["plan_id"]})
        self.assertEqual(blocked["error"]["code"], "blocked_plan")
        unknown = client.call("execute_plan", {"plan_id": "invented"})
        self.assertEqual(unknown["error"]["code"], "unknown_plan")
        status = client.result("status")
        self.assertEqual((status["attempts_used"], status["requested_samples"]), (0, 0))
        self.assertFalse(client.result("get_candidates")["records"])

    def test_ablations_expose_actual_flags_and_independent_behavior(self):
        mapping = self.broker("M_without_mapping")
        identity = mapping.result("describe_tools")["identity"]
        self.assertFalse(identity["checking_layer"]["requirement_mapping"])
        self.assertTrue(identity["checking_layer"]["evidence_checks"])
        plan = self.plan(mapping, {"raw_request": "Fe-Ni-O，最多4个", "conditions": {"chemical_system": "Fe-Ni-O"},
                                  "num_samples": 3, "batch_size": 2})
        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["requirement_mappings"], [])
        mapping.result("execute_plan", {"plan_id": plan["plan_id"]})
        checked = mapping.result("check_candidates", {"plan_id": plan["plan_id"], "candidate_ids": ["c_c"]})
        self.assertTrue(checked["checks_performed"])
        self.assertFalse(checked["records"][0]["all_hard_requirements_pass"])
        evidence = self.broker("M_without_evidence", "development_candidate_evidence_0")
        identity = evidence.result("describe_tools")["identity"]
        self.assertTrue(identity["checking_layer"]["requirement_mapping"])
        self.assertFalse(identity["checking_layer"]["evidence_checks"])
        plan = self.plan(evidence, {"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 1})
        checked = evidence.result("check_candidates", {"plan_id": plan["plan_id"], "candidate_ids": ["c_a"]})
        self.assertFalse(checked["checks_performed"])
        self.assertIsNone(checked["valid_candidate_count"])
        self.assertNotIn("all_hard_requirements_pass", checked["records"][0])
        self.assertIn("content", evidence.result("read_artifact", {"artifact_id": "c_a.cif"}))

    def test_existing_attempts_and_failed_requests_cannot_expand_budget(self):
        client = self.broker(fixture="development_partial_failure_1", cap=1)
        initial = client.result("status")
        self.assertEqual((initial["attempts_used"], initial["remaining_budget"]), (1, 0))
        plan = self.plan(client, {"conditions": {"chemical_system": "Fe-Ni-O"}, "num_samples": 1, "batch_size": 1})
        self.assertEqual(plan["state"], "ready", plan["issues"])
        attempted = client.result("execute_plan", {"plan_id": plan["plan_id"]})
        self.assertIn("error", attempted["upstream_result"])
        self.assertEqual(attempted["generator_calls"][0]["replay_error"]["code"], "budget_exceeded")
        self.assertEqual(attempted["replay_status"]["attempts_used"], 1)
        self.assertEqual(attempted["replay_status"]["remaining_budget"], 0)
        self.assertEqual(client.call("execute_plan", {"plan_id": plan["plan_id"]})["error"]["code"], "already_attempted")
        revised = self.plan(client, {"conditions": {"chemical_system": "Fe-Ni-O"}, "num_samples": 1, "batch_size": 1, "seed": 7})
        self.assertNotEqual(revised["plan_id"], plan["plan_id"])
        retried = client.result("execute_plan", {"plan_id": revised["plan_id"]})
        self.assertEqual(retried["generator_calls"][0]["replay_error"]["code"], "budget_exceeded")
        status = client.result("status")
        self.assertEqual((status["attempts_used"], status["sampling_cap"], status["remaining_budget"]), (1, 1, 0))
        self.assertEqual(status["requested_samples"], 2)

    def test_all_groups_receive_identical_context_schemas_versions_failures_and_changed_bytes(self):
        manifest = write_context_fixture(self.root, tampered=True)
        facts, schemas = [], []
        for group in ("U", "U+", "M"):
            client = self.broker(group, "cpu_context", manifest=manifest)
            description = client.result("describe_tools")
            self.assertEqual(description["identity"]["shared_context_contract"], "matpilot-shared-replay-context/v2")
            schemas.append({t["name"]: t for t in description["tools"] if t["name"] in READ_TOOLS})
            versions = client.result("get_versions", {"candidate_id": "c_a"})["records"]
            metrics = [client.result("get_metrics", {"candidate_id": "c_a", "structure_version_id": r["structure_version_id"]})["metrics"][0]["value"] for r in versions]
            query = client.result("query_database", {"database": "synthetic_materials_db", "query": {"chemical_system": "Na-Cl"}})
            integrity = client.result("verify_candidate_artifact", {"candidate_id": "c_b"})
            execution = client.result("status")["execution"]
            facts.append({"versions": [(r["version_key"], r["structure_version_id"], r["parent_version_id"], r["sha256"]) for r in versions],
                "metrics": metrics, "database": [query[k] for k in ("status", "novelty", "records", "error")],
                "integrity": [integrity[k] for k in ("intact", "expected_sha256", "observed_sha256")],
                "execution": [execution[k] for k in ("state", "heartbeat_late", "termination_confirmed")]})
            self.assertEqual(query["novelty"], "unknown")
            self.assertIsNone(query["records"])
            self.assertFalse(integrity["intact"])
            self.assertEqual(execution["state"], "running")
            self.assertFalse(execution["termination_confirmed"])
            self.assertIn("error", client.call("get_metrics", {"candidate_id": "c_b"}))
        self.assertEqual(schemas[0], schemas[1])
        self.assertEqual(schemas[0], schemas[2])
        self.assertEqual(facts[0], facts[1])
        self.assertEqual(facts[0], facts[2])


if __name__ == "__main__":
    unittest.main()
