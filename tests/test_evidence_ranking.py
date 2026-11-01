"""Synthetic software evidence only: no predictor or scientific accuracy test."""
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pymatgen.core import Lattice, Structure
from pymatgen.io.cif import CifWriter

from matpilot.evidence import validate_metric
from matpilot.evidence_ranking import build_evidence_rank_keys, validate_objective


class EvidenceRankingTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.sequence = 0

    def tearDown(self):
        self.temp.cleanup()

    def record(self, cid, species=("Na", "Cl"), supercell=1):
        coords = [[i / len(species)] * 3 for i in range(len(species))]
        crystal = Structure(Lattice.cubic(5.64), list(species), coords)
        if supercell != 1:
            crystal.make_supercell([supercell, 1, 1])
        path = self.root / (cid + ".cif")
        path.write_text(str(CifWriter(crystal)), encoding="utf-8")
        return {"candidate_id": cid, "structure_version_id": "original_" + cid,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "path": str(path),
                "parent_version_id": None, "transformation": "generated", "job_id": "synthetic_job",
                "formula": "deliberately_untrusted", "test_fixture_only": True}

    def metric(self, record, value=0.1, prop="energy_above_hull", unit="eV/atom", **changes):
        self.sequence += 1
        raw = self.root / f"synthetic_metric_{self.sequence}.json"
        raw.write_text(json.dumps({"test_fixture_only": True, "value": value}), encoding="utf-8")
        data = {"candidate_id": record["candidate_id"], "structure_version_id": record["structure_version_id"],
                "property": prop, "unit": unit, "value": value,
                "method": {"name": "synthetic_evidence_fixture", "version": "1", "weights_identity": "synthetic_no_model", "parameters": {}},
                "input_artifact": {"path": record["path"], "sha256": record["sha256"]},
                "raw_output": {"path": str(raw), "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()},
                "convergence": "not_applicable", "reference_set": {"id": "synthetic_reference", "sha256": "a" * 64},
                **changes}
        return validate_metric(data, record)

    @staticmethod
    def select(metric, job=False):
        selection = {k: metric[k] for k in ("candidate_id", "structure_version_id", "metric_id")}
        if job:
            selection["job_id"] = "synthetic_job"
        return selection

    @staticmethod
    def objective(prop="energy_above_hull", unit="eV/atom", direction="minimize", **bounds):
        return {"property": prop, "unit": unit, "direction": direction, **bounds}

    def run_rank(self, records, metrics, objective=None, selections=None):
        evidence = {}
        for metric in metrics:
            evidence.setdefault(metric["structure_version_id"], []).append(metric)
        return build_evidence_rank_keys(records, evidence, objective or self.objective(),
                                        [self.select(m) for m in metrics] if selections is None else selections)

    def test_hull_compares_across_non_fenio_compositions_with_same_reference(self):
        nacl, silicon = self.record("nacl"), self.record("silicon", ("Si",))
        result = self.run_rank([nacl, silicon], [self.metric(nacl, .18), self.metric(silicon, .03)])
        self.assertLess(result["rank_keys"]["silicon"], result["rank_keys"]["nacl"])
        self.assertTrue(result["ranking_metadata"]["comparability"]["comparable"])
        self.assertEqual(result["ranking_metadata"]["scientific_accuracy"], "not_validated")

    def test_raw_energy_per_atom_and_formation_energy_cannot_rank_across_compositions(self):
        nacl, silicon = self.record("nacl"), self.record("silicon", ("Si",))
        for prop, unit in [("energy", "eV"), ("total_energy", "eV"),
                           ("energy_per_atom", "eV/atom"), ("formation_energy", "eV/atom")]:
            with self.subTest(prop=prop), self.assertRaisesRegex(ValueError, "across compositions"):
                self.run_rank([nacl, silicon], [self.metric(nacl, -3, prop, unit), self.metric(silicon, -4, prop, unit)],
                              self.objective(prop, unit))

    def test_cell_energy_rejects_supercell_but_normalized_energy_accepts_same_composition(self):
        one, two = self.record("one"), self.record("two", supercell=2)
        with self.assertRaisesRegex(ValueError, "atom count"):
            self.run_rank([one, two], [self.metric(one, -3, "total_energy", "eV"), self.metric(two, -6, "total_energy", "eV")],
                          self.objective("total_energy", "eV"))
        result = self.run_rank([one, two], [self.metric(one, -3, "energy_per_atom"), self.metric(two, -3.1, "energy_per_atom")],
                               self.objective("energy_per_atom"))
        self.assertEqual(len(result["rank_keys"]), 2)

    def test_no_explicit_choice_is_review_even_with_one_or_more_measurements(self):
        record = self.record("one")
        metrics = [self.metric(record, .01), self.metric(record, .3)]
        result = self.run_rank([record], metrics, selections=[])
        self.assertEqual(result["rank_keys"], {})
        self.assertEqual(len(result["unresolved_candidates"]), 1)
        self.assertEqual(result["metric_selection_audit"]["unselected_metrics"], metrics)
        unique = self.run_rank([record], metrics[:1], selections=[])
        self.assertEqual(unique["rank_keys"], {})

    def test_explicit_unfavorable_choice_is_not_replaced_by_better_measurement(self):
        record = self.record("one")
        better, chosen = self.metric(record, .01), self.metric(record, .3)
        result = self.run_rank([record], [better, chosen], selections=[self.select(chosen)])
        self.assertEqual(result["rank_keys"]["one"], (0, 0, .3))
        self.assertEqual(result["metric_selection_audit"]["unselected_metrics"], [better])
        self.assertEqual(result["all_metrics_by_structure_version"][record["structure_version_id"]], [better, chosen])
        result["all_metrics_by_structure_version"][record["structure_version_id"]][0]["value"] = 999
        self.assertEqual(better["value"], .01)

    def test_unknown_method_reference_value_and_convergence_remain_review(self):
        record = self.record("one")
        cases = [{"value": None}, {"convergence": "unknown"}, {"convergence": "not_converged"},
                 {"reference_set": None}]
        for field in ("name", "version", "weights_identity", "parameters"):
            method = {"name": "fixture", "version": "1", "weights_identity": "synthetic", "parameters": {}}
            method[field] = None
            cases.append({"method": method})
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.run_rank([record], [self.metric(record, **changes)])
                self.assertEqual(result["rank_keys"], {})
                self.assertEqual(result["unresolved_candidates"][0]["status"], "review")

    def test_method_weights_parameters_or_reference_mismatch_fail_closed(self):
        one, two = self.record("one"), self.record("two")
        first = self.metric(one)
        for field, value in [("version", "2"), ("weights_identity", "other"), ("parameters", {"correction": "different"})]:
            method = deepcopy(first["method"])
            method[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "global ranking"):
                self.run_rank([one, two], [first, self.metric(two, method=method)])
        with self.assertRaisesRegex(ValueError, "reference"):
            self.run_rank([one, two], [first, self.metric(two, reference_set={"id": "other_reference", "sha256": "b" * 64})])

    def test_property_and_unit_selection_must_exactly_match_objective(self):
        record = self.record("one")
        for prop, unit in [("dft_band_gap", "eV"), ("energy_above_hull", "meV/atom")]:
            with self.subTest(prop=prop, unit=unit), self.assertRaisesRegex(ValueError, "property and unit"):
                self.run_rank([record], [self.metric(record, prop=prop, unit=unit)])

    def test_candidate_version_metric_and_job_bindings_are_not_interchangeable(self):
        one, two = self.record("one"), self.record("two")
        metric = self.metric(one)
        for field, value in [("candidate_id", "two"), ("structure_version_id", "original_two"),
                             ("metric_id", "m_" + "f" * 64), ("job_id", "other_job")]:
            selection = {**self.select(metric, job=True), field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_rank([one, two], [metric], selections=[selection])
        accepted = self.run_rank([one], [metric], selections=[self.select(metric, job=True)])
        self.assertIn("one", accepted["rank_keys"])

    def test_duplicate_selection_or_record_is_not_silently_overwritten(self):
        record = self.record("one")
        first, second = self.metric(record, .1), self.metric(record, .2)
        with self.assertRaisesRegex(ValueError, "one metric"):
            self.run_rank([record], [first, second])
        with self.assertRaisesRegex(ValueError, "unique"):
            self.run_rank([record, deepcopy(record)], [first])

    def test_registered_metric_identity_is_rehashed_before_use(self):
        record = self.record("one")
        metric = self.metric(record)
        metric["value"] = .001
        result = self.run_rank([record], [metric])
        self.assertEqual(result["rank_keys"], {})
        self.assertIn("integrity", result["unresolved_candidates"][0]["reasons"][0])

    def test_changed_raw_output_or_structure_has_no_rank_key(self):
        for what in ("raw_output", "structure"):
            with self.subTest(what=what):
                record = self.record("record_" + what)
                metric = self.metric(record)
                path = record["path"] if what == "structure" else metric["raw_output"]["path"]
                Path(path).write_text("changed", encoding="utf-8")
                result = self.run_rank([record], [metric])
                self.assertEqual(result["rank_keys"], {})
                self.assertIn("checksum", result["unresolved_candidates"][0]["reasons"][0])

    def test_transformed_structure_evidence_cannot_replace_original(self):
        record = self.record("one")
        metric = self.metric(record)
        for modified in [{**record, "parent_version_id": "old"}, {**record, "transformation": "relaxed"}]:
            with self.assertRaisesRegex(ValueError, "original"):
                self.run_rank([modified], [metric])

    def test_target_bounds_precede_direction_without_claiming_hard_requirement_pass(self):
        records = [self.record(x) for x in ("low", "in_range", "high")]
        metrics = [self.metric(r, val, "dft_band_gap", "eV", reference_set=None)
                   for r, val in zip(records, (.5, 1.5, 3))]
        result = self.run_rank(records, metrics, self.objective("dft_band_gap", "eV", "maximize", target_min=1, target_max=2))
        keys = result["rank_keys"]
        self.assertEqual(sorted(keys, key=keys.get), ["in_range", "low", "high"])
        self.assertIn("not replaced", result["ranking_metadata"]["target_semantics"])

    def test_force_and_stress_are_readiness_not_stability(self):
        record = self.record("one")
        for prop, unit in [("max_force", "eV/angstrom"), ("max_stress", "GPa"), ("max_abs_stress", "GPa")]:
            with self.subTest(prop=prop):
                result = self.run_rank([record], [self.metric(record, .2, prop, unit, reference_set=None)], self.objective(prop, unit))
                self.assertEqual(result["ranking_metadata"]["interpretation"], "calculation_readiness_only_not_stability")
                invalid = self.run_rank([record], [self.metric(record, -.2, prop, unit, reference_set=None)], self.objective(prop, unit))
                self.assertEqual(invalid["rank_keys"], {})

    def test_nonfinite_or_ambiguous_objective_is_rejected(self):
        for changes in [{"target_min": True}, {"target_max": float("inf")}, {"target_min": 2, "target_max": 1},
                        {"direction": "best"}, {"property": "raw_energy"}, {"unit": "meV/atom"}, {"weights": [1]}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_objective({**self.objective(), **changes})
        self.assertEqual(validate_objective(self.objective(direction="min"))["direction"], "minimize")

    def test_overflowing_target_distance_is_review_not_nonfinite_pipeline_key(self):
        record = self.record("one")
        metric = self.metric(record, 1e308, "dft_band_gap", "eV", reference_set=None)
        result = self.run_rank([record], [metric], self.objective("dft_band_gap", "eV", target_max=-1e308))
        self.assertEqual(result["rank_keys"], {})
        self.assertIn("overflows", result["unresolved_candidates"][0]["reasons"][0])

    def test_single_usable_candidate_is_not_claimed_as_comparative_validation(self):
        one, two = self.record("one"), self.record("two")
        result = self.run_rank([one, two], [self.metric(one)])
        self.assertEqual(set(result["rank_keys"]), {"one"})
        self.assertFalse(result["ranking_metadata"]["comparability"]["comparable"])
        self.assertEqual(result["unresolved_candidates"][0]["candidate_id"], "two")

    def test_signed_fixed_reference_hull_distance_preserves_negative_evidence(self):
        one, two = self.record("one"), self.record("two", ("Si",))
        prop = "energy_above_reference_hull"
        result = self.run_rank([one, two], [self.metric(one, -.05, prop), self.metric(two, .08, prop)],
                               self.objective(prop))
        self.assertEqual(result["rank_keys"]["one"], (0, 0, -.05))
        self.assertLess(result["rank_keys"]["one"], result["rank_keys"]["two"])

    def test_signed_reference_hull_without_explicit_reference_is_review(self):
        record = self.record("one")
        prop = "energy_above_reference_hull"
        for reference in [None, *({"id": value, "sha256": "a" * 64} for value in
                                 ("unknown", "unverified", "not_measured", " Unknown "))]:
            with self.subTest(reference=reference):
                result = self.run_rank([record], [self.metric(record, -.05, prop, reference_set=reference)], self.objective(prop))
                self.assertEqual(result["rank_keys"], {})
                self.assertEqual(result["unresolved_candidates"][0]["status"], "review")
        # Blank identifiers cannot enter registered evidence at all.
        for value in ("", "  "):
            with self.subTest(reference=value), self.assertRaisesRegex(ValueError, "Reference-set identity"):
                self.metric(record, -.05, prop, reference_set={"id": value, "sha256": "a" * 64})


if __name__ == "__main__":
    unittest.main()
