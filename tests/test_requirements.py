import tempfile
import unittest
from pathlib import Path

from matpilot.config import Settings
from matpilot.service import Service
from matpilot.requirements import contract


def requirement(raw, quote, kind, prop, value, *, rid="r1", strength="hard", relation="equal", unit=None):
    start = raw.index(quote)
    result = {"requirement_id": rid, "source": {"start": start, "end": start + len(quote), "quote": quote},
              "kind": kind, "property": prop, "value": value, "strength": strength, "relation": relation}
    if unit is not None:
        result["unit"] = unit
    return result


class RequirementTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.service = Service(Settings(data_dir=Path(self.temp.name)))

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, raw, requirements, **kwargs):
        return self.service.prepare_plan({"raw_request": raw, "requirements": requirements, **kwargs})

    def test_public_example_is_executable_with_anchored_mappings(self):
        p = self.service.prepare_plan(self.service.get_capabilities()["request_example"])
        self.assertEqual(p["state"], "ready", p["issues"])
        self.assertEqual(len(p["requirement_mappings"][0]["routes"]), 2)
        self.assertIn("not_verified", p["coverage"]["semantic_completeness"])

    def test_raw_text_cannot_bypass_ledger(self):
        p = self.service.prepare_plan({"raw_request": "Fe-Ni-O，最多4个", "conditions": {"chemical_system": "Fe-Ni-O"}, "num_samples": 4})
        self.assertNotEqual(p["state"], "ready")
        self.assertTrue(p["coverage"]["uncovered_signals"])

    def test_no_prose_structured_entry_does_not_invent_nlp_evidence(self):
        p = self.service.prepare_plan({"conditions": {"chemical_system": "Fe-Ni-O"}})
        self.assertEqual(p["state"], "ready", p["issues"])
        self.assertEqual(p["coverage"]["scope"], "structured_input")
        self.assertTrue(all(r["source"] is None for r in p["requirements"]))

    def test_fabricated_source_or_duplicate_ids_block(self):
        raw = "Fe-Ni-O"
        r = requirement(raw, raw, "element_set", "chemical_system", raw)
        for proposed in [[dict(r, source={"start": 0, "end": 7, "quote": "Ni-Fe-O"})], [r, r]]:
            p = self.plan(raw, proposed, conditions={"chemical_system": raw})
            self.assertEqual(p["state"], "blocked")

    def test_source_offsets_are_unicode_codepoints(self):
        raw = "🧪Fe-Ni-O"
        r = requirement(raw, "Fe-Ni-O", "element_set", "chemical_system", "Fe-Ni-O")
        self.assertEqual(r["source"]["start"], 1)
        p = self.plan(raw, [r], conditions={"chemical_system": "Fe-Ni-O"})
        self.assertEqual(p["state"], "ready", p["issues"])

    def test_omitted_budget_and_goal_are_detected(self):
        raw = "Fe-Ni-O，最多4个，研究析氧活性"
        r = requirement(raw, "Fe-Ni-O", "element_set", "chemical_system", "Fe-Ni-O")
        p = self.plan(raw, [r], conditions={"chemical_system": "Fe-Ni-O"}, num_samples=4)
        self.assertNotEqual(p["state"], "ready")
        self.assertTrue({"number", "research_goal"} <= {s["kind"] for s in p["coverage"]["uncovered_signals"]})

    def test_budget_cannot_be_expanded(self):
        example = contract()["example"]
        p = self.plan(example["raw_request"], example["requirements"], conditions={"chemical_system": "Fe-Ni-O"}, num_samples=8)
        self.assertEqual(p["state"], "blocked")
        self.assertTrue(any(i.get("requirement_id") == "r2" for i in p["issues"]))

    def test_unit_conversion_retains_original_proposal(self):
        raw = "带隙目标1200 meV"
        r = requirement(raw, "1200 meV", "numeric_target", "dft_band_gap", 1200, unit="meV", relation="target", strength="goal")
        p = self.plan(raw, [r], conditions={"dft_band_gap": {"value": 1200, "unit": "meV"}})
        self.assertEqual(p["state"], "ready", p["issues"])
        self.assertEqual(p["requirements"][0]["value"], 1200)
        self.assertEqual(p["requirements"][0]["normalized_value"], 1.2)
        self.assertEqual(p["requirements"][0]["normalized_unit"], "eV")

    def test_numeric_bound_cannot_become_target(self):
        raw = "带隙至少2 eV"
        r = requirement(raw, "至少2 eV", "numeric_bound", "dft_band_gap", 2, relation="at_least", unit="eV")
        p = self.plan(raw, [r], conditions={"dft_band_gap": 2})
        self.assertEqual(p["state"], "blocked")
        self.assertTrue(any(i["code"] == "unverified_hard_requirement" for i in p["issues"]))

    def test_llm_cannot_hide_bound_word_by_shortening_source_span(self):
        raw = "带隙至少2 eV"
        for quote in ("至少2 eV", "2 eV"):
            r = requirement(raw, quote, "numeric_target", "dft_band_gap", 2, relation="target", unit="eV", strength="goal")
            p = self.plan(raw, [r], conditions={"dft_band_gap": 2})
            self.assertNotEqual(p["state"], "ready")

    def test_llm_unit_relabeling_is_rejected_before_conversion(self):
        raw = "带隙目标1200 meV"
        r = requirement(raw, "1200 meV", "numeric_target", "dft_band_gap", 1200, relation="target", unit="eV", strength="goal")
        p = self.plan(raw, [r], conditions={"dft_band_gap": 1200})
        self.assertEqual(p["state"], "blocked")

    def test_explicit_hard_formula_cannot_become_soft_goal(self):
        raw = "必须精确NaCl2"
        r = requirement(raw, raw, "stoichiometry", "chemical_formula", "NaCl2", strength="goal")
        p = self.plan(raw, [r], unconditional=True, search_mode="generate_then_filter")
        self.assertEqual(p["state"], "blocked")

    def test_unsupported_goal_stays_unknown_without_optional_clarification(self):
        raw = "Fe-Ni-O，研究析氧活性"
        r1 = requirement(raw, "Fe-Ni-O", "element_set", "chemical_system", "Fe-Ni-O")
        r2 = requirement(raw, "析氧活性", "research_goal", "oer_activity", "析氧活性", rid="r2", relation="goal", strength="goal")
        p = self.plan(raw, [r1, r2], conditions={"chemical_system": "Fe-Ni-O"})
        self.assertEqual(p["state"], "ready", p["issues"])
        self.assertEqual(p["review_targets"], ["析氧活性"])
        self.assertEqual(p["requirement_mappings"][1]["routes"][0]["status"], "unavailable")

    def test_hard_research_claim_cannot_be_unverified(self):
        raw = "Fe-Ni-O，必须耐氯腐蚀"
        r1 = requirement(raw, "Fe-Ni-O", "element_set", "chemical_system", "Fe-Ni-O")
        r2 = requirement(raw, "必须耐氯腐蚀", "research_goal", "corrosion", "耐氯腐蚀", rid="r2", relation="goal")
        p = self.plan(raw, [r1, r2], conditions={"chemical_system": "Fe-Ni-O"})
        self.assertEqual(p["state"], "blocked")

    def test_fixed_formula_needs_explicit_search_and_keeps_output_check(self):
        raw = "精确LiFePO4"
        r = requirement(raw, "LiFePO4", "stoichiometry", "chemical_formula", "LiFePO4")
        p = self.plan(raw, [r], unconditional=True)
        self.assertNotEqual(p["state"], "ready")
        p = self.plan(raw, [r], unconditional=True, search_mode="generate_then_filter")
        self.assertEqual(p["state"], "ready", p["issues"])
        self.assertEqual(p["requirement_mappings"][0]["routes"][0]["checker"], "reduced_stoichiometry")

    def test_extra_condition_and_fabricated_number_are_rejected(self):
        raw = "Fe-Ni-O"
        r = requirement(raw, raw, "element_set", "chemical_system", raw)
        p = self.plan(raw, [r], conditions={"chemical_system": raw, "energy_above_hull": 0.1})
        self.assertEqual(p["state"], "blocked")
        fake = requirement(raw, raw, "numeric_target", "dft_band_gap", 2, relation="target", strength="goal", unit="eV")
        p = self.plan(raw, [fake], conditions={"dft_band_gap": 2})
        self.assertEqual(p["state"], "blocked")

    def test_sampling_cap_and_valid_target_are_separate(self):
        p = self.service.prepare_plan({"unconditional": True, "num_samples": 16, "valid_candidates_target": 7})
        self.assertEqual(p["state"], "ready", p["issues"])
        self.assertEqual(p["sampling_budget"], {"max_samples": 16, "valid_candidates_target": 7, "target_guaranteed": False})
        p = self.service.prepare_plan({"unconditional": True, "num_samples": 4, "valid_candidates_target": 7})
        self.assertEqual(p["state"], "blocked")

    def test_quote_only_is_resolved_to_exact_codepoint_span(self):
        raw = "🧪 请探索 Fe-Ni-O"
        r = requirement(raw, "Fe-Ni-O", "element_set", "chemical_system", "Fe-Ni-O")
        r["source"] = {"quote": "Fe-Ni-O"}
        p = self.plan(raw, [r], conditions={"chemical_system": "Fe-Ni-O"})
        self.assertEqual(p["state"], "ready", p["issues"])
        self.assertEqual(p["requirements"][0]["source"], {"start": 6, "end": 13, "quote": "Fe-Ni-O"})
        self.assertEqual(p["requirements"][0]["source_resolution"], "unique_exact_quote")
        self.assertEqual(r["source"], {"quote": "Fe-Ni-O"})

    def test_ambiguous_quote_needs_disambiguation(self):
        raw = "Fe-Ni-O；仍然是Fe-Ni-O"
        r = requirement(raw, "Fe-Ni-O", "element_set", "chemical_system", "Fe-Ni-O")
        r["source"] = {"quote": "Fe-Ni-O"}
        p = self.plan(raw, [r], conditions={"chemical_system": "Fe-Ni-O"})
        self.assertEqual(p["state"], "blocked")
        self.assertIn("repeated", p["issues"][0]["message"])
        r["source"] = {"quote": raw}
        self.assertEqual(self.plan(raw, [r], conditions={"chemical_system": "Fe-Ni-O"})["state"], "ready")

    def test_quote_only_never_repairs_missing_text_or_supplied_bad_offsets(self):
        raw = "研究Fe-Ni-O"
        r = requirement(raw, "Fe-Ni-O", "element_set", "chemical_system", "Fe-Ni-O")
        for source in ({"quote": "Fe-Ni"}, {"quote": "Ni-Fe-O"}, {"quote": ""}, {"quote": "Fe-Ni-O", "start": 0, "end": 7}, {"quote": "Fe-Ni-O", "start": 2}):
            p = self.plan(raw, [dict(r, source=source)], conditions={"chemical_system": "Fe-Ni-O"})
            self.assertEqual(p["state"], "blocked", source)

    def test_quote_only_still_rejects_unit_and_budget_changes(self):
        raw = "带隙目标1200 meV，最多2次"
        r1 = requirement(raw, "1200 meV", "numeric_target", "dft_band_gap", 1200, unit="meV", relation="target", strength="goal")
        r2 = requirement(raw, "最多2次", "sample_budget", "max_samples", 2, rid="r2", relation="at_most")
        for r in (r1, r2):
            r["source"] = {"quote": r["source"]["quote"]}
        kw = {"conditions": {"dft_band_gap": {"value": 1200, "unit": "meV"}}, "num_samples": 2}
        p = self.plan(raw, [r1, r2], **kw)
        self.assertEqual(p["state"], "ready", p["issues"])
        self.assertEqual(p["requirements"][0]["normalized_value"], 1.2)
        self.assertEqual(self.plan(raw, [dict(r1, unit="eV"), r2], **kw)["state"], "blocked")
        kw["num_samples"] = 4
        self.assertEqual(self.plan(raw, [r1, r2], **kw)["state"], "blocked")
