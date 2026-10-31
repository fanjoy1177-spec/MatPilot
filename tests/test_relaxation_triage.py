import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pymatgen.core import Lattice, Structure
from pymatgen.io.cif import CifWriter

from matpilot.relaxation_triage import (
    analyze_relaxation_candidate, classify_relaxation_metrics,
    recommend_for_relaxation, uniform_oxidation_balance_possible,
)


class RelaxationTriageTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, candidate_id, crystal=None, transform=None):
        crystal = crystal or Structure(Lattice.cubic(5), ["Fe", "Ni", "O", "O"],
            [[0, 0, 0], [0.5, 0.5, 0], [0.5, 0, 0.5], [0, 0.5, 0.5]])
        content = str(CifWriter(crystal))
        if transform:
            content = transform(content)
        data = content.encode("utf-8")
        path = self.root / (candidate_id + ".cif")
        path.write_bytes(data)
        return {"candidate_id": candidate_id, "path": str(path), "sha256": hashlib.sha256(data).hexdigest()}

    def classify(self, **overrides):
        values = dict(minimum_distance_angstrom=2, minimum_covalent_ratio=1,
                      volume_per_atom_angstrom3=15, density_g_cm3=5,
                      uniform_charge_balance_possible=True)
        values.update(overrides)
        return classify_relaxation_metrics(**values)

    def test_frozen_boundary_rules_and_exclusive_short_contact_penalty(self):
        self.assertEqual(self.classify(minimum_distance_angstrom=0.8, minimum_covalent_ratio=0.65,
                                      volume_per_atom_angstrom3=5, density_g_cm3=1)["score"], 100)
        self.assertEqual(self.classify(volume_per_atom_angstrom3=40, density_g_cm3=12)["score"], 100)
        severe = self.classify(minimum_distance_angstrom=0.799, minimum_covalent_ratio=0.2)
        self.assertEqual((severe["score"], severe["tier"]), (50, "defer"))
        suspect = self.classify(minimum_covalent_ratio=0.649)
        self.assertEqual((suspect["score"], suspect["tier"]), (75, "manual_review"))
        self.assertEqual(self.classify(volume_per_atom_angstrom3=40.001)["score"], 80)
        self.assertEqual(self.classify(density_g_cm3=12.001)["tier"], "priority_relaxation")
        self.assertEqual(self.classify(minimum_covalent_ratio=0.6, density_g_cm3=12.001)["score"], 60)
        self.assertEqual(self.classify(minimum_covalent_ratio=0.6, density_g_cm3=12.001)["tier"], "manual_review")

    def test_failed_uniform_charge_check_is_review_even_at_score_85_and_is_not_mixed_valence_test(self):
        self.assertTrue(uniform_oxidation_balance_possible({"Fe": 1, "Ni": 1, "O": 2}))
        # Fe2Ni2O5 balances uniform Fe3+/Ni2+. Fe3Ni3O7 can balance
        # mixed +2/+3 sites (7 + 7 = 14), but not uniform states per element.
        self.assertTrue(uniform_oxidation_balance_possible({"Fe": 2, "Ni": 2, "O": 5}))
        self.assertFalse(uniform_oxidation_balance_possible({"Fe": 3, "Ni": 3, "O": 7}))
        result = self.classify(uniform_charge_balance_possible=False)
        self.assertEqual((result["score"], result["tier"]), (85, "manual_review"))
        self.assertIn("mixed_valence_not_tested", result["issues"][0])

    def test_unknown_metrics_fail_closed_and_other_systems_are_explicitly_unsupported(self):
        for value in (None, float("nan"), float("inf"), 0, True):
            with self.subTest(value=value):
                result = self.classify(minimum_distance_angstrom=value)
                self.assertEqual(result["tier"], "manual_review")
                self.assertIsNone(result["score"])
                self.assertFalse(result["eligible_for_relaxation_shortlist"])
        self.assertFalse(self.classify(uniform_charge_balance_possible=None)["eligible_for_relaxation_shortlist"])
        with self.assertRaisesRegex(ValueError, "supports exactly Fe, Ni and O"):
            recommend_for_relaxation([self.record("a")], ["Fe", "O"])

    def test_periodic_self_image_is_included_in_short_contact_triage(self):
        crystal = Structure(Lattice.orthorhombic(0.7, 8, 8), ["Fe", "Ni", "O", "O"],
            [[0, 0, 0], [0, 0.5, 0], [0, 0, 0.5], [0, 0.5, 0.5]])
        result = analyze_relaxation_candidate(self.record("self-image", crystal), ["Fe", "Ni", "O"])
        self.assertAlmostEqual(result["metrics"]["minimum_distance_angstrom"], 0.7)
        self.assertIn("severely_short_periodic_contact", result["issues"])
        self.assertEqual(result["tier"], "defer")

    def test_missing_occupancy_and_changed_bytes_never_enter_priority_pool(self):
        def without_occupancy(text):
            lines = text.splitlines()
            position = next(i for i, line in enumerate(lines) if line.strip() == "_atom_site_occupancy")
            lines.pop(position)
            for i in range(position, len(lines)):
                if lines[i].strip() and not lines[i].lstrip().startswith(("#", "_", "loop_", "data_")):
                    values = lines[i].split()
                    if len(values) >= 7:
                        lines[i] = "  " + "  ".join(values[:-1])
            return "\n".join(lines) + "\n"
        missing = self.record("missing", transform=without_occupancy)
        changed = self.record("changed")
        Path(changed["path"]).write_text("changed", encoding="utf-8")
        result = recommend_for_relaxation([missing, changed], ["Fe", "Ni", "O"], 2)
        self.assertEqual(result["selected_count"], 0)
        rows = {r["candidate_id"]: r for r in result["records"]}
        self.assertIn("explicit_full_occupancy_not_available", rows["missing"]["structural_exclusion_reasons"])
        self.assertIn("artifact_sha256_mismatch", rows["changed"]["structural_exclusion_reasons"])

    def test_full_pipeline_deduplicates_before_triage_keeps_audit_and_reports_shortfall(self):
        good = self.record("a")
        duplicate = self.record("b")
        mismatch = self.record("wrong", Structure(Lattice.cubic(5), ["Ni", "O"], [[0, 0, 0], [0.5, 0.5, 0.5]]))
        result = recommend_for_relaxation([duplicate, mismatch, good], ["O", "Ni", "Fe"], 4)
        self.assertEqual(result["selected_candidate_ids"], ["a"])
        self.assertFalse(result["target_met"])
        self.assertEqual(result["stage_counts"]["shortfall_count"], 3)
        self.assertEqual(result["stage_counts"]["structural_representative_count"], 1)
        self.assertEqual(result["stage_counts"]["priority_relaxation_count"], 1)
        rows = {r["candidate_id"]: r for r in result["records"]}
        self.assertEqual(rows["b"]["representative_id"], "a")
        self.assertIn("equivalent_to_qualified_representative", rows["b"]["structural_exclusion_reasons"])
        self.assertEqual(rows["a"]["relaxation_priority"]["priority"], "优先弛豫")

    def test_analysis_failure_and_external_priority_preference_cannot_force_a_recommendation(self):
        record = self.record("a")
        with patch("matpilot.relaxation_triage._periodic_contact_metrics", side_effect=ValueError("no contacts")):
            result = recommend_for_relaxation([record], ["Fe", "Ni", "O"], 1,
                priority_candidate_ids=["a"], priority_selection_basis="operator asks to review a")
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["stage_counts"]["manual_review_count"], 1)
        self.assertTrue(result["records"][0]["structural_representative_qualified"])
        self.assertEqual(result["records"][0]["relaxation_priority"]["tier"], "manual_review")
        self.assertIsNone(result["records"][0]["relaxation_priority"]["score"])
        self.assertEqual(result["caller_priority_selector"]["candidate_ids"], ["a"])


if __name__ == "__main__":
    unittest.main()
