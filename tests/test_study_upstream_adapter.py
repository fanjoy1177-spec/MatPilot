"""Actual locked upstream functions; scientific engine is a CPU replay fixture."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from study_context_fixture import write_context_fixture


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "benchmarks/study_drivers/upstream_replay.py"
MANIFEST = ROOT / "benchmarks/study/fixtures.development.json"


class StudyUpstreamAdapterTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    def worker(self, group, requests, fixture="development_control_semantics_0", cap=None, manifest=MANIFEST):
        self.counter += 1
        output = self.root / f"run-{self.counter}"
        command = [sys.executable, str(WORKER), "--group", group, "--manifest", str(manifest),
                   "--fixture-id", fixture, "--run-id", f"run-{self.counter}", "--output-dir", str(output)]
        if cap is not None:
            command += ["--sampling-cap", str(cap)]
        result = subprocess.run(command, input="".join(json.dumps({"id": index, **request}) + "\n" for index, request in enumerate(requests)),
                                text=True, encoding="utf-8", capture_output=True, timeout=60,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), len(requests), result.stderr)
        for row in rows:
            self.assertNotIn("error", row, row)
        return [row["result"] for row in rows], output

    @staticmethod
    def generation(**kwargs):
        return {"operation": "generate_structures", "arguments": {"num_structures": 1,
                "chemical_system": "Fe-Ni-O", "guidance_scale": 2, "batch_size": 1, **kwargs}}

    def test_actual_u_rounds_up_but_uplus_preserves_hard_odd_budget(self):
        request = self.generation(num_structures=3, batch_size=2)
        u, _ = self.worker("U", [request], cap=3)
        fixed, output = self.worker("U+", [request], cap=3)
        self.assertIn("error", u[0]["upstream_result"])
        self.assertEqual(u[0]["generator_calls"][0]["actual_engine_request"]["num_samples"], 4)
        self.assertEqual(u[0]["replay_status"]["attempts_used"], 0)
        self.assertEqual(u[0]["replay_status"]["requested_samples"], 4)
        self.assertEqual([c["batch_size"] for c in fixed[0]["generator_calls"]], [2, 1])
        self.assertEqual(fixed[0]["upstream_result"]["num_generated"], 3)
        self.assertEqual(fixed[0]["upstream_result"]["sampling_attempts"], 3)
        self.assertEqual(fixed[0]["replay_status"]["attempts_used"], 3)
        metadata = json.loads(Path(fixed[0]["upstream_result"]["metadata_path"]).read_text())
        self.assertEqual(metadata["batch_counts"], [2, 1])
        self.assertTrue((output / "upstream-call-0001.json").is_file())

    def test_actual_u_silently_overrides_condition_while_uplus_rejects(self):
        request = self.generation(model_name="dft_band_gap", properties={"dft_band_gap": 2})
        u, _ = self.worker("U", [request])
        fixed, _ = self.worker("U+", [request])
        actual = u[0]["generator_calls"][0]["actual_engine_request"]
        self.assertEqual(actual["model_name"], "chemical_system")
        self.assertEqual(actual["conditions"], {"chemical_system": "Fe-Ni-O"})
        self.assertEqual(u[0]["upstream_result"]["num_generated"], 1)
        self.assertIn("none were discarded", fixed[0]["upstream_result"]["error"])
        self.assertEqual(fixed[0]["generator_calls"], [])
        self.assertEqual(fixed[0]["replay_status"]["attempts_used"], 0)

    def test_constructor_source_and_normal_instructions_are_locked_not_rewritten(self):
        rows, output = self.worker("U", [{"operation": "identity", "arguments": {}}, {"operation": "describe_tools", "arguments": {}}])
        identity, description = rows
        self.assertEqual(identity["source_revision"], "6afdd465a35088136e4875d7d2fbaefc633f4adf")
        self.assertIn("wrapper.__init__", identity["executed_function_hashes"])
        self.assertIsNone(identity["applied_patch_sha256"])
        self.assertFalse(identity["science_computation_performed"])
        self.assertEqual(description["tools"][0]["name"], "generate_structures")
        self.assertIn("chemical_system parameter overrides", description["tools"][0]["description"])
        for entry in identity["normal_instructions"].values():
            data = Path(entry["path"]).read_text(encoding="utf-8").encode()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256_utf8_lf"])
        instructions = identity["normal_instructions"]["skills/ml-generative-mattergen/SKILL.md"]
        text = Path(instructions["path"]).read_text(encoding="utf-8")
        self.assertIn("filter", text.lower())
        self.assertTrue((output / "driver-identity.json").is_file())

    def test_upstream_rewritten_cif_has_new_identity_without_metric_inheritance(self):
        rows, _ = self.worker("U+", [self.generation()])
        mapping = rows[0]["candidate_mapping"][0]
        self.assertEqual(mapping["candidate_id"], "c_a")
        self.assertNotEqual(mapping["structure_version_id"], mapping["parent_version_id"])
        self.assertEqual(hashlib.sha256(Path(mapping["path"]).read_bytes()).hexdigest(), mapping["sha256"])
        self.assertEqual(mapping["provenance"]["kind"], "synthetic")
        self.assertIn("none", mapping["metric_inheritance"])

    def test_upstream_read_boundary_exposes_shared_versions_database_failure_and_integrity(self):
        manifest = write_context_fixture(self.root, tampered=True)
        requests = [{"operation": "replay", "arguments": {"operation": name, "arguments": args}} for name, args in [
            ("get_versions", {"candidate_id": "c_a"}),
            ("query_database", {"database": "synthetic_materials_db", "query": {"chemical_system": "Na-Cl"}}),
            ("verify_candidate_artifact", {"candidate_id": "c_b"})]]
        rows, _ = self.worker("U+", requests, fixture="cpu_context", manifest=manifest)
        self.assertEqual([r["version_key"] for r in rows[0]["records"]], ["s_a1", "s_a2"])
        self.assertEqual((rows[1]["status"], rows[1]["novelty"], rows[1]["records"]), ("failed", "unknown", None))
        self.assertFalse(rows[2]["intact"])
        self.assertNotEqual(rows[2]["expected_sha256"], rows[2]["observed_sha256"])

    def test_actual_patched_partial_result_breaks_without_replenishment(self):
        request = self.generation(chemical_system=None, num_structures=3, batch_size=2)
        rows, _ = self.worker("U+", [request], fixture="development_partial_failure_0")
        result = rows[0]
        self.assertEqual(result["upstream_result"]["status"], "partial")
        self.assertEqual(result["upstream_result"]["num_generated"], 1)
        self.assertEqual(result["upstream_result"]["sampling_attempts"], 2)
        self.assertEqual(len(result["generator_calls"]), 1)
        self.assertEqual(result["generator_calls"][0]["replay_result"]["errors"][0]["code"], "synthetic_timeout")
        self.assertEqual(result["replay_status"]["remaining_budget"], 1)

    def test_candidate_artifact_access_is_available_to_baselines(self):
        requests = [{"operation": "replay", "arguments": {"operation": "get_candidates", "arguments": {}}},
                    {"operation": "replay", "arguments": {"operation": "get_metrics", "arguments": {"candidate_id": "c_b"}}},
                    {"operation": "trace_receipt", "arguments": {}}]
        rows, _ = self.worker("U", requests, fixture="development_candidate_evidence_0")
        self.assertEqual([x["candidate_id"] for x in rows[0]["records"]], ["c_a", "c_b"])
        self.assertEqual(rows[1]["metrics"][0]["value"], 1.25)
        self.assertEqual(rows[1]["metrics"][0]["provenance"]["kind"], "synthetic")
        self.assertEqual(rows[2]["events"], 2)

    def test_actual_diffcsp_server_and_wrapper_use_registered_template_and_shared_replay(self):
        request = {"operation": "generate_structures_with_symmetry", "arguments": {
            "spacegroup": 221, "wyckoff_letters": "1a,1b", "atom_types": "Na,Cl",
            "model_name": "mp_csp", "num_samples": 1, "step_lr": 1e-5, "batch_size": 1}}
        rows, _ = self.worker("U", [{"operation": "get_templates", "arguments": {}}, request,
                                   {"operation": "list_upstream_artifacts", "arguments": {}}],
                              fixture="development_missing_information_0")
        self.assertEqual(rows[0]["templates"][0]["space_group"], 221)
        result = rows[1]
        self.assertEqual(result["upstream_result"]["num_generated"], 1)
        # The actual upstream server intentionally returns only these two fields.
        self.assertEqual(set(result["upstream_result"]), {"num_generated", "output_dir"})
        self.assertEqual(result["generator_calls"][0]["actual_engine_request"]["conditions"]["wyckoff_letters"], ["a", "b"])
        self.assertEqual(result["candidate_mapping"][0]["candidate_id"], "c_a")
        self.assertTrue(any(x["relative_path"].endswith("generation_metadata.json") for x in rows[2]["artifacts"]))
        self.assertIn("diffcsp.wrapper.generate_with_symmetry", result["identity"]["executed_function_hashes"])
        self.assertTrue(any("not tested or claimed equivalent" in x for x in result["identity"]["limitations"]))

    def test_diffcsp_unregistered_symmetry_cannot_receive_generic_fixture_success(self):
        request = {"operation": "generate_structures_with_symmetry", "arguments": {
            "spacegroup": 225, "wyckoff_letters": "4a,4b", "atom_types": "Na,Cl", "num_samples": 1}}
        rows, _ = self.worker("U+", [request], fixture="development_missing_information_0")
        self.assertIn("outside the explicitly registered", rows[0]["upstream_result"]["error"])
        self.assertTrue(rows[0]["upstream_result"]["raised_by_upstream_function"])
        self.assertEqual(rows[0]["generator_calls"], [])
        self.assertEqual(rows[0]["replay_status"]["attempts_used"], 0)


if __name__ == "__main__":
    unittest.main()
