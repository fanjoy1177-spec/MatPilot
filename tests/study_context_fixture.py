"""Small explicit CPU-only context fixture shared by broker/checking tests."""
import copy
import json
from pathlib import Path


def write_context_fixture(folder, *, tampered=False, stopped=False):
    source = Path(__file__).resolve().parents[1] / "benchmarks/study/fixtures.development.json"
    manifest = json.loads(source.read_text(encoding="utf-8"))
    fixture = copy.deepcopy(manifest["fixtures"]["development_candidate_evidence_0"])
    fixture["initial_attempts"] = len(fixture["initial_candidates"])
    fixture["sampling_cap"] = fixture["initial_attempts"]
    fixture["initial_outcomes"] = [{"attempt": index, "candidate_id": candidate_id, "error": None}
                                   for index, candidate_id in enumerate(fixture["initial_candidates"], 1)]
    candidate = fixture["candidates"]["c_a"]
    candidate["version_key"] = "s_a1"
    child = copy.deepcopy(candidate)
    child.update(version_key="s_a2", parent_version_key="s_a1", transformation="synthetic_relaxation")
    child["structure"]["lattice"] = [[5, 0, 0], [0, 5, 0], [0, 0, 5]]
    child["metrics"][0]["value"] = 1.5
    candidate["versions"] = [child]
    fixture["database_queries"] = [{"query_id": "database_failure", "database": "synthetic_materials_db",
        "query": {"chemical_system": "Na-Cl"}, "error": {"code": "timeout", "message": "Synthetic database timeout"},
        "provenance": copy.deepcopy(fixture["provenance"])}]
    fixture["execution"] = {"job_id": "synthetic_job", "state": "stopped" if stopped else "running",
        "observed_at": 100, "heartbeat_at": 10, "heartbeat_timeout_seconds": 30,
        "termination": {"confirmed": stopped, "reason": "stop_requested" if stopped else "unconfirmed"},
        "provenance": copy.deepcopy(fixture["provenance"])}
    if tampered:
        fixture["artifact_observations"] = [{"candidate_id": "c_b", "version_key": "initial",
            "observed_text": "Synthetic changed bytes, not a valid CIF", "provenance": copy.deepcopy(fixture["provenance"])}]
    manifest["fixtures"] = {"cpu_context": fixture}
    output = Path(folder) / "context-fixtures.json"
    output.write_text(json.dumps(manifest), encoding="utf-8")
    return output
