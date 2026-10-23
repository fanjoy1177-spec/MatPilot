"""Freeze five developer-seen context cases for CPU tool-contract checks only.

The candidate source remains pending review. This command deliberately labels its
derived local suite development and never creates a formal review attestation.
The existing eight bridge checks remain prerequisites for exporting the runtime.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys

from prepare_development import ROOT, hash_file, prepare, save


CONTEXT_IDS = ("T0502", "T0505", "T0603", "T0605", "T0606")


def prepare_context(output, *, python_path=None, node_path=None, host_version="0.2.1-rc.2"):
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    inputs = output.with_name(output.name + "-inputs")
    inputs.mkdir(parents=True, exist_ok=False)
    task_path = ROOT / "benchmarks/study/test_candidates.draft.v2.json"
    fixture_path = ROOT / "benchmarks/study/fixtures.candidates.draft.v2.json"
    source_suite = json.loads(task_path.read_text(encoding="utf-8"))
    source_fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    by_id = {task["id"]: task for task in source_suite["tasks"]}
    selected = [copy.deepcopy(by_id[task_id]) for task_id in CONTEXT_IDS]
    suite = {"schema_version": 1, "split": "development", "tasks": selected,
             "exposure": "Developer-seen candidate draft copied for CPU tool contracts, not a formal or unseen evaluation.",
             "independent_review": "pending", "formal_ready": False,
             "source_candidate_suite_sha256": hash_file(task_path)}
    # Retain the old development fixtures for the existing two-engine bridge
    # smoke, then add exact selected facts without adding task_bindings/probes.
    manifest = json.loads((ROOT / "benchmarks/study/fixtures.development.json").read_text(encoding="utf-8"))
    for task in selected:
        fixture_id = task["fixture_id"]
        if fixture_id in manifest["fixtures"]:
            raise ValueError("Context fixture identity collides with an existing development fixture.")
        manifest["fixtures"][fixture_id] = copy.deepcopy(source_fixture["fixtures"][fixture_id])
    manifest.update(purpose="development", review_status="pending_independent_materials_review",
                    source_candidate_fixture_sha256=hash_file(fixture_path),
                    notes=["Developer-only merged CPU fixture set; no formal attestation or AI score.",
                           "Selected candidate fixture definitions are copied unchanged; no task answers are exposed by the runtime."])
    save(inputs / "suite.json", suite)
    save(inputs / "fixtures.json", manifest)
    save(inputs / "provenance.json", {"formal_ready": False, "task_ids": list(CONTEXT_IDS),
         "candidate_suite_sha256": hash_file(task_path), "candidate_fixture_sha256": hash_file(fixture_path),
         "suite_sha256": hash_file(inputs / "suite.json"), "fixtures_sha256": hash_file(inputs / "fixtures.json"),
         "scope": "development_cpu_tool_contract_only_not_an_ai_experiment"})
    return prepare(output, provider="cpu-only-no-provider", model="not-invoked", reasoning_mode="not-invoked",
                   host_version=host_version, python_path=python_path or sys.executable, node_path=node_path,
                   repeats=1, groups=["U", "U+", "M"], task_ids=list(CONTEXT_IDS),
                   suite_path=inputs / "suite.json", fixture_manifest_path=inputs / "fixtures.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    parser.add_argument("--python-path")
    parser.add_argument("--node-path")
    parser.add_argument("--host-version", default="0.2.1-rc.2")
    print(json.dumps(prepare_context(**vars(parser.parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
