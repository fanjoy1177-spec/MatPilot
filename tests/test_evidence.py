import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from matpilot.config import Settings
from matpilot.evidence import compare_metrics, check_requirements, metric_usable
from matpilot.service import Service
from matpilot.worker import execute_job
from test_requirements import requirement


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.service = Service(Settings(data_dir=self.root))

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, request=None):
        plan = self.service.prepare_plan(request or {"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 2, "batch_size": 1})
        self.assertEqual(plan["state"], "ready", plan["issues"])
        job = self.service.store.create_job(plan, "evidence-" + str(len(self.service.store.jobs())), "demo", self.service.settings.snapshot())[0]
        execute_job(self.service.store, self.service.store.claim(os.getpid()))
        return plan, self.service.get_job(job["job_id"])

    def metric(self, record, value=1.0):
        raw = self.root / (record["candidate_id"] + "-metric.json")
        raw.write_text(json.dumps({"test_fixture_only": True, "value": value}))
        return {"candidate_id": record["candidate_id"], "structure_version_id": record["structure_version_id"],
                "property": "dft_band_gap", "value": value, "unit": "eV",
                "method": {"name": "test-fixture", "version": "1", "weights_identity": "not_applicable", "parameters": {}},
                "input_artifact": {"path": record["path"], "sha256": record["sha256"]},
                "raw_output": {"path": str(raw), "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()},
                "convergence": "not_applicable", "reference_set": None}

    def test_identical_formula_and_bytes_do_not_replace_candidate_ids(self):
        plan, job = self.execute()
        result = self.service.screen_candidates(job["job_id"])
        one, two = result["records"]
        self.assertEqual(one["formula"], two["formula"])
        self.assertEqual(one["sha256"], two["sha256"])
        self.assertNotEqual(one["candidate_id"], two["candidate_id"])
        self.assertNotEqual(one["structure_version_id"], two["structure_version_id"])
        self.assertEqual(result["valid_candidate_count"], 2)
        self.assertEqual(result["unique_valid_candidate_count"], 1)
        self.assertEqual(two["equivalent_to_candidate_id"], one["candidate_id"])
        self.assertTrue(two["exact_file_duplicate"])
        self.assertEqual(self.service.store.structure_version(one["structure_version_id"])["candidate_id"], one["candidate_id"])

    def test_structure_equivalence_is_not_file_hash_equality(self):
        from pymatgen.core import Lattice, Structure
        from pymatgen.io.cif import CifWriter
        def fixture(target, count):
            crystal = Structure(Lattice.cubic(5.64), ["Na", "Cl"], [[0, 0, 0], [0.5, 0.5, 0.5]])
            with zipfile.ZipFile(target, "w") as z:
                for i in range(count):
                    z.writestr(f"{i}.cif", f"# fixture {i}\n" + str(CifWriter(crystal)))
        with patch("matpilot.worker.demo_archive", side_effect=fixture):
            _, job = self.execute({"unconditional": True, "num_samples": 2, "batch_size": 2})
        result = self.service.screen_candidates(job["job_id"])
        self.assertFalse(result["records"][1]["exact_file_duplicate"])
        self.assertEqual(result["records"][1]["structural_equivalence_status"], "equivalent")
        self.assertEqual(result["unique_valid_candidate_count"], 1)

    def test_wrong_stoichiometry_does_not_count_as_qualified(self):
        raw = "精确NaCl2"
        req = requirement(raw, "NaCl2", "stoichiometry", "chemical_formula", "NaCl2")
        plan, job = self.execute({"raw_request": raw, "requirements": [req], "unconditional": True,
                                  "search_mode": "generate_then_filter", "num_samples": 2, "valid_candidates_target": 1})
        result = self.service.screen_candidates(job["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["unique_valid_candidate_count"], 0)
        self.assertFalse(result["valid_target_met"])
        self.assertEqual(result["records"][0]["requirement_checks"][0]["status"], "fail")
        report = self.service.build_report(job["job_id"])
        self.assertIn("目标是否达到：False", Path(report["report_path"]).read_text(encoding="utf8"))

    def test_unknown_research_goal_is_separate_from_structural_success(self):
        raw = "Na-Cl，研究析氧活性"
        reqs = [requirement(raw, "Na-Cl", "element_set", "chemical_system", "Na-Cl"),
                requirement(raw, "析氧活性", "research_goal", "oer_activity", "析氧活性", rid="r2", relation="goal", strength="goal")]
        _, job = self.execute({"raw_request": raw, "requirements": reqs, "conditions": {"chemical_system": "Na-Cl"}, "num_samples": 1})
        record = self.service.screen_candidates(job["job_id"])["records"][0]
        self.assertTrue(record["all_hard_requirements_pass"])
        self.assertFalse(record["research_goals_verified"])
        self.assertEqual(record["requirement_checks"][1]["status"], "unknown")

    def test_metric_cannot_be_assigned_by_formula_or_to_another_candidate(self):
        _, job = self.execute()
        one, two = self.service.get_results(job["job_id"])["records"]
        metric = self.metric(one)
        with self.assertRaisesRegex(ValueError, "another candidate"):
            self.service.store.record_metric({**metric, "candidate_id": two["candidate_id"]})
        saved = self.service.store.record_metric(metric)
        self.assertEqual(saved["candidate_id"], one["candidate_id"])
        self.assertEqual(self.service.store.metrics(two["structure_version_id"]), [])

    def test_transformed_version_has_parent_and_does_not_reuse_metrics(self):
        _, job = self.execute()
        original = self.service.get_results(job["job_id"])["records"][0]
        self.service.store.record_metric(self.metric(original))
        transformed = self.root / "transformed.cif"
        transformed.write_text("# different serialization\n" + Path(original["path"]).read_text(), encoding="utf8")
        newer = self.service.store.add_structure_version(original["structure_version_id"], transformed, "test_transform", {"method": None, "convergence": "unknown"})
        self.assertEqual(newer["candidate_id"], original["candidate_id"])
        self.assertEqual(newer["parent_version_id"], original["structure_version_id"])
        self.assertNotEqual(newer["structure_version_id"], original["structure_version_id"])
        self.assertEqual(self.service.store.metrics(newer["structure_version_id"]), [])
        self.assertEqual(len(self.service.store.structure_versions(original["candidate_id"])), 2)

    def test_metric_input_and_raw_output_are_verified(self):
        _, job = self.execute()
        record = self.service.get_results(job["job_id"])["records"][0]
        metric = self.metric(record)
        wrong = deepcopy(metric)
        wrong["input_artifact"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.service.store.record_metric(wrong)
        self.service.store.record_metric(metric)
        Path(metric["raw_output"]["path"]).write_text("changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.service.store.metrics(record["structure_version_id"])

    def test_missing_metadata_and_unconverged_values_stay_unknown(self):
        _, job = self.execute()
        record = self.service.get_results(job["job_id"])["records"][0]
        metric = self.metric(record)
        metric["method"] = {"name": None, "version": None, "weights_identity": None, "parameters": None}
        metric["convergence"] = "unknown"
        saved = self.service.store.record_metric(metric)
        self.assertIsNone(saved["method"]["name"])
        self.assertFalse(metric_usable(saved))

    def test_same_units_do_not_make_scientific_methods_comparable(self):
        _, job = self.execute()
        records = self.service.get_results(job["job_id"])["records"]
        one = self.service.store.record_metric(self.metric(records[0]))
        two_data = self.metric(records[1], 2.0)
        two_data["method"]["version"] = "2"
        two = self.service.store.record_metric(two_data)
        self.assertFalse(compare_metrics([one, two])["comparable"])
        self.assertNotIn("ranking", compare_metrics([one, two]))

    def test_convex_hull_metric_needs_reference_identity(self):
        _, job = self.execute()
        record = self.service.get_results(job["job_id"])["records"][0]
        metric = self.metric(record)
        metric.update(property="energy_above_hull", unit="eV/atom", reference_set=None)
        saved = self.service.store.record_metric(metric)
        self.assertFalse(metric_usable(saved))

    def test_generated_guidance_is_checked_against_actual_structure(self):
        _, job = self.execute({"conditions": {"chemical_system": "Fe-Ni-O"}, "num_samples": 1})
        result = self.service.screen_candidates(job["job_id"])
        self.assertEqual(result["records"][0]["requirement_checks"][0]["status"], "fail")
        self.assertEqual(result["valid_candidate_count"], 0)

    def test_metric_for_identical_other_candidate_cannot_satisfy_requirement(self):
        _, job = self.execute()
        one, two = self.service.get_results(job["job_id"])["records"]
        metric = self.service.store.record_metric(self.metric(two))
        plan = {"requirements": [{"requirement_id": "r1", "kind": "numeric_target", "property": "dft_band_gap", "normalized_value": 1,
                                  "normalized_unit": "eV", "strength": "goal", "relation": "target"}]}
        self.assertEqual(check_requirements(plan, one, [metric])[0]["status"], "unknown")
