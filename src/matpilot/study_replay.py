"""Shared, condition-bound CPU replay for workflow studies, never model inference.

This module deliberately implements no U/U+/M agent. Those drivers must retain
their real upstream functions and instructions and intercept only the scientific
engine boundary. A fixture is an ordered stream of attempts, not a generic
success response. Different batch partitions consume the same stream prefix.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
import sys
import threading
from typing import Any

from .adapters.mattergen import canonical_hash
from .evidence import validate_metric, verify_artifact


VERSION = "study-replay-v1"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SCOPE = "workflow_engineering_only_not_material_property_evidence"
CONTEXT_CONTRACT = "matpilot-shared-replay-context/v2"


class ReplayError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _write_new(path: Path, value: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(value)
    return _bytes_hash(value)


def _positive_int(value: Any, label: str, *, zero: bool = False) -> int:
    if type(value) is not int or value < (0 if zero else 1):
        raise ReplayError("invalid_request", f"{label} must be an integer >= {0 if zero else 1}.")
    return value


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ReplayError("invalid_manifest", f"Invalid {label}.")
    return value


def request_identity(arguments: dict) -> dict:
    """Bind scientific inputs; normalize key/element order and equal JSON numbers.

    Units, checkpoint names, target/bound semantics, symmetry and parameters are
    not inferred or repaired here. Drivers supply actual engine inputs.
    """
    if not isinstance(arguments, dict) or set(arguments) != {"engine", "model_name", "conditions", "parameters"}:
        raise ReplayError("invalid_request", "Route requires engine, model_name, conditions and parameters exactly.")
    result = copy.deepcopy(arguments)
    if result["engine"] not in {"mattergen", "diffcsp"} or not isinstance(result["model_name"], str) or not result["model_name"]:
        raise ReplayError("invalid_request", "Unknown engine or missing model_name.")
    if not isinstance(result["conditions"], dict) or not isinstance(result["parameters"], dict):
        raise ReplayError("invalid_request", "conditions and parameters must be objects.")
    chemical = result["conditions"].get("chemical_system")
    if chemical is not None:
        from pymatgen.core import Element
        if not isinstance(chemical, str):
            raise ReplayError("invalid_request", "chemical_system must be an element-set string.")
        elements = chemical.split("-")
        try:
            if len(set(elements)) != len(elements) or not all(Element.is_valid_symbol(x) for x in elements):
                raise ValueError()
        except (ValueError, TypeError):
            raise ReplayError("invalid_request", "Invalid or repeated chemical_system elements.") from None
        result["conditions"]["chemical_system"] = "-".join(sorted(elements))
    try:
        canonical_hash(result)
    except (ValueError, TypeError):
        raise ReplayError("invalid_request", "Inputs must be finite JSON values.") from None

    def equal_number_form(value):
        # JSON serializers may render the same scientific value as 2 or 2.0.
        # Exact integral floats normalize; booleans, units, strings and
        # nonintegral values remain distinct. This never rounds real targets.
        if type(value) is float and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {key: equal_number_form(item) for key, item in value.items()}
        if isinstance(value, list):
            return [equal_number_form(item) for item in value]
        return value

    return equal_number_form(result)


def _provenance(value: Any, label: str, *, synthetic_only: bool = False) -> None:
    if (not isinstance(value, dict) or value.get("kind") not in ({"synthetic"} if synthetic_only else {"synthetic", "recorded"})
            or not isinstance(value.get("description"), str) or not value["description"].strip()
            or value["kind"] == "recorded" and not value.get("source")):
        raise ReplayError("invalid_manifest", f"{label} needs explicit provenance.")


def _version_definitions(candidate: dict) -> list[dict]:
    root = {**candidate, "version_key": candidate.get("version_key", "initial"),
            "parent_version_key": None, "transformation": "fixture_import"}
    return [root, *candidate.get("versions", [])]


def _validate_version(definition: dict) -> None:
    _provenance(definition.get("provenance"), "Structure version")
    source = definition["provenance"]
    if source["kind"] == "synthetic":
        if set(definition.get("structure", {})) != {"lattice", "species", "frac_coords"}:
            raise ReplayError("invalid_manifest", "Synthetic structures require explicit lattice, species and fractional coordinates.")
    elif (not isinstance(definition.get("cif_text"), str)
          or _bytes_hash(definition["cif_text"].encode()) != definition.get("cif_sha256")):
        raise ReplayError("invalid_manifest", "Recorded CIF requires an exact content hash.")
    metrics = definition.get("metrics", [])
    if not isinstance(metrics, list):
        raise ReplayError("invalid_manifest", "metrics must be an array.")
    for metric in metrics:
        required = {"property", "value", "unit", "method", "convergence", "reference_set", "provenance"}
        if not isinstance(metric, dict) or set(metric) != required:
            raise ReplayError("invalid_manifest", "Metric evidence/provenance schema is incomplete.")
        _provenance(metric["provenance"], "Metric")
        value = metric["value"]
        if value is not None and (type(value) not in {int, float} or not math.isfinite(value)):
            raise ReplayError("invalid_manifest", "Metric value must be finite or unknown.")


def _validate_manifest(manifest: dict) -> None:
    if manifest.get("schema_version") != 1 or manifest.get("evidence_scope") != SCOPE:
        raise ReplayError("invalid_manifest", "Explicit version and non-scientific evidence scope required.")
    if manifest.get("purpose") not in {"development", "formal_workflow_study"}:
        raise ReplayError("invalid_manifest", "Unknown fixture purpose.")
    if not isinstance(manifest.get("author"), str) or not manifest["author"].strip():
        raise ReplayError("invalid_manifest", "Fixture author is required.")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, dict) or not fixtures:
        raise ReplayError("invalid_manifest", "At least one fixture is required.")
    for fixture_id, fixture in fixtures.items():
        _safe_id(fixture_id, "fixture_id")
        cap = _positive_int(fixture.get("sampling_cap"), "sampling_cap", zero=True)
        used = _positive_int(fixture.get("initial_attempts", 0), "initial_attempts", zero=True)
        if used > cap:
            raise ReplayError("invalid_manifest", "Initial attempts exceed the cap.")
        provenance = fixture.get("provenance", {})
        if provenance.get("kind") not in {"synthetic", "recorded"} or not provenance.get("description"):
            raise ReplayError("invalid_manifest", "Every fixture needs explicit synthetic/recorded provenance.")
        if provenance["kind"] == "recorded" and not provenance.get("source"):
            raise ReplayError("invalid_manifest", "Recorded fixtures require their source identity.")
        routes = fixture.get("routes", {})
        if not isinstance(routes, dict):
            raise ReplayError("invalid_manifest", "routes must be an object.")
        route_hashes = set()
        for route_id, route in routes.items():
            _safe_id(route_id, "route_id")
            route_hash = canonical_hash(request_identity(route))
            if route_hash in route_hashes:
                raise ReplayError("invalid_manifest", "Duplicate semantic routes.")
            route_hashes.add(route_hash)
        candidates = fixture.get("candidates", {})
        if not isinstance(candidates, dict):
            raise ReplayError("invalid_manifest", "candidates must be an object.")
        for candidate_id, candidate in candidates.items():
            _safe_id(candidate_id, "candidate_id")
            if not isinstance(candidate.get("versions", []), list):
                raise ReplayError("invalid_manifest", "versions must be an ordered array.")
            seen_versions = set()
            for index, definition in enumerate(_version_definitions(candidate)):
                key = _safe_id(definition.get("version_key"), "version_key")
                if key in seen_versions or (index and definition.get("parent_version_key") not in seen_versions):
                    raise ReplayError("invalid_manifest", "Versions need unique keys and an earlier parent in the same candidate.")
                if index and (not isinstance(definition.get("transformation"), str) or not definition["transformation"].strip()):
                    raise ReplayError("invalid_manifest", "Child versions require an explicit transformation.")
                _validate_version(definition)
                seen_versions.add(key)
            if candidate.get("current_version_key", candidate.get("version_key", "initial")) not in seen_versions:
                raise ReplayError("invalid_manifest", "Current version must reference a declared version.")
        initial = fixture.get("initial_candidates", [])
        if not isinstance(initial, list) or len(set(initial)) != len(initial) or any(x not in candidates for x in initial):
            raise ReplayError("invalid_manifest", "Invalid initial candidate references.")
        if "initial_outcomes" in fixture:
            outcomes = fixture["initial_outcomes"]
            if not isinstance(outcomes, list) or len(outcomes) != used:
                raise ReplayError("invalid_manifest", "Initial outcomes must account for every existing attempt.")
            exposed = set()
            for index, outcome in enumerate(outcomes, 1):
                if not isinstance(outcome, dict) or set(outcome) != {"attempt", "candidate_id", "error"} or type(outcome["attempt"]) is not int or outcome["attempt"] != index:
                    raise ReplayError("invalid_manifest", "Initial outcomes require contiguous exact attempt identities.")
                candidate, error = outcome["candidate_id"], outcome["error"]
                if candidate is not None:
                    if candidate not in initial or candidate in exposed:
                        raise ReplayError("invalid_manifest", "Initial outcome candidate is missing or repeated.")
                    exposed.add(candidate)
                if error is not None and (not isinstance(error, dict) or set(error) != {"code", "message"} or not all(isinstance(x, str) and x for x in error.values())):
                    raise ReplayError("invalid_manifest", "Initial failure requires a code and message.")
                if candidate is None and error is None:
                    raise ReplayError("invalid_manifest", "Initial outcome cannot be empty.")
            if exposed != set(initial):
                raise ReplayError("invalid_manifest", "Initial outcomes must explain every available candidate.")
        execution = fixture.get("execution")
        if execution is not None:
            if "initial_outcomes" not in fixture:
                raise ReplayError("invalid_manifest", "Execution context requires a complete initial attempt ledger, including an explicit empty ledger.")
            required = {"job_id", "state", "observed_at", "heartbeat_at", "heartbeat_timeout_seconds", "termination", "provenance"}
            if not isinstance(execution, dict) or set(execution) != required:
                raise ReplayError("invalid_manifest", "Execution context requires its complete observation schema.")
            _safe_id(execution["job_id"], "job_id")
            _provenance(execution["provenance"], "CPU execution observation", synthetic_only=True)
            if execution["state"] not in {"running", "stopped", "partial", "failed", "completed"}:
                raise ReplayError("invalid_manifest", "Unknown observed execution state.")
            for key in ("observed_at", "heartbeat_at", "heartbeat_timeout_seconds"):
                _positive_int(execution[key], key, zero=key != "heartbeat_timeout_seconds")
            if execution["heartbeat_at"] > execution["observed_at"]:
                raise ReplayError("invalid_manifest", "Heartbeat cannot follow the fixed observation clock.")
            termination = execution["termination"]
            if (not isinstance(termination, dict) or set(termination) != {"confirmed", "reason"}
                    or type(termination["confirmed"]) is not bool
                    or termination["reason"] not in {"stop_requested", "command_exited", "unconfirmed"}
                    or termination["confirmed"] != (termination["reason"] != "unconfirmed")
                    or execution["state"] == "stopped" and termination != {"confirmed": True, "reason": "stop_requested"}
                    or execution["state"] == "running" and termination["confirmed"]
                    or execution["state"] != "running" and not termination["confirmed"]
                    or execution["state"] in {"completed", "partial", "failed"} and termination["reason"] != "command_exited"):
                raise ReplayError("invalid_manifest", "Execution state and termination evidence contradict each other.")
            # A cap is only an upper bound, not the requested completion count.
            # Check the evidence we do have; never infer failure merely because
            # the observed job used less than its maximum sampling allowance.
            outcomes = fixture["initial_outcomes"]
            failed_attempts = any(outcome["error"] is not None for outcome in outcomes)
            if (execution["state"] == "completed" and failed_attempts
                    or execution["state"] == "failed" and initial
                    or execution["state"] == "partial" and not initial):
                raise ReplayError("invalid_manifest", "Execution terminal state contradicts its attempt outcomes and archived candidates.")
        queries = fixture.get("database_queries", [])
        if not isinstance(queries, list):
            raise ReplayError("invalid_manifest", "database_queries must be an array.")
        query_ids, query_hashes = set(), set()
        for query in queries:
            if not isinstance(query, dict) or set(query) != {"query_id", "database", "query", "error", "provenance"}:
                raise ReplayError("invalid_manifest", "Database failure requires an exact request and error schema.")
            query_id = _safe_id(query["query_id"], "query_id")
            if not isinstance(query["database"], str) or not query["database"] or not isinstance(query["query"], dict):
                raise ReplayError("invalid_manifest", "Database request needs a name and query object.")
            digest = canonical_hash({"database": query["database"], "query": query["query"]})
            if query_id in query_ids or digest in query_hashes:
                raise ReplayError("invalid_manifest", "Database requests and IDs must be unique.")
            query_ids.add(query_id)
            query_hashes.add(digest)
            _provenance(query["provenance"], "CPU database observation", synthetic_only=True)
            if not isinstance(query["error"], dict) or set(query["error"]) != {"code", "message"} or not all(isinstance(x, str) and x for x in query["error"].values()):
                raise ReplayError("invalid_manifest", "Database failure needs a nonempty code and message.")
        observations = fixture.get("artifact_observations", [])
        if not isinstance(observations, list):
            raise ReplayError("invalid_manifest", "artifact_observations must be an array.")
        observed = set()
        for observation in observations:
            keys = set(observation) if isinstance(observation, dict) else set()
            content_keys = keys & {"observed_text", "observed_append_text"}
            if len(content_keys) != 1 or keys != {"candidate_id", "version_key", "provenance", *content_keys}:
                raise ReplayError("invalid_manifest", "Artifact observation requires a version and exactly one byte-content instruction.")
            candidate_id, key = observation["candidate_id"], observation["version_key"]
            if (candidate_id not in initial or key not in {v["version_key"] for v in _version_definitions(candidates[candidate_id])}
                    or (candidate_id, key) in observed):
                raise ReplayError("invalid_manifest", "Artifact observations must uniquely bind initially available versions.")
            observed.add((candidate_id, key))
            _provenance(observation["provenance"], "CPU artifact observation", synthetic_only=True)
            value = observation[next(iter(content_keys))]
            if not isinstance(value, str) or len(value.encode()) > 4 * 1024 * 1024:
                raise ReplayError("invalid_manifest", "Observed artifact text must fit the 4 MiB fixture limit.")
        frames = fixture.get("attempts", [])
        if not isinstance(frames, list) or len(frames) > cap - used:
            raise ReplayError("invalid_manifest", "Attempt stream exceeds the approved remaining budget.")
        seen = set(initial)
        for frame in frames:
            if set(frame) != {"route_id", "candidate_id", "error"} or frame["route_id"] not in routes:
                raise ReplayError("invalid_manifest", "Attempt frame must bind a declared route.")
            candidate = frame["candidate_id"]
            error = frame["error"]
            if candidate is not None:
                if candidate not in candidates or candidate in seen:
                    raise ReplayError("invalid_manifest", "Attempt candidates must be declared and uniquely identified.")
                seen.add(candidate)
            if error is not None and (not isinstance(error, dict) or set(error) != {"code", "message"} or not all(isinstance(v, str) and v for v in error.values())):
                raise ReplayError("invalid_manifest", "Injected failures need a code and message.")
            if candidate is None and error is None:
                raise ReplayError("invalid_manifest", "An attempt must expose a candidate or an explicit failure.")


class ReplaySession:
    """One isolated, non-resumable session. Outputs and events are create-only.

    Formal review is externally anchored: expected manifest and review hashes
    must come from the frozen study configuration, not fields inside the file.
    Software verifies the attestation's identity and binding; it cannot verify
    that the named human actually performed the independent review.
    """
    def __init__(self, manifest_path: str | Path, fixture_id: str, output_dir: str | Path,
                 run_id: str, sampling_cap: int | None = None, *, formal: bool = False,
                 expected_manifest_sha256: str | None = None,
                 review_path: str | Path | None = None,
                 expected_review_sha256: str | None = None):
        raw = Path(manifest_path).read_bytes()
        self.manifest_sha256 = _bytes_hash(raw)
        if expected_manifest_sha256 and self.manifest_sha256 != expected_manifest_sha256:
            raise ReplayError("manifest_changed", "Manifest byte hash differs from the frozen configuration.")
        self.manifest = json.loads(raw)
        _validate_manifest(self.manifest)
        self.review_sha256 = None
        if formal:
            if not expected_manifest_sha256 or not expected_review_sha256 or not review_path or self.manifest["purpose"] != "formal_workflow_study":
                raise ReplayError("review_required", "Formal replay requires frozen manifest/review hashes and a formal workflow fixture.")
            review_raw = Path(review_path).read_bytes()
            self.review_sha256 = _bytes_hash(review_raw)
            if self.review_sha256 != expected_review_sha256:
                raise ReplayError("review_changed", "Review hash differs from the frozen configuration.")
            review = json.loads(review_raw)
            if (review.get("manifest_sha256") != self.manifest_sha256 or review.get("approved") is not True
                    or review.get("independent_of_implementation") is not True
                    or not all(isinstance(review.get(k), str) and review[k].strip() for k in ("reviewer", "materials_qualification", "reviewed_at", "notes"))
                    or review["reviewer"] == self.manifest["author"]
                    or set(review.get("fixture_ids", [])) != set(self.manifest["fixtures"])):
                raise ReplayError("review_required", "Review must independently attest to this exact manifest and every fixture.")
        if fixture_id not in self.manifest["fixtures"]:
            raise ReplayError("fixture_unavailable", "The requested fixture does not exist.")
        self.fixture_id = fixture_id
        self.fixture = self.manifest["fixtures"][fixture_id]
        self.run_id = _safe_id(run_id, "run_id")
        approved_cap = self.fixture["sampling_cap"]
        self.sampling_cap = approved_cap if sampling_cap is None else min(approved_cap, _positive_int(sampling_cap, "sampling_cap", zero=True))
        self.attempts_used = self.fixture.get("initial_attempts", 0)
        if self.attempts_used > self.sampling_cap:
            raise ReplayError("invalid_request", "Configured cap is below the fixture's existing attempts.")
        self.requested_samples = 0
        self.cursor = 0
        self.event_count = 0
        self.last_event_sha256 = None
        self.lock = threading.Lock()
        self.root = Path(output_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.artifacts: dict[str, dict] = {}
        self.candidates: dict[str, dict] = {}
        self.metrics: dict[str, list] = {}
        self.versions: dict[str, dict] = {}
        self.metrics_by_version: dict[str, list] = {}
        self.context_artifacts: dict[str, dict] = {}
        self.database_results: dict[str, dict] = {}
        self.artifact_observations: dict[str, dict] = {}
        self.routes = {canonical_hash(request_identity(v)): k for k, v in self.fixture.get("routes", {}).items()}
        self.identity = {"backend": VERSION, "manifest_sha256": self.manifest_sha256,
                         "fixture_id": fixture_id, "fixture_sha256": canonical_hash(self.fixture),
                         "review_sha256": self.review_sha256, "formal": formal,
                         "backend_source_sha256": _bytes_hash(Path(__file__).read_bytes()),
                         "runtime": {"python": sys.version.split()[0],
                                     "pymatgen": importlib.metadata.version("pymatgen"),
                                     "structure_serializer": "pymatgen.io.cif.CifWriter"},
                         "provenance": self.fixture["provenance"], "evidence_scope": SCOPE,
                         "science_computation_performed": False}
        self.identity["shared_context_contract"] = CONTEXT_CONTRACT
        _write_new(self.root / "session.json", _json_bytes({**self.identity, "run_id": run_id, "sampling_cap": self.sampling_cap}))
        for candidate_id in self.fixture.get("initial_candidates", []):
            self._materialize(candidate_id)
        self._materialize_context()

    def _artifact(self, artifact_id: str, relative: str, data: bytes, kind: str) -> dict:
        path = self.root / relative
        digest = _write_new(path, data)
        record = {"artifact_id": artifact_id, "path": str(path), "relative_path": relative,
                  "sha256": digest, "kind": kind, "bytes": len(data)}
        self.artifacts[artifact_id] = record
        return record

    def _materialize(self, candidate_id: str) -> dict:
        candidate = self.fixture["candidates"][candidate_id]
        by_key = {}
        for index, definition in enumerate(_version_definitions(candidate)):
            parent = by_key.get(definition["parent_version_key"])
            record = self._materialize_version(candidate_id, definition, parent, index == 0)
            by_key[definition["version_key"]] = record
            self.versions[record["structure_version_id"]] = record
        current = by_key[candidate.get("current_version_key", candidate.get("version_key", "initial"))]
        self.candidates[candidate_id] = current
        self.metrics[candidate_id] = self.metrics_by_version[current["structure_version_id"]]
        return current

    def _materialize_version(self, candidate_id: str, definition: dict, parent: dict | None, root: bool) -> dict:
        from pymatgen.core import Lattice, Structure
        from pymatgen.io.cif import CifWriter
        source = definition["provenance"]
        if source["kind"] == "synthetic":
            data = definition["structure"]
            crystal = Structure(Lattice(data["lattice"]), data["species"], data["frac_coords"])
            cif = ("# SYNTHETIC WORKFLOW FIXTURE: NOT MODEL OUTPUT OR MATERIAL EVIDENCE\n" + str(CifWriter(crystal))).encode()
        else:
            cif = definition["cif_text"].encode()
            crystal = Structure.from_str(cif.decode(), fmt="cif")
        stem = candidate_id if root else f"{candidate_id}--{definition['version_key']}"
        artifact = self._artifact(f"{stem}.cif", f"candidates/{stem}.cif", cif, "structure")
        parent_id = parent["structure_version_id"] if parent else None
        version_id = "s_" + canonical_hash([self.identity["fixture_sha256"], candidate_id, artifact["sha256"], parent_id, definition["transformation"]])
        # Reject before touching the per-version metric registry. A later alias
        # with the same parent, content and transformation is the same version,
        # not permission to replace its already bound scientific evidence.
        if version_id in self.versions:
            raise ReplayError("version_identity_conflict", "Different version declarations resolve to the same immutable structure identity; aliases cannot overwrite metadata or metrics.")
        record = {"candidate_id": candidate_id, "structure_version_id": version_id,
                  "version_key": definition["version_key"], "parent_version_id": parent_id,
                  "transformation": definition["transformation"], **artifact,
                  "formula": crystal.composition.reduced_formula,
                  "elements": sorted(x.symbol for x in crystal.composition.elements),
                  "num_sites": len(crystal), "parse_valid": True,
                  "provenance": source, "evidence_scope": SCOPE}
        self.metrics_by_version[version_id] = []
        for index, definition_metric in enumerate(definition.get("metrics", [])):
            raw = self._artifact(f"{stem}.metric.{index}", f"metrics/{stem}-{index}.json",
                                 _json_bytes({**definition_metric, "candidate_id": candidate_id,
                                              "structure_version_id": version_id, "evidence_scope": SCOPE}), "metric_raw")
            metric = {k: copy.deepcopy(v) for k, v in definition_metric.items() if k != "provenance"}
            metric.update(candidate_id=candidate_id, structure_version_id=version_id,
                          input_artifact={k: artifact[k] for k in ("path", "sha256")},
                          raw_output={k: raw[k] for k in ("path", "sha256")})
            validated = validate_metric(metric, record)
            self.metrics_by_version[version_id].append({**validated, "provenance": definition_metric["provenance"], "evidence_scope": SCOPE})
        return record

    def _materialize_context(self) -> None:
        for key in ("execution", "initial_outcomes"):
            if key in self.fixture:
                self.context_artifacts[key] = self._artifact(f"context.{key}", f"context/{key}.json",
                    _json_bytes({"format": CONTEXT_CONTRACT, key: self.fixture[key], "evidence_scope": SCOPE,
                                 "provenance": self.fixture["provenance"], "science_computation_performed": False}), "execution_context")
        for query in self.fixture.get("database_queries", []):
            request = {k: query[k] for k in ("database", "query")}
            result = {"query_id": query["query_id"], **request, "request_sha256": canonical_hash(request),
                      "status": "failed", "novelty": "unknown", "records": None, "error": query["error"],
                      "provenance": query["provenance"], "evidence_scope": SCOPE,
                      "network_request_performed": False}
            raw = self._artifact(f"database.{query['query_id']}", f"database/{query['query_id']}.json",
                                 _json_bytes(result), "database_failure")
            self.database_results[result["request_sha256"]] = {**result, "raw_output": raw}
        for observation in self.fixture.get("artifact_observations", []):
            record = next(v for v in self.versions.values() if v["candidate_id"] == observation["candidate_id"] and v["version_key"] == observation["version_key"])
            original = {k: record[k] for k in ("artifact_id", "path", "relative_path", "sha256", "kind", "bytes")}
            observed_bytes = (observation["observed_text"].encode() if "observed_text" in observation
                              else Path(original["path"]).read_bytes() + observation["observed_append_text"].encode())
            stem = f"{record['candidate_id']}--{record['version_key']}"
            actual = self._artifact(f"{stem}.observed", f"observations/{stem}.cif", observed_bytes, "structure_observation")
            receipt = self._artifact(f"{stem}.observation", f"observations/{stem}.json",
                _json_bytes({"format": CONTEXT_CONTRACT, "candidate_id": record["candidate_id"],
                             "structure_version_id": record["structure_version_id"], "original_artifact": original,
                             "observed_artifact": actual, "provenance": observation["provenance"], "evidence_scope": SCOPE}), "artifact_observation")
            # Keep expected content identity and version ID. The observed file
            # is a separate immutable artifact, never a silent new structure version.
            record.update(path=actual["path"], relative_path=actual["relative_path"], artifact_id=actual["artifact_id"])
            self.artifact_observations[record["structure_version_id"]] = {
                "original_artifact": original, "observed_artifact": actual,
                "observation_receipt": receipt, "provenance": observation["provenance"]}

    def _selected_version(self, args: dict) -> dict:
        current = self.candidates.get(args["candidate_id"])
        if current is None:
            raise ReplayError("candidate_unavailable", "Candidate does not exist in this session.")
        selected = self.versions.get(args.get("structure_version_id", current["structure_version_id"]))
        if selected is None or selected["candidate_id"] != args["candidate_id"]:
            raise ReplayError("version_mismatch", "Requested candidate and structure version do not match.")
        return selected

    def _integrity(self, record: dict) -> dict:
        path = Path(record["path"])
        actual = _bytes_hash(path.read_bytes()) if path.is_file() else None
        observation = self.artifact_observations.get(record["structure_version_id"], {})
        for key in ("original_artifact", "observation_receipt"):
            if key in observation:
                verify_artifact(observation[key])
        return {"candidate_id": record["candidate_id"], "structure_version_id": record["structure_version_id"],
                "expected_sha256": record["sha256"], "observed_sha256": actual, "intact": actual == record["sha256"],
                "path": str(path), **observation, "evidence_scope": SCOPE}

    def _execution_status(self) -> dict | None:
        execution = self.fixture.get("execution")
        if execution is None:
            return None
        verify_artifact(self.context_artifacts["execution"])
        verify_artifact(self.context_artifacts["initial_outcomes"])
        integrity = [self._integrity(v) for v in self.versions.values()]
        reasons = []
        if execution["state"] not in {"partial", "failed", "stopped"}:
            reasons.append("The observed job is not in a resumable terminal state.")
        if not execution["termination"]["confirmed"]:
            reasons.append("Execution termination has not been confirmed.")
        if self.attempts_used >= self.sampling_cap:
            reasons.append("The hard sampling budget is exhausted, including failed attempts.")
        if any(not value["intact"] for value in integrity):
            reasons.append("An archived structure is missing or changed.")
        return {**execution, "heartbeat_age_seconds": execution["observed_at"] - execution["heartbeat_at"],
                "heartbeat_late": execution["observed_at"] - execution["heartbeat_at"] > execution["heartbeat_timeout_seconds"],
                "termination_confirmed": execution["termination"]["confirmed"],
                "resume_allowed": not reasons, "resume_blockers": reasons,
                "raw_output": self.context_artifacts["execution"], "artifact_integrity": integrity,
                "attempt_ledger_artifact": self.context_artifacts["initial_outcomes"],
                "termination_evidence_scope": "synthetic_observation_not_live_process_supervision",
                "clock_kind": "fixed_synthetic_observation_not_wall_clock", "evidence_scope": SCOPE}

    def _status(self) -> dict:
        result = {**self.identity, "attempts_used": self.attempts_used, "sampling_cap": self.sampling_cap,
                "remaining_budget": self.sampling_cap - self.attempts_used,
                "requested_samples": self.requested_samples,
                "candidate_ids": list(self.candidates), "events_recorded": self.event_count}
        if "execution" in self.fixture:
            result["execution"] = self._execution_status()
        if "initial_outcomes" in self.fixture:
            verify_artifact(self.context_artifacts["initial_outcomes"])
            result["initial_outcomes"] = self.fixture["initial_outcomes"]
            result["initial_outcomes_artifact"] = self.context_artifacts["initial_outcomes"]
        return result

    def _sample(self, args: dict) -> dict:
        if not isinstance(args, dict) or set(args) != {"engine", "model_name", "conditions", "parameters", "num_samples"}:
            raise ReplayError("invalid_request", "sample needs the complete route plus num_samples.")
        count = _positive_int(args["num_samples"], "num_samples")
        self.requested_samples += count
        execution = self._execution_status()
        if execution is not None and not execution["resume_allowed"]:
            raise ReplayError("execution_not_resumable", " ".join(execution["resume_blockers"]))
        if any(not self._integrity(v)["intact"] for v in self.versions.values()):
            raise ReplayError("artifact_changed", "Existing archived structures must match their frozen identity before further attempts.")
        route_hash = canonical_hash(request_identity({k: v for k, v in args.items() if k != "num_samples"}))
        if count > self.sampling_cap - self.attempts_used:
            raise ReplayError("budget_exceeded", "Entire request denied: it exceeds the remaining hard sampling budget.")
        route_id = self.routes.get(route_hash)
        if route_id is None:
            raise ReplayError("request_unavailable", "No recorded route matches these actual model conditions and parameters.")
        frames = self.fixture.get("attempts", [])[self.cursor:self.cursor + count]
        if len(frames) != count:
            raise ReplayError("fixture_exhausted", "The approved attempt stream has no complete response for this request.")
        if any(frame["route_id"] != route_id for frame in frames):
            raise ReplayError("sequence_mismatch", "This request does not match the next approved attempt sequence.")
        start = self.attempts_used
        self.cursor += count
        self.attempts_used += count
        records, errors, outcomes = [], [], []
        for index, frame in enumerate(frames):
            record = self._materialize(frame["candidate_id"]) if frame["candidate_id"] is not None else None
            if record:
                records.append(record)
            if frame["error"]:
                errors.append({"attempt": start + index + 1, **frame["error"]})
            outcomes.append({"attempt": start + index + 1, "candidate_id": frame["candidate_id"], "error": frame["error"]})
        return {"status": "partial" if records and errors else "failed" if errors else "completed",
                "requested_samples": count, "attempts_consumed": count, "num_generated": len(records),
                "records": records, "structures": [x["path"] for x in records], "outcomes": outcomes,
                "errors": errors, "output_dir": str(self.root / "candidates"), "route_sha256": route_hash,
                "budget": self._status()}

    def _dispatch(self, operation: str, args: dict) -> dict:
        if not isinstance(args, dict):
            raise ReplayError("invalid_request", "arguments must be an object.")
        if operation == "sample":
            return self._sample(args)
        if operation == "status" and not args:
            return self._status()
        if operation == "list_artifacts" and not args:
            return {"artifacts": list(self.artifacts.values())}
        if operation == "read_artifact" and set(args) == {"artifact_id"}:
            artifact = self.artifacts.get(args["artifact_id"])
            if not artifact:
                raise ReplayError("artifact_unavailable", "Artifact ID does not exist in this session.")
            content = Path(artifact["path"]).read_bytes()
            if _bytes_hash(content) != artifact["sha256"]:
                raise ReplayError("artifact_changed", "Artifact bytes changed after materialization.")
            return {**artifact, "content": content.decode("utf-8")}
        if operation == "get_candidates" and set(args) <= {"candidate_ids"}:
            ids = args.get("candidate_ids", list(self.candidates))
            if not isinstance(ids, list) or len(set(ids)) != len(ids) or any(x not in self.candidates for x in ids):
                raise ReplayError("candidate_unavailable", "Select explicit existing candidate IDs without duplicates.")
            for candidate_id in ids:
                verify_artifact(self.candidates[candidate_id])
            return {"records": [self.candidates[x] for x in ids]}
        if operation == "get_versions" and set(args) == {"candidate_id"}:
            self._selected_version(args)
            return {"candidate_id": args["candidate_id"], "records": [
                {**record, "integrity": self._integrity(record)} for record in self.versions.values()
                if record["candidate_id"] == args["candidate_id"]]}
        if operation == "verify_candidate_artifact" and set(args) <= {"candidate_id", "structure_version_id"} and "candidate_id" in args:
            return self._integrity(self._selected_version(args))
        if operation == "query_database" and set(args) == {"database", "query"}:
            if not isinstance(args["database"], str) or not isinstance(args["query"], dict):
                raise ReplayError("invalid_request", "Database request needs a name and query object.")
            result = self.database_results.get(canonical_hash(args))
            if result is None:
                raise ReplayError("query_unavailable", "No frozen database observation matches the exact request.")
            verify_artifact(result["raw_output"])
            return result
        if operation == "get_metrics" and set(args) <= {"candidate_id", "structure_version_id"} and "candidate_id" in args:
            record = self._selected_version(args)
            verify_artifact(record)
            metrics = self.metrics_by_version[record["structure_version_id"]]
            for metric in metrics:
                verify_artifact(metric["input_artifact"])
                verify_artifact(metric["raw_output"])
            return {"candidate_id": record["candidate_id"], "structure_version_id": record["structure_version_id"],
                    "metrics": metrics}
        raise ReplayError("invalid_request", "Unknown operation or invalid operation arguments.")

    def invoke(self, operation: str, arguments: dict | None = None) -> dict:
        """Return a result or raise ReplayError; every attempted call is journaled."""
        with self.lock:
            # Snapshot first so callers cannot mutate stored traces or returned state.
            request = json.loads(json.dumps({"operation": operation, "arguments": arguments if arguments is not None else {}}, allow_nan=False))
            before = {"attempts_used": self.attempts_used, "requested_samples": self.requested_samples, "cursor": self.cursor}
            failure = None
            try:
                result = self._dispatch(request["operation"], request["arguments"])
                outcome = {"result": result}
            except (ReplayError, ValueError, TypeError, KeyError, OSError) as exc:
                failure = exc if isinstance(exc, ReplayError) else ReplayError("replay_error", str(exc))
                outcome = {"error": {"code": failure.code, "message": str(failure)}}
            event = {"schema_version": 1, "index": self.event_count, "previous_sha256": self.last_event_sha256,
                     "run_id": self.run_id, "identity": self.identity, "request": request, "before": before,
                     "after": {"attempts_used": self.attempts_used, "requested_samples": self.requested_samples, "cursor": self.cursor}, **outcome}
            digest = _write_new(self.root / "events" / f"{self.event_count:06d}.json", _json_bytes(event))
            self.last_event_sha256 = digest
            self.event_count += 1
            if failure:
                raise failure
            return json.loads(json.dumps({**result, "replay": self.identity,
                                         "receipt": {"event_index": self.event_count - 1, "event_sha256": digest}}, allow_nan=False))


def verify_trace(output_dir: str | Path, *, expected_head_sha256: str | None = None,
                 expected_events: int | None = None) -> dict:
    """Verify contiguous create-only events and their hash chain against session identity."""
    root = Path(output_dir)
    session = json.loads((root / "session.json").read_bytes())
    previous = None
    count = 0
    for index, path in enumerate(sorted((root / "events").glob("*.json"))):
        raw = path.read_bytes()
        event = json.loads(raw)
        if path.name != f"{index:06d}.json" or event["index"] != index or event["previous_sha256"] != previous:
            raise ReplayError("trace_changed", "Event sequence or hash chain changed.")
        if event["run_id"] != session["run_id"] or event["identity"]["manifest_sha256"] != session["manifest_sha256"] or event["identity"]["fixture_sha256"] != session["fixture_sha256"]:
            raise ReplayError("trace_changed", "Event identity differs from session identity.")
        previous = _bytes_hash(raw)
        count += 1
    if ((expected_head_sha256 is not None and previous != expected_head_sha256)
            or (expected_events is not None and count != expected_events)):
        raise ReplayError("trace_changed", "Trace differs from its externally frozen receipt.")
    return {"events": count, "head_sha256": previous, "manifest_sha256": session["manifest_sha256"],
            "limitation": "Export and freeze this receipt externally to detect tail deletion or complete-chain replacement."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--sampling-cap", type=int)
    parser.add_argument("--formal", action="store_true")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--review")
    parser.add_argument("--expected-review-sha256")
    args = parser.parse_args(argv)
    session = ReplaySession(args.manifest, args.fixture_id, args.output_dir, args.run_id, args.sampling_cap,
                            formal=args.formal, expected_manifest_sha256=args.expected_manifest_sha256,
                            review_path=args.review, expected_review_sha256=args.expected_review_sha256)
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) - {"id", "operation", "arguments"}:
                raise ReplayError("invalid_request", "JSONL envelope must contain id, operation and arguments only.")
            response = {"id": request.get("id"), "result": session.invoke(request.get("operation"), request.get("arguments", {}))}
        except (ReplayError, ValueError, TypeError, KeyError) as exc:
            response = {"id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": getattr(exc, "code", "invalid_json"), "message": str(exc)}}
        print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
