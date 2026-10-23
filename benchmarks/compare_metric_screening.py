"""Compare frozen/current explicit metric screening on identical synthetic CPU data.

Two isolated SQLite stores receive the same NaCl ZIP and two raw measurements
(1.1 and 1.6 eV). These are synthetic values, not calculated material properties.
Actual Service and registered MCP functions are called; no LLM/model/API is run.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def child(work: Path, fixture: Path, source: Path) -> dict:
    import matpilot
    from matpilot.config import Settings
    from matpilot.server import MetricSelection, make_server
    from matpilot.service import Service
    from matpilot.worker import execute_job

    package = Path(matpilot.__file__).resolve().parent
    package.relative_to(source.resolve())
    work.mkdir(parents=True, exist_ok=False)
    settings = Settings(data_dir=work / "data", backend="demo", real_compute_enabled=False)
    service = Service(settings)
    plan = service.prepare_plan({"conditions": {"dft_band_gap": 1.1}, "num_samples": 1, "batch_size": 1})
    assert plan["state"] == "ready", plan["issues"]
    job, _ = service.store.create_job(plan, "synthetic-screening-comparison", "demo", settings.snapshot())
    with patch("matpilot.worker.demo_archive", side_effect=lambda path, count: shutil.copyfile(fixture, path)):
        execute_job(service.store, service.store.claim(os.getpid()))
    job_id = job["job_id"]
    original = service.get_results(job_id)["records"][0]
    metrics = []
    for index, value in enumerate((1.1, 1.6)):
        raw = work / f"synthetic-metric-{index}.json"
        raw.write_text(json.dumps({"synthetic": True, "value": value, "science_computation_performed": False}, sort_keys=True), encoding="utf-8")
        metrics.append(service.store.record_metric({"candidate_id": original["candidate_id"],
            "structure_version_id": original["structure_version_id"], "property": "dft_band_gap", "value": value, "unit": "eV",
            "method": {"name": "synthetic-method", "version": "synthetic-v1", "weights_identity": "synthetic-no-weights", "parameters": {"synthetic": True}},
            "input_artifact": {"path": original["path"], "sha256": original["sha256"]},
            "raw_output": {"path": str(raw), "sha256": sha(raw)}, "convergence": "not_applicable", "reference_set": None}))
    tools = {tool.name: tool for tool in make_server(settings)._tool_manager.list_tools()}

    def state(result):
        record = result["records"][0]
        check = next(check for check in record["requirement_checks"] if check["property"] == "dft_band_gap")
        return {key: check.get(key) for key in ("status", "observed", "metric_id", "reason")}

    def accounting():
        status = service.get_job(job_id)
        return {"candidate_count": service.get_results(job_id)["count"],
            "sampling_attempts_charged": status["sampling_attempts_charged"],
            "sampling_budget_remaining": status["sampling_budget_remaining"]}

    before = accounting()
    cases = {"default_multiple_metrics": {"service": state(service.screen_candidates(job_id)),
        "registered_mcp": state(tools["screen_candidates"].fn(job_id=job_id))}}
    reports = [service.build_report(job_id)]
    supports_selection = "metric_selections" in inspect.signature(service.screen_candidates).parameters
    for name, metric in (("select_1_1", metrics[0]), ("select_1_6", metrics[1])):
        selection = {"job_id": job_id, **{key: metric[key] for key in
            ("candidate_id", "structure_version_id", "metric_id")}}
        try:
            result = service.screen_candidates(job_id, metric_selections=[selection])
            typed_selection = MetricSelection(**selection)
            report = tools["build_report"].fn(job_id=job_id, metric_selections=[typed_selection])
            reports.append(report)
            saved = json.loads(Path(report["evidence_path"]).read_bytes())["screening"]
            cases[name] = {"supported": True, "service": state(result),
                "registered_mcp": state(tools["screen_candidates"].fn(job_id=job_id, metric_selections=[typed_selection])),
                "persisted_report": state(saved), "all_metric_values": [m["value"] for m in
                    saved["all_metrics_by_structure_version"][original["structure_version_id"]]],
                "selected_metric_ids": saved["metric_selection_audit"]["selected_metric_ids"],
                "unselected_metric_ids": saved["metric_selection_audit"]["unselected_metric_ids"],
                "selection_sha256": saved["metric_selection_audit"]["selection_sha256"]}
        except (TypeError, ValueError) as exc:
            if supports_selection:
                raise
            cases[name] = {"supported": False, "error_type": type(exc).__name__, "error": str(exc)}
    wrong = {"job_id": job_id, **{key: metrics[0][key] for key in
        ("candidate_id", "structure_version_id", "metric_id")}, "candidate_id": "synthetic-unrelated-candidate"}
    try:
        service.screen_candidates(job_id, metric_selections=[wrong])
        cases["wrong_binding"] = {"supported": supports_selection, "rejected": False}
    except (TypeError, ValueError) as exc:
        cases["wrong_binding"] = {"supported": supports_selection, "rejected": supports_selection and isinstance(exc, ValueError),
            "error_type": type(exc).__name__, "error": str(exc)}
    paths = [Path(report["evidence_path"]) for report in reports]
    cases["reports_coexist"] = {"evidence_count": len(set(paths)),
        "all_still_exist": all(path.is_file() for path in paths),
        "persisted_statuses": [state(json.loads(path.read_bytes())["screening"])["status"] for path in paths],
        "evidence_files": [{"path": path.relative_to(work).as_posix(), "sha256": sha(path)} for path in paths]}
    after = accounting()
    assert after == before
    assert state(service.screen_candidates(job_id))["status"] == "unknown"
    assert cases["default_multiple_metrics"]["service"]["status"] == "unknown"
    if supports_selection:
        assert cases["select_1_1"]["service"]["status"] == "pass"
        assert cases["select_1_6"]["service"]["status"] == "fail"
        assert cases["wrong_binding"]["rejected"]
        assert cases["reports_coexist"]["persisted_statuses"] == ["unknown", "pass", "fail"]
    return {"declared_version": matpilot.__version__, "source_sha256": {
        path.relative_to(package).as_posix(): sha(path) for path in sorted(package.rglob("*.py"))},
        "tool_count": len(tools), "input_schemas": {name: tools[name].parameters for name in ("screen_candidates", "build_report")},
        "cases": cases, "fixture_sha256": sha(fixture), "original_cif_sha256": original["sha256"],
        "synthetic_metric_raw_sha256": [metric["raw_output"]["sha256"] for metric in metrics],
        "accounting_before": before, "accounting_after": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-wheel")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--output")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--fixture")
    parser.add_argument("--source")
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(Path(args.work_dir), Path(args.fixture), Path(args.source)), ensure_ascii=False))
        return
    if not args.baseline_wheel or not args.output:
        parser.error("--baseline-wheel and --output are required for comparison")
    work, output, wheel = ((ROOT / value).resolve() for value in (args.work_dir, args.output, args.baseline_wheel))
    for path in (work, output, wheel):
        path.relative_to(ROOT)
    if output.exists():
        raise FileExistsError(output)
    work.mkdir(parents=True, exist_ok=False)
    wheel_hash = sha(wheel)
    frozen, current = work / "frozen-release-source", work / "candidate-source"
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("matpilot/") and name.endswith(".py"):
                target = (frozen / name).resolve()
                target.relative_to(frozen.resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    source_identity = {}
    for original in sorted((ROOT / "src/matpilot").rglob("*.py")):
        relative = original.relative_to(ROOT / "src")
        target = current / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original.read_bytes())
        source_identity[relative.as_posix()] = sha(target)
    if any(sha(ROOT / "src" / relative) != digest for relative, digest in source_identity.items()):
        raise ValueError("Current source changed while copying.")
    sys.path.insert(0, str(current))
    from matpilot.structures import demo_archive
    fixture = work / "synthetic-nacl.zip"
    demo_archive(fixture, 1)
    versions = {}
    for name, source in (("frozen_release", frozen), ("candidate_source", current)):
        env = {key: value for key, value in os.environ.items() if key.upper() in
               {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME"}}
        env.update(PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        completed = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--child", "--work-dir", str(work / name),
            "--fixture", str(fixture), "--source", str(source)], env=env, capture_output=True, text=True,
            encoding="utf-8", timeout=60, check=True)
        versions[name] = json.loads(completed.stdout)
    before, after = versions.values()
    for field in ("fixture_sha256", "original_cif_sha256", "synthetic_metric_raw_sha256"):
        if before[field] != after[field]:
            raise ValueError("Comparison arms did not receive identical synthetic evidence: " + field)
    if sha(wheel) != wheel_hash:
        raise ValueError("Frozen input release changed.")
    if any(sha(ROOT / "src" / relative) != digest for relative, digest in source_identity.items()):
        raise ValueError("Current source changed during the comparison.")
    report = {"format": "matpilot-metric-screening-cpu-comparison/v1",
        "scope": "production_service_and_registered_mcp_function_capabilities_only",
        "baseline": {"path": wheel.relative_to(ROOT).as_posix(), "sha256": wheel_hash, "release_unchanged": True},
        "versions": versions, "environment": {"python": sys.version.split()[0], "platform": sys.platform,
            "pymatgen": importlib.metadata.version("pymatgen"), "mcp": importlib.metadata.version("mcp")},
        "real_material_model_runs": 0, "property_calculations": 0, "api_requests": 0, "api_cny": 0,
        "limitations": ["All structure and metric values are synthetic CPU fixtures, not scientific evidence.",
            "Both isolated stores receive identical ZIP/CIF/raw metric byte hashes. Random identifiers and timestamps are not compared across runs.",
            "Explicit pass/fail applies only to the selected method/metric; adverse measurements stay in the evidence, and default multiple-value checks remain unknown.",
            "This measures deterministic tool functionality, not AI accuracy, causal AI benefit, general benchmark scores or user-time savings.",
            "Actual Service and registered MCP functions are called. This is not external Harness, MCP transport, or scientific model acceptance.",
            "Candidate counts and sampling charges must remain unchanged by screening/report operations."],
        "reproduction": {"script_path": "benchmarks/compare_metric_screening.py", "script_sha256": sha(__file__),
            "command": "python benchmarks/compare_metric_screening.py --baseline-wheel " + wheel.relative_to(ROOT).as_posix()
                + " --work-dir .work/metric-screening-recompute --output artifacts/metric-screening-recompute.json"}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"output": output.relative_to(ROOT).as_posix(), "sha256": sha(output),
        "before": before["cases"], "after": after["cases"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
