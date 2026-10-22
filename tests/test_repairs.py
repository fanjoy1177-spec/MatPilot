"""Reviewable finite repairs; no generation, API calls or implicit approval."""
import copy
from pathlib import Path
import tempfile
import unittest

from matpilot.config import Settings
from matpilot.service import Service
from matpilot.repairs import repair_options


class RepairOptionsTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".work/test-temp"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.service = Service(Settings(data_dir=Path(self.temp.name)))

    def tearDown(self):
        self.temp.cleanup()

    def structural_options(self, plan):
        return [o for o in plan["repair_options"] if o["kind"] == "move_structural_condition_to_check"]

    def test_numeric_target_retained_and_structural_requirement_remains_a_hard_check(self):
        request = {"conditions": {"chemical_system": "Fe-Ni-O", "dft_band_gap": {"value": 1200, "unit": "meV"}},
                   "num_samples": 4, "batch_size": 2, "seed": 7, "valid_candidates_target": 1,
                   "raw_request": "Fe-Ni-O，带隙引导1200 meV，最多4个，每批2个，种子7，合格候选目标1个",
                   "requirements": [
                       {"requirement_id": "elements", "source": {"quote": "Fe-Ni-O"}, "kind": "element_set", "property": "chemical_system", "value": "Fe-Ni-O", "strength": "hard", "relation": "equal"},
                       {"requirement_id": "gap", "source": {"quote": "带隙引导1200 meV"}, "kind": "numeric_target", "property": "dft_band_gap", "value": 1200, "unit": "meV", "strength": "goal", "relation": "target"},
                       {"requirement_id": "budget", "source": {"quote": "最多4个"}, "kind": "sample_budget", "property": "max_samples", "value": 4, "strength": "hard", "relation": "at_most"},
                       {"requirement_id": "batch", "source": {"quote": "每批2个"}, "kind": "execution_parameter", "property": "batch_size", "value": 2, "strength": "hard", "relation": "equal"},
                       {"requirement_id": "seed", "source": {"quote": "种子7"}, "kind": "execution_parameter", "property": "seed", "value": 7, "strength": "hard", "relation": "equal"},
                       {"requirement_id": "valid", "source": {"quote": "合格候选目标1个"}, "kind": "sample_budget", "property": "valid_candidates_target", "value": 1, "strength": "hard", "relation": "at_least"},
                   ]}
        plan = self.service.prepare_plan(request)
        before = copy.deepcopy(plan)
        options = self.structural_options(plan)
        self.assertEqual(len(options), 1)
        option = options[0]
        target = option["execution_changes"]["after"]
        self.assertEqual(target["checkpoint"], "dft_band_gap")
        self.assertEqual(target["conditions"], {"dft_band_gap": 1.2})
        self.assertEqual((target["num_samples"], target["batch_size"], target["seed"], target["valid_candidates_target"]), (4, 2, 7, 1))
        change = option["requirement_changes"][0]
        self.assertEqual(change["before"]["property"], "chemical_system")
        for field in ("kind", "property", "value", "unit", "strength", "relation"):
            self.assertEqual(change["before"][field], change["after"][field])
        self.assertEqual(change["after"]["checker"], "element_equality")
        self.assertEqual(change["after"]["stages"], ["search", "post_check"])
        self.assertFalse(option["auto_apply"])
        self.assertTrue(option["requires_reviewed_new_plan"])
        self.assertEqual(option["sampling_cap_change"], 0)
        self.assertEqual(plan, before)
        self.assertEqual(plan["original_request"], request)
        # Explicitly simulates the user's later approval; the repair itself has
        # not mutated the old plan or silently dropped the element requirement.
        fields = {"requirement_id", "source", "kind", "property", "value", "unit", "strength", "relation"}
        revised = {**request, **target, "requirements": [{k: v for k, v in r.items() if k in fields} for r in plan["requirements"]]}
        accepted = self.service.prepare_plan(revised)
        self.assertEqual(accepted["state"], "ready", accepted["issues"])
        mapping = next(m for m in accepted["requirement_mappings"] if m["requirement_id"] == change["requirement_id"])
        self.assertEqual({r["stage"] for r in mapping["routes"]}, {"search", "post_check"})
        self.assertNotEqual(accepted["plan_id"], plan["plan_id"])
        self.assertEqual(self.service.store.plan(plan["plan_id"])["state"], "blocked")

    def test_two_structural_guides_have_distinct_explicit_alternatives(self):
        plan = self.service.prepare_plan({"conditions": {"chemical_system": "Na-Cl", "space_group": 221}, "num_samples": 2})
        options = self.structural_options(plan)
        self.assertEqual({o["execution_changes"]["after"]["checkpoint"] for o in options}, {"chemical_system", "space_group"})
        self.assertEqual(len({o["option_id"] for o in options}), 2)
        for option in options:
            changed = option["requirement_changes"]
            self.assertEqual(len(changed), 1)
            remaining = option["execution_changes"]["after"]["conditions"]
            self.assertNotIn(changed[0]["before"]["property"], remaining)
            self.assertEqual(set(option["preserved_requirement_ids"]), {r["requirement_id"] for r in plan["requirements"]})
            self.assertFalse(option["execution_changes"]["after"]["unconditional"])
            self.assertIn("user-approved task revision", option["proposed_action"])

    def test_no_suggestion_silently_drops_an_unsupported_numerical_combination(self):
        plan = self.service.prepare_plan({"conditions": {"chemical_system": "Fe-Ni-O", "dft_band_gap": 2, "ml_bulk_modulus": 150}, "num_samples": 2})
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(self.structural_options(plan), [])
        self.assertEqual(plan["conditions"]["ml_bulk_modulus"], 150)
        self.assertEqual(plan["conditions"]["dft_band_gap"], 2)

    def test_ready_plans_offer_no_repair_and_option_identity_is_stable(self):
        plan = self.service.prepare_plan({"conditions": {"chemical_system": "Fe-Ni-O"}, "num_samples": 2})
        self.assertEqual(plan["repair_options"], [])
        blocked = self.service.prepare_plan({"conditions": {"chemical_system": "Fe-Ni-O", "dft_band_gap": 2}, "num_samples": 2})
        self.assertEqual(repair_options(blocked), blocked["repair_options"])


if __name__ == "__main__":
    unittest.main()
