import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pymatgen.core import Lattice, Structure
from pymatgen.io.cif import CifWriter

from matpilot.recommendations import recommend_candidates


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, candidate_id, crystal=None, suffix=""):
        if crystal is None:
            crystal = Structure(Lattice.cubic(5.64), ["Na", "Cl"], [[0, 0, 0], [0.5, 0.5, 0.5]])
        content = (str(CifWriter(crystal)) + suffix).encode("utf-8")
        path = self.root / f"{candidate_id}.cif"
        path.write_bytes(content)
        return {"candidate_id": candidate_id, "structure_version_id": "v-" + candidate_id,
                "path": str(path), "sha256": hashlib.sha256(content).hexdigest()}

    def test_rechecks_bytes_and_ignores_forged_summary_fields(self):
        valid = self.record("good")
        wrong = self.record("wrong", Structure(Lattice.cubic(5), ["Na"], [[0, 0, 0]]))
        wrong.update(elements=["Na", "Cl"], full_occupancy=True, composition_match=True)
        changed = self.record("changed")
        Path(changed["path"]).write_text("corrupt", encoding="utf-8")
        result = recommend_candidates([valid, wrong, changed], ["Na", "Cl"], 3)
        self.assertEqual(result["selected_candidate_ids"], ["good"])
        rows = {r["candidate_id"]: r for r in result["records"]}
        self.assertIn("element_set_mismatch", rows["wrong"]["exclusion_reasons"])
        self.assertIn("artifact_sha256_mismatch", rows["changed"]["exclusion_reasons"])
        self.assertFalse(result["target_met"])

    def test_partial_occupancy_and_periodic_self_contact_are_rejected(self):
        partial = Structure(Lattice.cubic(5), [{"Na": 0.5}, "Cl"], [[0, 0, 0], [0.5, 0.5, 0.5]])
        close = Structure(Lattice.orthorhombic(0.3, 5, 5), ["Na", "Cl"], [[0, 0, 0], [0.5, 0.5, 0.5]])
        result = recommend_candidates([self.record("partial", partial), self.record("self-image", close)], ["Na", "Cl"])
        self.assertEqual(result["selected_count"], 0)
        rows = {r["candidate_id"]: r for r in result["records"]}
        self.assertIn("partial_occupancy_or_disordered_sites", rows["partial"]["exclusion_reasons"])
        self.assertIn("periodic_contact_below_0_5_angstrom", rows["self-image"]["exclusion_reasons"])

    def test_duplicates_are_recomputed_and_stable_under_input_reordering(self):
        records = [self.record("b", suffix="\n# alternative encoding\n"), self.record("a")]
        records[0]["structural_equivalence_status"] = "distinct_within_checked_set"
        one = recommend_candidates(records, ["Na", "Cl"])
        two = recommend_candidates(records[::-1], ["Cl", "Na"])
        self.assertEqual(one, two)
        self.assertEqual(one["selected_candidate_ids"], ["a"])
        self.assertEqual(one["records"][1]["representative_id"], "a")
        self.assertNotEqual(records[0]["sha256"], records[1]["sha256"])

    def test_unknown_symmetry_is_excluded_not_labeled_p1(self):
        with patch("pymatgen.symmetry.analyzer.SpacegroupAnalyzer.get_space_group_number", side_effect=ValueError("unavailable")):
            result = recommend_candidates([self.record("a")], ["Na", "Cl"])
        self.assertEqual(result["selected_count"], 0)
        self.assertIn("space_group_unavailable", result["records"][0]["exclusion_reasons"])
        self.assertNotIn("space_group_number", result["records"][0])

    def test_unknown_equivalence_is_excluded(self):
        with patch("pymatgen.analysis.structure_matcher.StructureMatcher.fit", side_effect=ValueError("unknown")):
            result = recommend_candidates([self.record("a"), self.record("b")], ["Na", "Cl"])
        self.assertEqual(result["selected_candidate_ids"], ["a"])
        self.assertIn("structural_equivalence_unknown", result["records"][1]["exclusion_reasons"])

    def test_explicit_priority_filter_does_not_invent_a_scientific_label(self):
        records = [self.record("a"), self.record("b")]
        with self.assertRaises(ValueError):
            recommend_candidates(records, ["Na", "Cl"], priority_candidate_ids=["b"])
        with self.assertRaises(ValueError):
            recommend_candidates(records, ["Na", "Cl"], priority_candidate_ids=["missing"], priority_selection_basis="operator choice")
        result = recommend_candidates(records, ["Na", "Cl"], priority_candidate_ids=["b"], priority_selection_basis="operator choice")
        self.assertEqual(result["selected_candidate_ids"], ["b"])
        self.assertEqual(result["priority_selector"]["evidence_scope"], "explicit_caller_preference_not_property_validation")
        self.assertNotIn("priority", result["records"][1])

    def test_duplicate_identity_and_failed_hard_requirements_are_not_eligible(self):
        one = self.record("a")
        failed = self.record("b")
        failed["all_hard_requirements_pass"] = False
        result = recommend_candidates([one, dict(one), failed], ["Na", "Cl"])
        self.assertEqual(result["selected_count"], 0)
        self.assertIn("ambiguous_duplicate_candidate_id", result["records"][0]["exclusion_reasons"])
        self.assertIn("existing_hard_requirement_checks_not_passed", result["records"][2]["exclusion_reasons"])

    def test_category_coverage_uses_discrete_groups_and_consistent_ties(self):
        # Known feature rows isolate the policy from symmetry identification;
        # real CIF reanalysis and matching are tested separately above.
        features = [("a", "NaCl", 1, 2), ("b", "NaCl", 2, 3),
                    ("c", "NaCl2", 230, 12), ("d", "NaCl3", 1, 24)]
        rows = [{"candidate_id": cid, "path": str(self.root / (cid + ".cif"))} for cid, *_ in features]
        def inspect(record, expected, symprec, angle_tolerance):
            cid, formula, group, count = next(x for x in features if x[0] == record["candidate_id"])
            return {"candidate_id": cid, "qualified": False, "selected": False, "exclusion_reasons": [],
                    "reduced_formula": formula, "space_group_number": group, "site_count": count,
                    "size_bin": "1-10" if count <= 10 else "11-20" if count <= 20 else "21+"}, object()
        with patch("matpilot.recommendations._inspect", side_effect=inspect), \
                patch("pymatgen.analysis.structure_matcher.StructureMatcher.fit", return_value=False):
            result = recommend_candidates(rows, ["Na", "Cl"], 3)
        self.assertEqual(result["selected_candidate_ids"], ["a", "c", "d"])
        self.assertEqual(result["coverage"]["size_bin"]["selected_count"], 3)
        self.assertEqual(result["policy"]["category_weights"], {"reduced_formula": 1, "space_group_number": 1, "size_bin": 1})
        self.assertEqual(result["records"][1]["not_selected_reason"], "requested_shortlist_limit_reached_under_fixed_coverage_policy")


if __name__ == "__main__":
    unittest.main()
