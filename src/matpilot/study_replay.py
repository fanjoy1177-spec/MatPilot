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
            source = candidate.get("provenance", {})
            if source.get("kind") not in {"synthetic", "recorded"} or not source.get("description"):
                raise ReplayError("invalid_manifest", "Candidate provenance is required.")
            if source["kind"] == "synthetic":
                if set(candidate.get("structure", {})) != {"lattice", "species", "frac_coords"}:
                    raise ReplayError("invalid_manifest", "Synthetic structures require explicit lattice, species and fractional coordinates.")
            else:
                if not source.get("source") or not isinstance(candidate.get("cif_text"), str) or _bytes_hash(candidate["cif_text"].encode()) != candidate.get("cif_sha256"):
                    raise ReplayError("invalid_manifest", "Recorded CIF requires source identity and an exact content hash.")
            for metric in candidate.get("metrics", []):
                required = {"property", "value", "unit", "method", "convergence", "reference_set", "provenance"}
                if set(metric) != required or metric["provenance"].get("kind") not in {"synthetic", "recorded"}:
                    raise ReplayError("invalid_manifest", "Metric evidence/provenance schema is incomplete.")
                value = metric["value"]
                if value is not None and (type(value) not in {int, float} or not math.isfinite(value)):
                    raise ReplayError("invalid_manifest", "Metric value must be finite or unknown.")
                if metric["provenance"]["kind"] == "recorded" and not metric["provenance"].get("source"):
                    raise ReplayError("invalid_manifest", "Recorded metric needs a source identity.")
        initial = fixture.get("initial_candidates", [])
        if not isinstance(initial, list) or len(set(initial)) != len(initial) or any(x not in candidates for x in initial):
            raise ReplayError("invalid_manifest", "Invalid initial candidate references.")
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
        _write_new(self.root / "session.json", _json_bytes({**self.identity, "run_id": run_id, "sampling_cap": self.sampling_cap}))
        for candidate_id in self.fixture.get("initial_candidates", []):
            self._materialize(candidate_id)

    def _artifact(self, artifact_id: str, relative: str, data: bytes, kind: str) -> dict:
        path = self.root / relative
        digest = _write_new(path, data)
        record = {"artifact_id": artifact_id, "path": str(path), "relative_path": relative,
                  "sha256": digest, "kind": kind, "bytes": len(data)}
        self.artifacts[artifact_id] = record
        return record

    def _materialize(self, candidate_id: str) -> dict:
        from pymatgen.core import Lattice, Structure
        from pymatgen.io.cif import CifWriter
        definition = self.fixture["candidates"][candidate_id]
        source = definition["provenance"]
        if source["kind"] == "synthetic":
            data = definition["structure"]
            crystal = Structure(Lattice(data["lattice"]), data["species"], data["frac_coords"])
            cif = ("# SYNTHETIC WORKFLOW FIXTURE: NOT MODEL OUTPUT OR MATERIAL EVIDENCE\n" + str(CifWriter(crystal))).encode()
        else:
            cif = definition["cif_text"].encode()
            crystal = Structure.from_str(cif.decode(), fmt="cif")
        artifact = self._artifact(f"{candidate_id}.cif", f"candidates/{candidate_id}.cif", cif, "structure")
        version_id = "s_" + canonical_hash([self.identity["fixture_sha256"], candidate_id, artifact["sha256"], None, "fixture_import"])
        record = {"candidate_id": candidate_id, "structure_version_id": version_id,
                  "parent_version_id": None, "transformation": "fixture_import", **artifact,
                  "formula": crystal.composition.reduced_formula,
                  "elements": sorted(x.symbol for x in crystal.composition.elements),
                  "num_sites": len(crystal), "parse_valid": True,
                  "provenance": source, "evidence_scope": SCOPE}
        self.candidates[candidate_id] = record
        self.metrics[candidate_id] = []
        for index, definition_metric in enumerate(definition.get("metrics", [])):
            raw = self._artifact(f"{candidate_id}.metric.{index}", f"metrics/{candidate_id}-{index}.json",
                                 _json_bytes({**definition_metric, "candidate_id": candidate_id,
                                              "structure_version_id": version_id, "evidence_scope": SCOPE}), "metric_raw")
            metric = {k: copy.deepcopy(v) for k, v in definition_metric.items() if k != "provenance"}
            metric.update(candidate_id=candidate_id, structure_version_id=version_id,
                          input_artifact={k: artifact[k] for k in ("path", "sha256")},
                          raw_output={k: raw[k] for k in ("path", "sha256")})
            validated = validate_metric(metric, record)
            self.metrics[candidate_id].append({**validated, "provenance": definition_metric["provenance"], "evidence_scope": SCOPE})
        return record

    def _status(self) -> dict:
        return {**self.identity, "attempts_used": self.attempts_used, "sampling_cap": self.sampling_cap,
                "remaining_budget": self.sampling_cap - self.attempts_used,
                "requested_samples": self.requested_samples,
                "candidate_ids": list(self.candidates), "events_recorded": self.event_count}

    def _sample(self, args: dict) -> dict:
        if not isinstance(args, dict) or set(args) != {"engine", "model_name", "conditions", "parameters", "num_samples"}:
            raise ReplayError("invalid_request", "sample needs the complete route plus num_samples.")
        count = _positive_int(args["num_samples"], "num_samples")
        self.requested_samples += count
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
        if operation == "get_metrics" and set(args) <= {"candidate_id", "structure_version_id"} and "candidate_id" in args:
            record = self.candidates.get(args["candidate_id"])
            if record is None:
                raise ReplayError("candidate_unavailable", "Candidate does not exist in this session.")
            if args.get("structure_version_id", record["structure_version_id"]) != record["structure_version_id"]:
                raise ReplayError("version_mismatch", "Requested candidate and structure version do not match.")
            verify_artifact(record)
            for metric in self.metrics[record["candidate_id"]]:
                verify_artifact(metric["raw_output"])
            return {"candidate_id": record["candidate_id"], "structure_version_id": record["structure_version_id"],
                    "metrics": self.metrics[record["candidate_id"]]}
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
