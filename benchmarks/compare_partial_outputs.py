"""Recompute a CPU-only dev5/candidate production-worker regression comparison.

No provider, model import, checkpoint, GPU or scientific inference is involved.
The frozen release wheel is only read. Both arms receive identical synthetic ZIP
bytes; the external-failure case uses the same explicit standard-library stub.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CASES = ("short_return", "one_invalid_cif", "nonzero_exit_complete_archive")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def child(workspace, fixtures):
    import matpilot
    from matpilot.config import Settings
    from matpilot.service import Service
    from matpilot.worker import execute_job
    workspace.mkdir(parents=True)
    program = workspace / "cpu_fixture.py"
    program.write_text("import pathlib,shutil,sys\nshutil.copyfile(sys.argv[1],pathlib.Path(sys.argv[2])/'generated_crystals_cif.zip')\nsys.exit(7)\n", encoding="utf-8")
    results = []
    for case in CASES:
        service = Service(Settings(data_dir=workspace / case))
        plan = service.prepare_plan({"model_id": "mattergen", "conditions": {"chemical_system": "Na-Cl"},
                                     "num_samples": 2, "batch_size": 2})
        assert plan["state"] == "ready", plan["issues"]
        settings = service.settings.snapshot()
        fixture = fixtures / (case + ".zip")
        if case == "nonzero_exit_complete_archive":
            settings.update(backend="native", real_compute_enabled=True,
                            command=[sys.executable, str(program), str(fixture)])
            adapter = Mock(build_command=lambda p, output, count, seed, conf:
                           [sys.executable, str(program), str(fixture), conf.bridge_staging_dir])
            context = patch("matpilot.worker.ADAPTERS", {"mattergen": adapter})
        else:
            context = patch("matpilot.worker.demo_archive", side_effect=lambda path, count: shutil.copyfile(fixture, path))
        job, _ = service.store.create_job(plan, "comparison-" + case, settings["backend"], settings)
        claimed = service.store.claim(os.getpid())
        with context:
            execute_job(service.store, claimed)
        job = service.get_job(job["job_id"])
        records = service.get_results(job["job_id"])["records"]
        try:
            service.store.control(job["job_id"], "resume")
            resume = {"allowed": True, "reason": None}
        except ValueError as exc:
            resume = {"allowed": False, "reason": str(exc)}
        results.append({"case": case, "fixture_sha256": sha(fixture), "requested_samples": 2,
                        "retained_candidates": len(records), "sampling_attempts_charged": job["sampling_attempts_charged"],
                        "sampling_budget_remaining": job["sampling_budget_remaining"], "job_status": job["status"],
                        "batch_status": job["batches"][0]["status"], "can_resume": resume,
                        "retained_artifact_sha256": [r["sha256"] for r in records],
                        "error": job.get("error"), "collection_errors": job["batches"][0].get("collection_errors", [])})
    package = Path(matpilot.__file__).resolve().parent
    return {"declared_version": matpilot.__version__, "cases": results,
            "source_sha256": {p.relative_to(package).as_posix(): sha(p) for p in sorted(package.rglob("*.py"))}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default=".work/partial-output-comparison-v06")
    parser.add_argument("--output", default="artifacts/v06-partial-output-comparison.json")
    parser.add_argument("--baseline-wheel", help="Explicit frozen release wheel; this input is read only.")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--fixtures")
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(Path(args.work_dir), Path(args.fixtures)), ensure_ascii=False))
        return
    if os.name != "nt":
        raise ValueError("This declared external-process comparison uses Windows CPU-only adapter substitution; run on Windows.")
    if not args.baseline_wheel:
        raise ValueError("Provide --baseline-wheel with the exact frozen release input.")
    work = (ROOT / args.work_dir).resolve()
    work.relative_to(ROOT)
    output = (ROOT / args.output).resolve()
    output.relative_to(ROOT)
    if output.exists():
        raise FileExistsError(output)
    work.mkdir(parents=True, exist_ok=False)
    wheel = (ROOT / args.baseline_wheel).resolve()
    wheel.relative_to(ROOT)
    wheel_hash = sha(wheel)
    frozen = work / "frozen-dev5"
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if not name.startswith("matpilot/") or not name.endswith(".py"):
                continue
            target = (frozen / name).resolve()
            target.relative_to(frozen.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    current = work / "candidate-source"
    copied = []
    for original in sorted((ROOT / "src/matpilot").rglob("*.py")):
        target = current / original.relative_to(ROOT / "src")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original.read_bytes())
        copied.append((original, sha(target)))
    if any(sha(path) != expected for path, expected in copied):
        raise ValueError("Candidate source changed while copying.")
    sys.path.insert(0, str(current))
    from matpilot.structures import demo_archive
    fixtures = work / "fixtures"
    fixtures.mkdir()
    demo_archive(fixtures / "short_return.zip", 1)
    demo_archive(fixtures / "nonzero_exit_complete_archive.zip", 2)
    with zipfile.ZipFile(fixtures / "short_return.zip") as archive:
        cif = archive.read(archive.namelist()[0])
    with zipfile.ZipFile(fixtures / "one_invalid_cif.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("valid.cif", cif)
        archive.writestr("invalid.cif", "SYNTHETIC INVALID CIF FOR SOFTWARE TEST")
    versions = {}
    for name, source in (("frozen_dev5", frozen), ("candidate_source", current)):
        env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME"}}
        env.update(PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--child", "--work-dir",
                                 str(work / name), "--fixtures", str(fixtures)], capture_output=True, text=True,
                                encoding="utf-8", timeout=60, env=env, check=True)
        versions[name] = json.loads(result.stdout)
    assert sha(wheel) == wheel_hash
    comparisons = []
    for before, after in zip(versions["frozen_dev5"]["cases"], versions["candidate_source"]["cases"]):
        assert before["case"] == after["case"] and before["fixture_sha256"] == after["fixture_sha256"]
        comparisons.append({"case": before["case"], "before": before, "after": after,
                            "additional_retained_candidates": after["retained_candidates"] - before["retained_candidates"],
                            "sampling_charge_change": after["sampling_attempts_charged"] - before["sampling_attempts_charged"]})
    report = {"format": "matpilot-partial-output-cpu-comparison/v1", "scope": "production_worker_cpu_fixture_regression_only",
              "baseline": {"path": wheel.relative_to(ROOT).as_posix(), "sha256": wheel_hash,
                           "release_unchanged": True}, "versions": versions, "comparisons": comparisons,
              "environment": {"python": sys.version.split()[0], "platform": sys.platform,
                              "pymatgen": importlib.metadata.version("pymatgen")},
              "real_material_model_runs": 0, "api_requests": 0, "api_cny": 0,
              "limitations": ["All archives are synthetic fixed NaCl workflow data, not model outputs or material evidence.",
                              "Short/bad CIF cases use production worker with its demo archive producer replaced by identical bytes in both arms.",
                              "Nonzero-exit case substitutes the adapter command with the same standard-library CPU fixture in both arms; it tests worker retention, not model/adapter inference.",
                              "This is a deterministic regression comparison, not an LLM effect, correctness rate, user-time or generalization experiment.",
                              "Candidate source identity is the copied Python file hashes, even if a later package version label changes."],
              "reproduction": {"script_path": "benchmarks/compare_partial_outputs.py", "script_sha256": sha(__file__),
                               "script_source": Path(__file__).read_text(encoding="utf-8"),
                               "command": "python benchmarks/compare_partial_outputs.py --baseline-wheel " + wheel.relative_to(ROOT).as_posix() + " --work-dir .work/partial-comparison-recompute --output artifacts/partial-comparison-recompute.json"}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"output": output.relative_to(ROOT).as_posix(), "sha256": sha(output), "comparisons": comparisons}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
