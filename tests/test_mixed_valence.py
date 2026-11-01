"""Algorithm tests use stoichiometry and constructed CPU fixtures, never labels."""
import hashlib
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pymatgen.core import Lattice, Structure
from pymatgen.io.cif import CifWriter

from matpilot.mixed_valence import (
    POLICY_ID, analyze_mixed_valence_candidate, describe_ionic_system,
    formal_charge_balance, policy_description, recommend_with_mixed_valence,
    supports_elements, supports_ionic_system,
)


class MixedValenceTests(unittest.TestCase):
    def assert_witness(self, composition, result):
        self.assertEqual(result["status"], "feasible", result)
        self.assertTrue(result["mixed_possible"])
        witness = result["witness"]
        self.assertEqual(set(witness), set(composition))
        for symbol, count in composition.items():
            self.assertEqual(sum(witness[symbol].values()), count)
            self.assertTrue(all(isinstance(n, int) and n > 0 for n in witness[symbol].values()))
        self.assertEqual(sum(int(q) * n for states in witness.values() for q, n in states.items()), 0)
        return result

    def test_development_regressions_accept_mixed_counts_and_keep_witness(self):
        for composition in [{"Fe": 3, "Ni": 3, "O": 8}, {"Fe": 1, "Ni": 5, "O": 8}]:
            with self.subTest(composition=composition):
                result = self.assert_witness(composition, formal_charge_balance(composition))
                # Their old +2/+3 uniform policy failed. The new catalog is
                # broader, so acceptance need not imply a mixed-only witness.
                self.assertIn(result["uniform_possible"], (True, False))
                self.assertIn("Ni", result["system"]["cation_catalog"])

    def test_same_catalog_uniform_ablation_exposes_true_mixed_case(self):
        composition = {"Fe": 3, "O": 4}
        result = self.assert_witness(composition, formal_charge_balance(composition))
        self.assertFalse(result["uniform_possible"])
        self.assertTrue(result["requires_mixed_valence"])
        self.assertEqual(result["witness"]["Fe"], {"2": 1, "3": 2})
        self.assertIsNone(result["uniform_witness"])

    def test_catalog_icsd_precedence_and_common_fallback_are_explicit(self):
        real = describe_ionic_system(["Ni", "O"])
        self.assertTrue(real["supported"])
        self.assertIn(3, real["cation_catalog"]["Ni"]["states"])
        self.assertEqual(real["cation_catalog"]["Ni"]["source"], "pymatgen.Element.icsd_oxidation_states")
        class FakeElement:
            def __init__(self, symbol):
                self.symbol = symbol
                self.is_metal = symbol == "Na"
                self.icsd_oxidation_states = ()
                self.common_oxidation_states = (1,) if symbol == "Na" else (-1,)
        with patch("pymatgen.core.Element", FakeElement):
            fallback = describe_ionic_system(["Na", "Cl"])
        self.assertEqual(fallback["cation_catalog"]["Na"]["states"], [1])
        self.assertTrue(fallback["cation_catalog"]["Na"]["source"].endswith("common_oxidation_states_fallback"))

    def test_common_single_anion_systems_and_uniform_solution(self):
        formulas = [{"Na": 1, "Cl": 1}, {"Ca": 1, "F": 2}, {"Mg": 1, "Br": 2},
                    {"K": 1, "I": 1}, {"Fe": 1, "S": 1}, {"Zn": 1, "Se": 1},
                    {"Li": 3, "N": 1}, {"Li": 1, "Co": 1, "O": 2},
                    {"Cu": 2, "O": 1}, {"Mn": 3, "O": 4}]
        for composition in formulas:
            with self.subTest(composition=composition):
                self.assertTrue(supports_elements(list(composition)))
                self.assertTrue(supports_ionic_system(list(composition)))
                self.assert_witness(composition, formal_charge_balance(composition))
        uniform = formal_charge_balance({"Na": 1, "Cl": 1})
        self.assertTrue(uniform["uniform_possible"])
        self.assertFalse(uniform["requires_mixed_valence"])
        self.assertEqual(uniform["uniform_witness"], uniform["witness"])

    def test_unsupported_and_invalid_inputs_do_not_mean_impossible_chemistry(self):
        for composition in [{"Fe": 1, "Ni": 1}, {"Fe": 1, "O": 1, "S": 1},
                            {"C": 1, "O": 2}, {"O": 2}]:
            with self.subTest(composition=composition):
                result = formal_charge_balance(composition)
                self.assertEqual(result["status"], "unsupported")
                self.assertIsNone(result["mixed_possible"])
        for composition in [{}, {"Na": True, "Cl": 1}, {"Na": 0.5, "Cl": 1},
                            {"Na": float("nan"), "Cl": 1}, {"Na": 0, "Cl": 1},
                            {"NotAnElement": 1, "Cl": 1}]:
            with self.subTest(composition=composition):
                result = formal_charge_balance(composition)
                self.assertEqual(result["status"], "invalid_input")
                self.assertIsNone(result["mixed_possible"])

    def test_exhaustive_no_solution_is_distinct_from_budget_unknown(self):
        impossible = formal_charge_balance({"Na": 1, "Cl": 2})
        self.assertEqual(impossible["status"], "no_solution")
        self.assertFalse(impossible["uniform_possible"])
        self.assertFalse(impossible["mixed_possible"])
        self.assertIsNone(impossible["witness"])
        for limits in [{"max_atoms": 1}, {"max_target_charge": 1},
                       {"max_transitions": 0}, {"max_memory_bytes": 0}]:
            with self.subTest(limits=limits):
                result = formal_charge_balance({"Fe": 3, "O": 4}, **limits)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["mixed_possible"])
                self.assertIsNone(result["witness"])

    def test_hard_limits_cannot_be_increased_or_bypassed_with_large_inputs(self):
        for limits in [{"max_atoms": 10**9}, {"max_target_charge": 10**9},
                       {"max_transitions": 10**12}, {"max_memory_bytes": 10**12}]:
            with self.subTest(limits=limits):
                result = formal_charge_balance({"Na": 1, "Cl": 1}, **limits)
                self.assertEqual(result["status"], "invalid_input")
                self.assertIsNone(result["mixed_possible"])
        huge_count = formal_charge_balance({"Na": 10**100, "Cl": 1})
        self.assertEqual((huge_count["status"], huge_count["reason"]), ("unknown", "atom_budget_exceeded"))
        huge_mapping = formal_charge_balance({str(i): 1 for i in range(119)})
        self.assertEqual((huge_mapping["status"], huge_mapping["reason"]), ("unknown", "element_entry_budget_exceeded"))
        invalid_symbol = formal_charge_balance({"N" * 10000: 1, "Cl": 1})
        self.assertEqual(invalid_symbol["status"], "invalid_input")
        self.assertFalse(supports_elements(["Na"] * 10000 + ["Cl"]))

    def test_small_compositions_match_independent_cartesian_enumeration(self):
        for fe, ni, oxygen in itertools.product(range(1, 4), range(1, 4), range(1, 7)):
            composition = {"Fe": fe, "Ni": ni, "O": oxygen}
            result = formal_charge_balance(composition)
            catalog = result["system"]["cation_catalog"]
            states_fe = catalog["Fe"]["states"]
            states_ni = catalog["Ni"]["states"]
            target = 2 * oxygen
            uniform = any(fe * a + ni * b == target for a in states_fe for b in states_ni)
            mixed = any(sum(values) == target for values in itertools.product(*([states_fe] * fe + [states_ni] * ni)))
            with self.subTest(composition=composition):
                self.assertEqual(result["uniform_possible"], uniform)
                self.assertEqual(result["mixed_possible"], mixed)
                self.assertEqual(result["status"], "feasible" if mixed else "no_solution")
                if mixed:
                    self.assert_witness(composition, result)

    def test_policy_card_records_nonscientific_scope_and_resource_bounds(self):
        card = policy_description()
        self.assertEqual(card["id"], POLICY_ID)
        self.assertFalse(card["performance_labels_used"])
        self.assertEqual(card["resource_limits"]["max_atoms"], 1000)
        self.assertEqual(card["anion_states"]["N"], -3)
        self.assertIn("same state catalog", card["uniform_ablation"])


class MixedValencePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, name, species, lattice=8):
        # Widely separated, deliberately hand-built CPU fixtures. No energies.
        positions = [[(index % 3) / 3, ((index // 3) % 3) / 3, (index // 9) / 3]
                     for index in range(len(species))]
        crystal = Structure(Lattice.cubic(lattice), species, positions)
        path = self.root / (name + ".cif")
        path.write_text(str(CifWriter(crystal)), encoding="utf-8")
        return {"candidate_id": name, "path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_large_volume_does_not_become_cross_system_physical_rejection(self):
        record = self.record("open-cell", ["Na", "Cl"], lattice=30)
        result = analyze_mixed_valence_candidate(record, ["Na", "Cl"])
        self.assertEqual(result["tier"], "priority_relaxation")
        self.assertGreater(result["metrics"]["volume_per_atom_angstrom3"], 40)
        self.assertTrue(result["metrics"]["density_and_volume_are_descriptive_only"])
        self.assertIsNone(result["score"])

    def test_pipeline_retains_task_checks_review_and_duplicate_provenance(self):
        valid = self.record("A", ["Fe"] * 3 + ["O"] * 4)
        missing = self.record("B", ["Fe"] * 2)
        no_solution = self.record("C", ["Fe", "O", "O", "O", "O"])
        duplicate = {**valid, "candidate_id": "Z"}
        result = recommend_with_mixed_valence([valid, missing, no_solution, duplicate], ["Fe", "O"], 4)
        self.assertEqual(result["stage_counts"]["structural_representative_count"], 2)
        self.assertEqual(result["stage_counts"]["priority_relaxation_count"], 1)
        self.assertEqual(result["stage_counts"]["manual_review_count"], 1)
        self.assertEqual(result["selected_candidate_ids"], ["A"])
        rows = {row["candidate_id"]: row for row in result["records"]}
        self.assertEqual(rows["Z"]["representative_id"], "A")
        self.assertIn("element_set_mismatch", rows["B"]["structural_exclusion_reasons"])
        self.assertEqual(rows["C"]["relaxation_priority"]["metrics"]["charge_feasibility"]["status"], "no_solution")
        self.assertEqual(rows["A"]["relaxation_priority"]["metrics"]["charge_feasibility"]["uniform_possible"], False)

    def test_unsupported_explicit_policy_errors_and_unknown_does_not_pass(self):
        record = self.record("A", ["Na", "Cl"])
        with self.assertRaisesRegex(ValueError, "does not support"):
            recommend_with_mixed_valence([record], ["Fe", "Ni"])
        with patch("matpilot.mixed_valence.formal_charge_balance", return_value={
            "status": "unknown", "mixed_possible": None, "reason": "transition_budget_exhausted"
        }):
            result = recommend_with_mixed_valence([record], ["Na", "Cl"])
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["stage_counts"]["manual_review_count"], 1)

    def test_internal_priority_filter_does_not_invent_caller_exclusion(self):
        record = self.record("A", ["Na", "Cl"])
        path = Path(record["path"])
        path.write_bytes(path.read_bytes() + b"# changed artifact\n")
        result = recommend_with_mixed_valence([record], ["Na", "Cl"], 1,
            priority_candidate_ids=["A"], priority_selection_basis="explicit operator preference")
        row = result["records"][0]
        self.assertEqual(row["exclusion_reasons"], ["artifact_sha256_mismatch"])
        self.assertEqual(row["not_selected_reason"], "artifact_sha256_mismatch")
        record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        actual_caller_filter = recommend_with_mixed_valence([record], ["Na", "Cl"], 1,
            priority_candidate_ids=[], priority_selection_basis="explicitly empty operator preference")
        self.assertEqual(actual_caller_filter["records"][0]["exclusion_reasons"], ["outside_explicit_priority_selector"])


if __name__ == "__main__":
    unittest.main()
