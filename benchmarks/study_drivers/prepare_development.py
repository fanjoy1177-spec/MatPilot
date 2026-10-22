"""Build and actually CPU-validate a pinned local development runtime pack.

No provider client, credential store, cost ledger or Harness agent is opened.
Provider/model/reasoning/host identities are explicit operator inputs, not an
assertion that this CPU-only preparation exercised that host or language model.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from matpilot.adapters.mattergen import CHECKPOINTS, UNITS
from matpilot.evaluation import ABLATIONS, GROUPS, configuration_identity, digest
from matpilot.study_workspace import development_selection, export_development


ENV_KEYS = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
            "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH"}


def hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def safe_environment():
    # Child CPU checks never need the user's API credentials or Python hooks.
    return {**{key: value for key, value in os.environ.items() if key.upper() in ENV_KEYS},
            "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


def executable(value, fallback, label):
    selected = value or fallback
    path = shutil.which(str(selected)) if selected else None
    if path is None and selected and Path(selected).is_file():
        path = str(Path(selected).resolve())
    if not path:
        raise ValueError(f"Provide a usable {label} executable explicitly.")
    return str(Path(path).resolve())


def copy_sources(source: Path):
    selected = set()
    for directory in (ROOT / "src/matpilot", ROOT / "benchmarks/baseline", ROOT / "benchmarks/study_drivers"):
        for path in directory.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.name.endswith(".test.mjs"):
                continue
            if path.suffix in {".py", ".mjs", ".json", ".md", ".txt", ".patch"} or path.name.startswith("LICENSE"):
                selected.add(path)
    selected.update(ROOT / path for path in (
        "benchmarks/harness_study.mjs", "benchmarks/study/fixtures.development.json",
        "examples/config.multimodel.demo.json", "examples/templates/cubic_nacl.cif", "pyproject.toml", "LICENSE"))
    captured = []
    for original in sorted(selected):
        data = original.read_bytes()
        destination = source / original.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(data)
        captured.append((original, hashlib.sha256(data).hexdigest()))
    if any(hash_file(path) != expected for path, expected in captured):
        raise ValueError("Source files changed while copying; prepare a fresh output directory.")


def original_instructions(source: Path):
    tree = ast.parse((source / "src/matpilot/server.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "INSTRUCTIONS" for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("Cannot find the actual production INSTRUCTIONS constant.")


def build_instructions(source: Path, capability: dict):
    upstream = source / "benchmarks/study_drivers/upstream_snapshot"
    skill_paths = sorted(upstream.glob("skills/*/SKILL.md"))
    if len(skill_paths) < 5:
        raise ValueError("Normal upstream skills are incomplete.")
    common = (
        "研究执行规则：只使用当前提供的工具。所有组使用同一份公开科学能力和固定科学参数，真实计算保持关闭。\n"
        "共同执行授权协议：本开发测试题的控制器已授权完成题干范围内的CPU回放及只读检查；合法参数不变且不扩大预算时，不重复请求执行授权。\n"
        "此共同预授权不补全未知参数，也不授权改变任务：缺少必需信息，或需要实质改变条件、预算时，仍调用ask_clarification；无法得到所需许可或信息时不作相应变更。\n"
        "当前后端为按实际输入和有序采样绑定的共享CPU回放，数据若标synthetic必须保留此标识；不能把流程测试说成材料性能。\n"
        "MatterGen guidance_scale统一2.0，DiffCSP step_lr统一1e-5。采样失败仍消耗已启动的次数，不自动补足。\n"
        "先读status可见已使用预算，get_candidates/get_metrics及两套artifact读取工具对各组同等可用；使用明确候选和结构版本。\n"
        "DiffCSP仅使用list_templates返回的已登记模板，不从化学式猜测空间群或Wyckoff信息。原始上游工具可能只返回目录，请读取目录工件。\n"
        "真正缺少必需信息时调用ask_clarification；固定控制器无答案时不能代替用户作决定。遵守题目已有的执行授权范围。\n"
        "以下正常上游技能全文仅适用于已提供工具与共同科学能力；环境安装、训练、外部脚本和真实计算步骤不在本研究工具范围内。\n\n"
        "共同科学能力（各组相同）：\n" + json.dumps(capability, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    for skill in skill_paths:
        common += "\n--- BEGIN NORMAL UPSTREAM SKILL " + skill.relative_to(upstream).as_posix() + " ---\n"
        common += skill.read_text(encoding="utf-8")
        common += "\n--- END NORMAL UPSTREAM SKILL ---\n"
    instruction_dir = source / "instructions"
    instruction_dir.mkdir()
    common_path = instruction_dir / "upstream.txt"
    common_path.write_text(common, encoding="utf-8", newline="\n")
    production = original_instructions(source)
    override = (
        "\n\n本研究执行覆盖（优先于上面的生产部署执行步骤）：\n"
        "上方生产说明中的再次确认步骤由共同执行授权协议中的预授权替代，仅限本研究题干范围内的CPU回放及只读检查；不影响生产插件的确认规则。\n"
        "本组调用get_capabilities→prepare_plan→execute_plan→check_candidates；执行只使用共享U+真实包装函数与CPU回放。\n"
        "禁止调用submit_job、get_job、control_job、get_results、screen_candidates、build_report等当前未提供的生产任务接口；没有job_id。\n"
        "Settings的demo仅使生产验证器允许已登记开发模板，不表示本研究统一返回固定NaCl。实际回放依输入、次数和故障帧绑定。\n"
        "完整复制用户原文到raw_request，并逐条保留requirements；不得为了通过验证删去原文。ready方案只在用户原任务授权范围内执行一次。\n"
        "结果检查首先针对共享engine产物版本。上游CIF重序列化的新版本必须保留父版本映射，不能直接继承另一版本的性质证据。\n"
        "本研究没有真实模型推理或随机种子推理验证，不宣称AI已提高正确率、提速或产生材料突破。\n")
    matpilot_path = instruction_dir / "matpilot.txt"
    matpilot_path.write_text(common + "\n--- ACTUAL MatPilot server.INSTRUCTIONS ---\n" + production + override,
                             encoding="utf-8", newline="\n")
    # Identical M-arm instructions: only the actual broker/checker switches vary.
    return {"U": common_path, "U+": common_path, "M": matpilot_path,
            **{group: matpilot_path for group in ABLATIONS}}


def source_manifest(source: Path):
    files = [{"path": path.relative_to(source).as_posix(), "sha256": hash_file(path)}
             for path in sorted(source.rglob("*")) if path.is_file()
             and "__pycache__" not in path.relative_to(source).parts and ".work" not in path.relative_to(source).parts]
    revision = "development-source-" + digest(files)
    manifest = {"source_revision": revision, "files": files,
                "scope": "All copied source, normal instructions, templates and declared dependencies; external Python/Node packages are identified separately."}
    path = source / "source-manifest.json"
    save(path, manifest)
    return path, manifest


def verify_smoke(report, source: Path, manifest, fixture_hash, groups=GROUPS):
    """Validate real smoke evidence before deriving configuration receipts."""
    if (report.get("format") != "matpilot-study-cpu-validation/v1"
            or report.get("passed") is not True or report.get("evidence_scope") != "cpu_tool_contract_only"
            or report.get("acceptance") != "not_harness_end_to_end_acceptance"
            or report.get("source_files_unchanged_during_run") is not True
            or report.get("fixture_manifest_sha256") != fixture_hash
            or any(report.get(key) != 0 for key in ("api_requests", "api_cny", "real_material_model_runs"))):
        raise ValueError("CPU bridge validation did not provide passing, zero-API evidence for this fixture.")
    declared = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    source_files = report.get("source_files", [])
    if not source_files:
        raise ValueError("CPU report has no bound source files.")
    # The JS smoke intentionally uses JSON.stringify (insertion order) here.
    js_hash = hashlib.sha256(json.dumps(source_files, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if report.get("source_manifest_sha256") != js_hash:
        raise ValueError("CPU report source-list identity changed.")
    for entry in source_files:
        if declared.get(entry["path"]) != entry["sha256"] or hash_file(source / entry["path"]) != entry["sha256"]:
            raise ValueError("CPU-tested dependency differs from the exported source manifest.")
    needed = {"benchmarks/study_drivers/harness_bridge.mjs", "benchmarks/study_drivers/broker.py",
              "benchmarks/study_drivers/upstream_replay.py", "src/matpilot/study_replay.py"}
    if not needed <= {entry["path"] for entry in source_files}:
        raise ValueError("CPU report does not cover the actual bridge and worker dependencies.")
    cases = report.get("cases", [])
    if not cases or any(case.get("passed") is not True or not case.get("checks") for case in cases):
        raise ValueError("CPU validation contains failed or undocumented checks.")
    for group in dict.fromkeys((*GROUPS, *groups)):
        selected = [case for case in cases if case.get("group") == group]
        if group in GROUPS and {case.get("engine") for case in selected} != {"mattergen", "diffcsp"}:
            raise ValueError("Every main group requires actual CPU checks of both engines.")
        if not selected:
            raise ValueError("Selected ablation has no actual CPU validation case: " + group)
        for case in selected:
            identity = case.get("identity", {})
            if identity.get("group") != group or identity.get("real_compute_allowed") is not False:
                raise ValueError("CPU report group identity or execution boundary is incorrect.")
            if group == "M" or group in ABLATIONS:
                mapping, evidence = ABLATIONS[group][1:] if group in ABLATIONS else (True, True)
                layer = identity.get("checking_layer", {})
                if layer.get("requirement_mapping") is not mapping or layer.get("evidence_checks") is not evidence:
                    raise ValueError("CPU checking-layer flags do not implement the selected group.")
                if case.get("candidate_checks", {}).get("checks_performed") is not evidence:
                    raise ValueError("CPU candidate checking behavior does not match its feature switch.")
                plan = case.get("plan", {})
                if not mapping and (plan.get("requirement_mapping_count") != 0
                                    or plan.get("coverage", {}).get("scope") != "disabled_by_predeclared_mapping_ablation"):
                    raise ValueError("CPU mapping ablation did not actually disable requirement mapping.")
                if not evidence and any(case["candidate_checks"].get(key) is not None for key in
                                        ("valid_candidate_count", "unique_valid_candidate_count")):
                    raise ValueError("Disabled evidence checks cannot report validated candidate counts.")
    return cases


def prepare(output, *, provider, model, reasoning_mode, host_version, python_path=None, node_path=None,
            max_turns=12, max_output_tokens=2048, max_requests=16, timeout_ms=180000, sampling_cap=32, repeats=1,
            task_ids=None, groups=None):
    for label, value in (("provider", provider), ("model", model), ("reasoning_mode", reasoning_mode), ("host_version", host_version)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Explicit nonempty " + label + " is required; it will not be inferred.")
    for label, value in (("max_turns", max_turns), ("max_output_tokens", max_output_tokens), ("max_requests", max_requests),
                         ("timeout_ms", timeout_ms), ("sampling_cap", sampling_cap)):
        if type(value) is not int or value <= 0:
            raise ValueError(label + " must be a positive integer.")
    if type(repeats) is not int or repeats not in (1, 2, 3):
        raise ValueError("Development repeats must be 1, 2 or 3.")
    suite = json.loads((ROOT / "benchmarks/study/development_tasks.json").read_text(encoding="utf-8"))
    task_ids, groups = development_selection(suite, task_ids, groups)
    defined_groups = list(dict.fromkeys((*GROUPS, *groups)))
    python_path = executable(python_path, sys.executable, "Python")
    node_path = executable(node_path, "node", "Node")
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = root / "source"
    source.mkdir()
    copy_sources(source)
    runtime_probe = subprocess.run([python_path, "-c", "import json,sys,importlib.metadata as m;print(json.dumps({'python':sys.version.split()[0],'packages':{p:m.version(p) for p in ['pymatgen','mcp','PyYAML']}}))"],
                                   check=True, text=True, encoding="utf-8", capture_output=True, timeout=30, env=safe_environment())
    runtime_versions = json.loads(runtime_probe.stdout)
    runtime_versions["node"] = subprocess.run([node_path, "--version"], check=True, text=True, capture_output=True,
                                               timeout=15, env=safe_environment()).stdout.strip()
    capability = {"format": "matpilot-common-study-capability/v1", "science_execution": "shared_condition_bound_cpu_replay",
                  "real_compute_allowed": False, "mattergen": {"checkpoints": CHECKPOINTS, "units": UNITS,
                  "semantics": "Conditions are generation targets, never measured properties or guaranteed bounds. chemical_system is an element set, not a fixed formula."},
                  "diffcsp": {"checkpoints": ["mp_csp", "perov_csp", "mpts_csp"],
                  "inputs": ["registered template", "space group", "Wyckoff orbits", "elements per orbit"],
                  "scope": "Only operator-registered templates. Actual wrapper runs; heavy tensor/symmetry/model setup is replaced by disclosed CPU replay."},
                  "scientific_defaults": {"guidance_scale": 2.0, "step_lr": 1e-5},
                  "runtime_versions": runtime_versions,
                  "candidate_evidence": "Explicit candidate/version/artifact hashes; every synthetic datum remains labelled. Different methods or structure versions cannot inherit each other's evidence."}
    capability_path = source / "scientific-capability.json"
    save(capability_path, capability)
    instructions = build_instructions(source, capability)
    clarification_policy = {"version": 1, "mode": "controller_only_fixed_replies", "no_invented_answers": True,
                            "tasks": [{"id": task["id"], "fixed_clarifications": task["fixed_clarifications"]}
                                      for task in suite["tasks"] if task["id"] in task_ids]}
    clarification_path = source / "clarification-policy.json"
    save(clarification_path, clarification_policy)
    manifest_path, manifest = source_manifest(source)
    fixture_path = source / "benchmarks/study/fixtures.development.json"
    fixture_hash = hash_file(fixture_path)
    # Execute the copied bridge/broker against copied source, not editable imports.
    (source / ".work").mkdir()
    report_path = root / "reports/cpu-smoke.json"
    report_path.parent.mkdir()
    result = subprocess.run([node_path, str(source / "benchmarks/study_drivers/smoke_bridge.mjs"), "--python", python_path, "--report", str(report_path)],
                            text=True, encoding="utf-8", capture_output=True, timeout=300, env=safe_environment())
    (root / "reports/smoke.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (root / "reports/smoke.stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode or not report_path.is_file():
        raise ValueError("Actual CPU bridge smoke failed; inspect reports/smoke.*.txt. No runtime pack was exported.")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cases = verify_smoke(report, source, manifest, fixture_hash, groups=groups)
    # Also bind instructions/config files outside the smoke's narrower source list.
    if any(hash_file(source / item["path"]) != item["sha256"] for item in manifest["files"]):
        raise ValueError("Exported source changed during CPU validation.")
    module = source / "benchmarks/study_drivers/harness_bridge.mjs"
    baseline = json.loads((source / "benchmarks/baseline/source-lock.json").read_text(encoding="utf-8"))
    common = {"host_version": host_version, "llm_provider": provider, "llm_version": model, "reasoning_mode": reasoning_mode,
              "scientific_capability_hash": hash_file(capability_path), "replay_fixture_hash": fixture_hash,
              "clarification_policy_hash": hash_file(clarification_path), "max_turns": max_turns, "max_output_tokens": max_output_tokens,
              "max_requests": max_requests, "timeout_ms": timeout_ms, "sampling_cap": sampling_cap, "guidance_scale": 2,
              "source_revision": manifest["source_revision"], "source_manifest_sha256": hash_file(manifest_path),
              "driver_sha256": hash_file(module), "ablation": None}
    environment, inventory = {}, {"fixture_manifest_path": str(fixture_path), "drivers": {}}
    for group in defined_groups:
        ablation, mapping, evidence = ABLATIONS.get(group, (None, group == "M", group == "M"))
        driver_options = {"python_path": python_path,
                          "implementation_group": group,
                          "requirement_mapping": mapping, "evidence_checks": evidence, "ablation": ablation,
                          "broker_path": str(source / "benchmarks/study_drivers/broker.py"),
                          "template_config": str(source / "examples/config.multimodel.demo.json"),
                          "request_timeout_ms": 30000}
        config = {**common, "instruction_hash": hash_file(instructions[group]),
                  "baseline_patch_hash": None if group == "U" else baseline["patch_sha256"],
                  "requirement_mapping": mapping, "evidence_checks": evidence, "ablation": ablation,
                  "driver_options_hash": digest(driver_options)}
        config_hash = configuration_identity(config)
        checks = [case for case in cases if case["group"] == group]
        receipt = {"format": "matpilot-development-driver-validation/v1", "group": group,
                   "configuration_sha256": config_hash, "passed": True, "evidence_scope": "cpu_tool_contract_only",
                   "acceptance": "not_harness_end_to_end_acceptance", "source_manifest_sha256": common["source_manifest_sha256"],
                   "smoke_report_sha256": hash_file(report_path), "smoke_report_path": "cpu-smoke.json", "checks": checks,
                   "declaration": "Passing is derived from actual copied-source bridge checks. Host/provider/model identities are scheduled settings, not CPU-tested AI behavior."}
        validation_path = root / "reports" / ("driver-" + group.replace("+", "plus") + ".json")
        save(validation_path, receipt)
        config.update(driver_validated=True, driver_validation={"configuration_sha256": config_hash, "report_sha256": hash_file(validation_path)})
        environment[group] = config
        inventory["drivers"][group] = {
            "module_path": str(module), "instruction_path": str(instructions[group]), "capability_path": str(capability_path),
            "source_manifest_path": str(manifest_path), "source_manifest_sha256": common["source_manifest_sha256"],
            "validation_report_path": str(validation_path), "options": driver_options}
    save(root / "environment.json", environment)
    save(root / "inventory.json", inventory)
    pack = export_development(suite, environment, inventory, root / "runtime.json", inventory_base=root, repeats=repeats,
                              task_ids=task_ids, groups=groups)
    summary = {"status": "development_prepared_cpu_validated_not_ai_run", "rows": len(pack["rows"]),
               "runtime_path": str(root / "runtime.json"), "runtime_sha256": hash_file(root / "runtime.json"),
               "source_manifest_sha256": common["source_manifest_sha256"], "cpu_passed_cases": report["passed_cases"],
               "selection": pack["selection"], "selection_sha256": pack["provenance"]["selection_sha256"],
               "defined_groups": defined_groups,
               "evidence_scope": "cpu_tool_contract_only", "api_requests": 0, "api_cny": 0,
               "cost_ledger_opened": False, "api_credentials_loaded": False, "real_material_model_runs": 0,
               "runtime_versions": runtime_versions}
    save(root / "preparation.json", summary)
    (root / "NEXT_STEPS.txt").write_text(
        "本包仅完成真实CPU工具链验证，尚未运行Harness智能体或证明AI提升。\n"
        "environment.json中的provider/model/reasoning/host来自显式输入；CPU检查未验证模型或宿主版本。\n"
        "runtime.json只含开发题目的公开提示和控制器回复，不含评分标准。source/source-manifest.json锁定实际代码与正常技能。\n"
        "下一步在明确的Harness运行环境加载source/benchmarks/harness_study.mjs，packPath指向runtime.json。\n"
        "继续使用已配置的API凭据、实际价格和既有费用账本；本准备器没有读取或新建账本，也没有修改20元通知阈值。\n"
        "先检查运行器ready.json，再按runtime SHA和明确选择的run_ids创建启动请求。准备器不会创建start.json或自动调用API。\n"
        f"本包冻结{len(task_ids)}道公开开发题、{len(groups)}个执行组、每题每组{repeats}次，共{len(pack['rows'])}条运行；selection包含完整预选清单。\n"
        "未排入rows的U/U+/M驱动定义仅供统一兼容核查，不会自动运行。消融臂与M使用完全相同提示，只改变已验证的执行开关。\n"
        "实际完成Harness运行后才能另存harness_replay_end_to_end验证报告。\n"
        "正式比较仍需独立审定的题库/夹具与真正Harness验收。CPU报告不能升级成正式AI效果证据。\n"
        "本包包含本机绝对运行路径，属于本地运行工件；换机器须用明确provider/model/reasoning/host重建，不直接当作公开发布包。\n",
        encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-mode", required=True)
    parser.add_argument("--host-version", required=True)
    parser.add_argument("--python-path")
    parser.add_argument("--node-path")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--max-requests", type=int, default=16)
    parser.add_argument("--timeout-ms", type=int, default=180000)
    parser.add_argument("--sampling-cap", type=int, default=32)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--task-id", "--task-ids", dest="task_ids", nargs="+", action="extend")
    parser.add_argument("--group", "--groups", dest="groups", nargs="+", action="extend", choices=(*GROUPS, *ABLATIONS))
    args = parser.parse_args(argv)
    print(json.dumps(prepare(**vars(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
