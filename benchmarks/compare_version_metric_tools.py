"""Recompute frozen-release/current production metric-tool CPU capabilities.

Both isolated SQLite stores receive the same fixed NaCl ZIP and explicitly
synthetic metric bytes. No model, property calculation, network or LLM is run.
This measures tool functionality, never AI accuracy or user time savings.
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
    from matpilot.server import make_server
    from matpilot.service import Service
    from matpilot.worker import execute_job
    package = Path(matpilot.__file__).resolve().parent
    package.relative_to(source.resolve())
    work.mkdir(parents=True, exist_ok=False)
    settings = Settings(data_dir=work / "data", backend="demo", real_compute_enabled=False)
    service = Service(settings)
    plan = service.prepare_plan({"conditions": {"chemical_system": "Na-Cl"}, "num_samples": 1, "batch_size": 1})
    assert plan["state"] == "ready", plan["issues"]
    job, _ = service.store.create_job(plan, "synthetic-metric-comparison", "demo", settings.snapshot())
    with patch("matpilot.worker.demo_archive", side_effect=lambda path, count: shutil.copyfile(fixture, path)):
        execute_job(service.store, service.store.claim(os.getpid()))
    original = service.get_results(job["job_id"])["records"][0]
    transformed = work / "synthetic-child.cif"
    transformed.write_text("# SYNTHETIC serialization transform; no relaxation performed\n" + Path(original["path"]).read_text(), encoding="utf-8")
    newer = service.store.add_structure_version(original["structure_version_id"], transformed,
        "synthetic_serialization_change", {"synthetic": True, "science_computation_performed": False})
    metrics = []
    for index, (version, value, method) in enumerate(((original, 1.1, "a"), (newer, 1.6, "a"), (newer, 0.5, "b"))):
        raw = work / f"synthetic-metric-{index}.json"
        raw.write_text(json.dumps({"synthetic": True, "value": value, "method": method, "science_computation_performed": False}, sort_keys=True), encoding="utf-8")
        metrics.append(service.store.record_metric({"candidate_id": version["candidate_id"],
            "structure_version_id": version["structure_version_id"], "property": "dft_band_gap", "value": value, "unit": "eV",
            "method": {"name": "synthetic-method-" + method, "version": "synthetic-v1", "weights_identity": "synthetic-no-weights", "parameters": {"synthetic": True}},
            "input_artifact": {"path": version["path"], "sha256": version["sha256"]},
            "raw_output": {"path": str(raw), "sha256": sha(raw)}, "convergence": "not_applicable", "reference_set": None}))
    result = service.get_results(job["job_id"])
    # Call the actual registered production functions, without requiring an
    # event-loop/network transport. Protocol transport has separate MCP tests.
    app = make_server(settings)
    tools = {tool.name: tool for tool in app._tool_manager.list_tools()}
    mcp_result = tools["get_results"].fn(job_id=job["job_id"])
    supports_selection = "metric_selections" in inspect.signature(service.compare_runs).parameters
    cases = {"version_metrics_read": {
        "generated_candidate_count": result["count"],
        "registered_version_count": len(result["structure_versions"][original["candidate_id"]]),
        "versions_with_readable_metrics": sum(bool(values) for values in result.get("metrics_by_structure_version", {}).values()),
        "readable_metric_count": sum(len(values) for values in result.get("metrics_by_structure_version", {}).values()),
        "mcp_registered_function_exposes_metrics": "metrics_by_structure_version" in mcp_result}}
    for name, chosen in (("single_job_same_method", [metrics[0], metrics[1]]),
                         ("single_job_different_methods", [metrics[0], metrics[2]])):
        selections = [{"job_id": job["job_id"], **{k: metric[k] for k in
            ("candidate_id", "structure_version_id", "metric_id")}} for metric in chosen]
        try:
            compared = service.compare_runs([job["job_id"]], metric_selections=selections)
            cases[name] = {"supported": True, "comparable": compared["metric_comparison"]["comparable"],
                           "reasons": compared["metric_comparison"]["reasons"],
                           "selected_values": [metric["value"] for metric in compared["selected_metrics"]],
                           "ranking_returned": "ranking" in compared or "ranking" in compared["metric_comparison"]}
        except (TypeError, ValueError) as exc:
            cases[name] = {"supported": supports_selection, "error_type": type(exc).__name__, "error": str(exc), "ranking_returned": False}
    screened = service.screen_candidates(job["job_id"])
    return {"declared_version": matpilot.__version__, "source_sha256": {
        path.relative_to(package).as_posix(): sha(path) for path in sorted(package.rglob("*.py"))},
        "tool_count": len(tools), "compare_runs_input_schema": tools["compare_runs"].parameters,
        "cases": cases, "fixture_sha256": sha(fixture),
        "original_cif_sha256": original["sha256"], "child_cif_sha256": newer["sha256"],
        "synthetic_metric_raw_sha256": [metric["raw_output"]["sha256"] for metric in metrics],
        "default_screened_candidate_count": screened["count"], "sampling_attempts_charged": service.get_job(job["job_id"])["sampling_attempts_charged"]}


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
    for field in ("fixture_sha256", "original_cif_sha256", "child_cif_sha256", "synthetic_metric_raw_sha256"):
        if before[field] != after[field]:
            raise ValueError("Comparison arms did not receive identical synthetic evidence: " + field)
    if sha(wheel) != wheel_hash:
        raise ValueError("Frozen input release changed.")
    report = {"format": "matpilot-version-metric-tools-cpu-comparison/v1",
        "scope": "production_service_and_registered_mcp_function_capabilities_only",
        "baseline": {"path": wheel.relative_to(ROOT).as_posix(), "sha256": wheel_hash, "release_unchanged": True},
        "versions": versions, "environment": {"python": sys.version.split()[0], "platform": sys.platform,
            "pymatgen": importlib.metadata.version("pymatgen"), "mcp": importlib.metadata.version("mcp")},
        "real_material_model_runs": 0, "property_calculations": 0, "api_requests": 0, "api_cny": 0,
        "limitations": ["All structures, transformations and metrics are explicitly synthetic CPU fixtures, not scientific evidence.",
            "The same ZIP/CIF/raw metric byte hashes are checked across isolated stores and source processes.",
            "This is tool functionality, not an LLM success rate, general benchmark score, causal AI benefit or user-time measurement.",
            "Mixed-method numerical rankings are never requested or produced; rejection of comparability is the supported behavior.",
            "The comparison invokes actual Service and registered MCP functions; it does not claim external Harness or protocol deployment acceptance.",
            "The current source snapshot is identified by its file hashes, even if its package version label has not yet been changed."],
        "reproduction": {"script_path": "benchmarks/compare_version_metric_tools.py", "script_sha256": sha(__file__),
            "command": "python benchmarks/compare_version_metric_tools.py --baseline-wheel " + wheel.relative_to(ROOT).as_posix()
                + " --work-dir .work/version-metrics-recompute --output artifacts/version-metrics-recompute.json"}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"output": output.relative_to(ROOT).as_posix(), "sha256": sha(output),
        "before": before["cases"], "after": after["cases"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
