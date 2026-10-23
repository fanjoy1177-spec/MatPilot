"""Actually build a copied-source CPU-validated pack; never invoke an LLM."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from matpilot.evaluation import ABLATIONS, GROUPS, configuration_identity
from matpilot.study_workspace import _driver


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "benchmarks/study_drivers/prepare_development.py"
SPEC = importlib.util.spec_from_file_location("study_prepare_under_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StudyPrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=base)
        cls.root = Path(cls.tmp.name)
        cls.output = cls.root / "prepared"
        cls.node = shutil.which("node")
        if not cls.node:
            raise RuntimeError("A real Node executable is required; CPU bridge validation must not be skipped.")
        cls.summary = MODULE.prepare(cls.output, provider="TEST-ONLY-unexecuted-provider", model="TEST-ONLY-unexecuted-model",
                                     reasoning_mode="TEST-ONLY-off", host_version="TEST-ONLY-unexecuted-host",
                                     python_path=sys.executable, node_path=cls.node)
        cls.environment = json.loads((cls.output / "environment.json").read_text(encoding="utf-8"))
        cls.inventory = json.loads((cls.output / "inventory.json").read_text(encoding="utf-8"))
        cls.pack = json.loads((cls.output / "runtime.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_actual_bridge_smoke_binds_copied_sources_and_zero_api_scope(self):
        report = json.loads((self.output / "reports/cpu-smoke.json").read_text(encoding="utf-8"))
        self.assertTrue(report["passed"])
        self.assertEqual(report["passed_cases"], 8)
        self.assertTrue(report["source_files_unchanged_during_run"])
        self.assertEqual(report["evidence_scope"], "cpu_tool_contract_only")
        self.assertEqual(self.summary["api_requests"], 0)
        self.assertFalse(self.summary["api_credentials_loaded"])
        self.assertFalse(self.summary["cost_ledger_opened"])
        self.assertEqual(self.summary["real_material_model_runs"], 0)
        service_hash = hashlib.sha256((self.output / "source/src/matpilot/service.py").read_bytes()).hexdigest()
        for case in report["cases"]:
            if case["group"].startswith("M"):
                self.assertEqual(case["identity"]["checking_layer"]["source_sha256"]["service"], service_hash)

    def test_pack_contains_public_tasks_actual_group_configs_and_no_answers(self):
        self.assertEqual(len(self.pack["rows"]), 36)
        self.assertEqual(self.pack["mode"], "development")
        self.assertFalse(self.pack["real_compute_allowed"])
        self.assertEqual(self.pack["shared"]["model"], "TEST-ONLY-unexecuted-model")
        self.assertEqual(self.pack["shared"]["host_version"], "TEST-ONLY-unexecuted-host")
        for task in self.pack["tasks"]:
            self.assertEqual(set(task), {"id", "prompt", "fixture_id", "fixed_clarifications"})
        for group, config in self.environment.items():
            self.assertEqual(config["driver_validation"]["configuration_sha256"], configuration_identity(config))
            self.assertEqual(self.inventory["drivers"][group]["options"]["implementation_group"], group)
            self.assertEqual(config["driver_options_hash"], MODULE.digest(self.inventory["drivers"][group]["options"]))
            self.assertEqual(config["requirement_mapping"], group == "M")
            self.assertEqual(config["evidence_checks"], group == "M")
            receipt = json.loads(Path(self.inventory["drivers"][group]["validation_report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(receipt["group"], group)
            self.assertTrue(all(case["group"] == group for case in receipt["checks"]))
            self.assertEqual(receipt["acceptance"], "not_harness_end_to_end_acceptance")

    def test_normal_skills_are_complete_and_common_capabilities_identical(self):
        source = self.output / "source"
        common = (source / "instructions/upstream.txt").read_text(encoding="utf-8")
        matpilot = (source / "instructions/matpilot.txt").read_text(encoding="utf-8")
        self.assertTrue(matpilot.startswith(common))
        for skill in (source / "benchmarks/study_drivers/upstream_snapshot/skills").glob("*/SKILL.md"):
            self.assertIn(skill.read_text(encoding="utf-8"), common)
        self.assertIn(MODULE.original_instructions(source), matpilot)
        self.assertIn("禁止调用submit_job", matpilot)
        self.assertIn("execute_plan", matpilot)
        self.assertEqual(len({c["scientific_capability_hash"] for c in self.environment.values()}), 1)
        self.assertEqual(self.environment["U"]["instruction_hash"], self.environment["U+"]["instruction_hash"])

    def test_source_manifest_is_root_relative_complete_and_tamper_checked(self):
        source = self.output / "source"
        manifest = json.loads((source / "source-manifest.json").read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in manifest["files"]}
        for required in ("src/matpilot/service.py", "benchmarks/study_drivers/broker.py",
                         "benchmarks/study_drivers/harness_bridge.mjs", "examples/templates/cubic_nacl.cif",
                         "benchmarks/baseline/atomisticskills-uplus.patch", "instructions/matpilot.txt"):
            self.assertIn(required, paths)
        self.assertFalse(any(Path(path).is_absolute() or ".." in Path(path).parts for path in paths))
        self.assertFalse(any(".env" == Path(path).name or "__pycache__" in path or "ledger" in path for path in paths))
        target = source / "src/matpilot/service.py"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n# changed after validation\n")
            with self.assertRaisesRegex(ValueError, "source dependency changed"):
                _driver(self.environment["M"], self.inventory["drivers"]["M"], self.output, formal=False)
        finally:
            target.write_bytes(original)

    def test_common_pre_authorization_is_identical_and_production_override_is_limited(self):
        source = self.output / "source"
        common = (source / "instructions/upstream.txt").read_text(encoding="utf-8")
        matpilot = (source / "instructions/matpilot.txt").read_text(encoding="utf-8")
        authorization = "共同执行授权协议：本开发测试题的控制器已授权完成题干范围内的CPU回放及只读检查；合法参数不变且不扩大预算时，不重复请求执行授权。"
        constraint = "此共同预授权不补全未知参数，也不授权改变任务：缺少必需信息，或需要实质改变条件、预算时，仍调用ask_clarification；无法得到所需许可或信息时不作相应变更。"
        for group in ("U", "U+", "M"):
            text = Path(self.inventory["drivers"][group]["instruction_path"]).read_text(encoding="utf-8")
            self.assertEqual(text.count(authorization), 1)
            self.assertEqual(text.count(constraint), 1)
            self.assertTrue(text.startswith(common))
        production = MODULE.original_instructions(source)
        override = "上方生产说明中的再次确认步骤由共同执行授权协议中的预授权替代，仅限本研究题干范围内的CPU回放及只读检查；不影响生产插件的确认规则。"
        self.assertIn(override, matpilot)
        self.assertNotIn(override, common)
        self.assertGreater(matpilot.index(override), matpilot.index(production) + len(production))
        self.assertIn("get_versions", common)
        self.assertIn("query_database", common)
        self.assertIn("verify_candidate_artifact", common)
        research_override = matpilot[matpilot.index(override):]
        self.assertIn("禁止调用未提供的compare_runs或不存在的compare_metrics", research_override)
        self.assertIn("compare_candidate_metrics(candidate_ids,property_name,structure_versions,metric_ids)", research_override)
        # Production sources remain byte-identical; only the research instructions override them.
        self.assertEqual((source / "src/matpilot/server.py").read_bytes(), (ROOT / "src/matpilot/server.py").read_bytes())

    def test_cpu_receipt_cannot_be_used_for_formal_acceptance(self):
        with self.assertRaisesRegex(ValueError, "actual Harness replay end-to-end"):
            _driver(self.environment["M"], self.inventory["drivers"]["M"], self.output, formal=True)
        report = json.loads((self.output / "reports/cpu-smoke.json").read_text(encoding="utf-8"))
        manifest = json.loads((self.output / "source/source-manifest.json").read_text(encoding="utf-8"))
        report["cases"][0]["passed"] = False
        with self.assertRaisesRegex(ValueError, "failed or undocumented"):
            MODULE.verify_smoke(report, self.output / "source", manifest, report["fixture_manifest_sha256"])

    def test_runtime_pack_passes_actual_javascript_preflight_without_running_agent(self):
        source = self.output / "source"
        code = ("import {loadRuntimePack} from " + json.dumps((source / "benchmarks/harness_study.mjs").as_uri()) + ";"
                "const r=loadRuntimePack(" + json.dumps(str(self.output / "runtime.json")) + ");"
                "console.log(JSON.stringify({mode:r.pack.mode,rows:r.pack.rows.length}));")
        result = subprocess.run([self.node, "--input-type=module", "-e", code], text=True, encoding="utf-8",
                                capture_output=True, timeout=30, env=MODULE.safe_environment())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"mode": "development", "rows": 36})

    def test_required_identity_inputs_and_no_credential_or_ledger_side_effect(self):
        missing = self.root / "must-not-exist"
        with self.assertRaisesRegex(ValueError, "Explicit nonempty model"):
            MODULE.prepare(missing, provider="test", model="", reasoning_mode="off", host_version="test")
        self.assertFalse(missing.exists())
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "TEST-ONLY-NEVER-SENT", "PYTHONPATH": "TEST-ONLY-UNTRUSTED"}):
            env = MODULE.safe_environment()
            self.assertNotIn("DEEPSEEK_API_KEY", env)
            self.assertNotIn("PYTHONPATH", env)
        result = subprocess.run([sys.executable, str(SCRIPT), str(missing)], text=True, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())
        self.assertFalse(any(self.output.rglob("start.json")))
        self.assertFalse(any(self.output.rglob("*ledger*")))

    def test_selected_ablation_cli_freezes_actual_switches_and_identical_instructions(self):
        output = self.root / "ablations"
        command = [sys.executable, str(SCRIPT), str(output), "--provider", "TEST-ONLY-unexecuted-provider",
                   "--model", "TEST-ONLY-unexecuted-model", "--reasoning-mode", "TEST-ONLY-off",
                   "--host-version", "TEST-ONLY-unexecuted-host", "--node-path", self.node,
                   "--task-ids", "D0101", "D0201", "D0202", "D0502",
                   "--groups", "M", *ABLATIONS, "--max-output-tokens", "8192"]
        result = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, timeout=300,
                                env=MODULE.safe_environment())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        pack = json.loads((output / "runtime.json").read_text(encoding="utf-8"))
        environment = json.loads((output / "environment.json").read_text(encoding="utf-8"))
        inventory = json.loads((output / "inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(len(pack["rows"]), 12)
        self.assertEqual({r["group"] for r in pack["rows"]}, {"M", *ABLATIONS})
        self.assertEqual(set(pack["drivers"]), {*GROUPS, *ABLATIONS})
        self.assertEqual(pack["selection"]["task_ids"], ["D0101", "D0201", "D0202", "D0502"])
        self.assertEqual(summary["selection_sha256"], MODULE.digest(pack["selection"]))
        self.assertEqual(summary["api_requests"], 0)
        self.assertEqual(summary["real_material_model_runs"], 0)
        m_text = Path(inventory["drivers"]["M"]["instruction_path"]).read_bytes()
        for group in ("M", *ABLATIONS):
            config, driver = environment[group], inventory["drivers"][group]
            self.assertEqual(Path(driver["instruction_path"]).read_bytes(), m_text)
            self.assertEqual(config["driver_options_hash"], MODULE.digest(driver["options"]))
            self.assertEqual(driver["options"]["implementation_group"], group)
            expected = ABLATIONS.get(group, (None, True, True))
            self.assertEqual(tuple(driver["options"][k] for k in ("ablation", "requirement_mapping", "evidence_checks")), expected)
            receipt = json.loads(Path(driver["validation_report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(receipt["configuration_sha256"], configuration_identity(config))
            self.assertTrue(receipt["checks"])
            self.assertTrue(all(c["group"] == group for c in receipt["checks"]))
            for case in receipt["checks"]:
                layer = case["identity"]["checking_layer"]
                self.assertEqual((layer["requirement_mapping"], layer["evidence_checks"]), expected[1:])
            with self.assertRaisesRegex(ValueError, "actual Harness replay end-to-end"):
                _driver(config, driver, output, formal=True)
        self.assertNotIn('"gold"', json.dumps(pack))
        self.assertNotIn('"review_path"', json.dumps(pack))
        self.assertFalse((output / "source/benchmarks/study/development_tasks.json").exists())
        policy = json.loads((output / "source/clarification-policy.json").read_text(encoding="utf-8"))
        self.assertEqual({t["id"] for t in policy["tasks"]}, set(pack["selection"]["task_ids"]))
        source = output / "source"
        code = ("import {loadRuntimePack} from " + json.dumps((source / "benchmarks/harness_study.mjs").as_uri()) + ";"
                "const r=loadRuntimePack(" + json.dumps(str(output / "runtime.json")) + ");"
                "console.log(JSON.stringify({rows:r.pack.rows.length,drivers:Object.keys(r.pack.drivers).length}));")
        preflight = subprocess.run([self.node, "--input-type=module", "-e", code], text=True, encoding="utf-8",
                                  capture_output=True, timeout=30, env=MODULE.safe_environment())
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertEqual(json.loads(preflight.stdout), {"rows": 12, "drivers": 5})

    def test_invalid_selection_is_rejected_before_cpu_or_output_creation(self):
        for index, kwargs in enumerate(({"task_ids": ["unknown"]}, {"groups": []},
                                        {"groups": ["M_without_mapping"]}, {"task_ids": ["D0101", "D0101"]})):
            output = self.root / ("rejected-" + str(index))
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MODULE.prepare(output, provider="test", model="test", reasoning_mode="off", host_version="test", **kwargs)
            self.assertFalse(output.exists())

    def test_custom_inputs_require_a_pair_and_cannot_promote_formal_fixtures(self):
        output = self.root / "rejected-custom-inputs"
        suite_path = ROOT / "benchmarks/study/development_tasks.json"
        with self.assertRaisesRegex(ValueError, "supplied together"):
            MODULE.prepare(output, provider="test", model="test", reasoning_mode="off", host_version="test",
                           suite_path=suite_path)
        fixture = json.loads((ROOT / "benchmarks/study/fixtures.development.json").read_text(encoding="utf-8"))
        fixture["purpose"] = "formal_workflow_study"
        fixture_path = self.root / "not-development.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "explicit development"):
            MODULE.prepare(output, provider="test", model="test", reasoning_mode="off", host_version="test",
                           suite_path=suite_path, fixture_manifest_path=fixture_path)
        self.assertFalse(output.exists())

    def test_ablation_receipt_requires_real_flag_and_behavior_evidence(self):
        original = json.loads((self.output / "reports/cpu-smoke.json").read_text(encoding="utf-8"))
        manifest = json.loads((self.output / "source/source-manifest.json").read_text(encoding="utf-8"))
        for mutation in ("missing_case", "false_flag", "made_up_count", "mapping_still_enabled"):
            report = copy.deepcopy(original)
            if mutation == "missing_case":
                report["cases"] = [c for c in report["cases"] if c["group"] != "M_without_evidence"]
            elif mutation == "false_flag":
                next(c for c in report["cases"] if c["group"] == "M_without_mapping")["identity"]["checking_layer"]["requirement_mapping"] = True
            elif mutation == "made_up_count":
                next(c for c in report["cases"] if c["group"] == "M_without_evidence")["candidate_checks"]["valid_candidate_count"] = 4
            else:
                next(c for c in report["cases"] if c["group"] == "M_without_mapping")["plan"]["requirement_mapping_count"] = 3
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                MODULE.verify_smoke(report, self.output / "source", manifest, report["fixture_manifest_sha256"], groups=("M", *ABLATIONS))


if __name__ == "__main__":
    unittest.main()
