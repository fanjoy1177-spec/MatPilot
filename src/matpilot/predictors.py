"""Disabled-by-default, operator-configured CHGNet static process adapter.

No optional model package is imported in the MatPilot process. Preparing a plan
parses and hashes existing local artifacts only; execution is a separate call.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from copy import deepcopy
from pathlib import Path

from .adapters.mattergen import ELEMENTS, canonical_hash
from .evidence import validate_metric, verify_artifact


ADAPTER_ID = "chgnet_static_v1"
PACKAGE_VERSION = "0.4.2"
CHECKPOINT_MODEL_VERSION = "0.3.0"
MATERIAL_CLASSES = {"ionic_crystal", "metal_alloy", "covalent_crystal", "molecular_crystal", "porous_crystal"}
STRUCTURE_STATES = {"original_generated", "relaxed"}
UNCERTAINTY = {"kind": "not_calibrated", "description": "No calibrated uncertainty or stability probability is supplied by this adapter."}

# Fixed source, not an LLM-authored command. The independent Python environment
# only needs CHGNet and its dependencies; it need not install MatPilot.
_WORKER = r'''"""MatPilot CHGNet static worker; no optimization, download or DFT."""
import hashlib, importlib.metadata, json, math, pathlib, sys
def sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()
manifest_path, output_path = map(pathlib.Path, sys.argv[1:])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
plan = manifest["input"]
encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
if hashlib.sha256(encoded).hexdigest() != manifest["plan_sha256"]:
    raise ValueError("Manifest plan checksum differs")
if plan["operator_config"]["enabled"] is not True:
    raise ValueError("Prediction execution is disabled")
if importlib.metadata.version("chgnet") != "0.4.2":
    raise ValueError("The independent environment must contain exactly chgnet==0.4.2")
if sha(plan["checkpoint"]["path"]) != plan["checkpoint"]["sha256"]:
    raise ValueError("Checkpoint checksum differs")
if sha(plan["record"]["path"]) != plan["record"]["sha256"]:
    raise ValueError("Input structure checksum differs")
from pymatgen.core import Structure
from chgnet.model.model import CHGNet
structure = Structure.from_file(plan["record"]["path"])
scope = plan["operator_config"]["scope"]
elements = sorted(x.symbol for x in structure.composition.elements)
if (not structure.is_ordered or not set(elements) <= set(scope["elements"])
        or len(structure) > scope["max_atoms"]):
    raise ValueError("Input structure is outside the declared predictor scope")
model = CHGNet.from_file(plan["checkpoint"]["path"], mlp_out_bias=False, version="0.3.0")
if not model.is_intensive:
    raise ValueError("This adapter requires an intensive energy checkpoint")
model = model.to(plan["operator_config"]["device"])
prediction = model.predict_structure(structure, task="efs", batch_size=1)
result = {"schema_version": 1, "adapter_id": "chgnet_static_v1", "plan_sha256": manifest["plan_sha256"],
          "package_version": "0.4.2", "checkpoint_sha256": plan["checkpoint"]["sha256"],
          "candidate_id": plan["record"]["candidate_id"], "structure_version_id": plan["record"]["structure_version_id"],
          "input_sha256": plan["record"]["sha256"], "task": "efs", "energy_per_atom": float(prediction["e"]),
          "forces": prediction["f"].tolist(), "stress": prediction["s"].tolist(),
          "units": {"energy_per_atom": "eV/atom", "forces": "eV/angstrom", "stress": "GPa"},
          "convergence": "not_applicable", "uncertainty": plan["operator_config"]["uncertainty"]}
with output_path.open("x", encoding="utf-8") as stream:
    json.dump(result, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
'''
WORKER_SHA256 = hashlib.sha256(_WORKER.encode("utf-8")).hexdigest()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def predictor_config_schema() -> dict:
    """Operator-only configuration; never expose path/command mutation to LLMs."""
    return {
        "adapter_id": ADAPTER_ID, "package_version": PACKAGE_VERSION,
        "checkpoint_model_version": CHECKPOINT_MODEL_VERSION, "enabled_default": False,
        "fields": ["enabled", "adapter_id", "package_version", "checkpoint_model_version", "python_executable",
                   "checkpoint_path", "checkpoint_sha256", "checkpoint_source", "device", "timeout_seconds",
                   "scope", "training_data_identity", "validation_domain", "uncertainty"],
        "scope": {"elements": "explicit element-symbol list; no wildcard", "material_classes": sorted(MATERIAL_CLASSES),
                  "structure_states": sorted(STRUCTURE_STATES), "max_atoms": "positive integer <= 4096"},
        "validation_domain": {"status": "declared or unknown", "description": "nonempty text", "source": "nonempty provenance text"},
        "uncertainty": deepcopy(UNCERTAINTY),
        "outputs": {"energy_per_atom": "eV/atom", "max_force": "eV/angstrom", "max_abs_stress": "GPa"},
        "not_provided": ["energy_above_hull", "formation_energy", "band_gap", "ionic_conductivity", "relaxation", "DFT"],
        "boundary": "Local trusted operator settings only; allowed elements/classes are declarations, not independent scientific validation.",
    }


def validate_predictor_config(config: dict | None = None) -> dict:
    defaults = {"enabled": False, "adapter_id": ADAPTER_ID, "package_version": PACKAGE_VERSION,
                "checkpoint_model_version": CHECKPOINT_MODEL_VERSION, "python_executable": None,
                "checkpoint_path": None, "checkpoint_sha256": None, "checkpoint_source": None,
                "device": "cpu", "timeout_seconds": 120,
                "scope": {"elements": [], "material_classes": [], "structure_states": [], "max_atoms": 256},
                "training_data_identity": None,
                "validation_domain": {"status": "unknown", "description": "Not yet declared", "source": "unknown"},
                "uncertainty": deepcopy(UNCERTAINTY)}
    if config is None:
        return defaults
    if not isinstance(config, dict) or set(config) - set(defaults):
        raise ValueError("Predictor configuration contains unknown operator fields; arbitrary commands are not accepted.")
    result = {**defaults, **deepcopy(config)}
    if type(result["enabled"]) is not bool:
        raise ValueError("Predictor enabled must be boolean.")
    if (result["adapter_id"], result["package_version"], result["checkpoint_model_version"]) != (ADAPTER_ID, PACKAGE_VERSION, CHECKPOINT_MODEL_VERSION):
        raise ValueError("This adapter requires CHGNet package 0.4.2 and checkpoint architecture 0.3.0.")
    for field in ("python_executable", "checkpoint_path"):
        value = result[field]
        if value is not None:
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError(f"{field} must be an absolute operator-configured local path or null.")
            result[field] = str(Path(value).resolve())
    if result["python_executable"] is not None and not re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", Path(result["python_executable"]).name, re.IGNORECASE):
        raise ValueError("python_executable must name a Python interpreter, not a shell or arbitrary command.")
    if result["checkpoint_sha256"] is not None and (not isinstance(result["checkpoint_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", result["checkpoint_sha256"])):
        raise ValueError("checkpoint_sha256 must be an explicit lowercase SHA256 or null.")
    for field in ("checkpoint_source", "training_data_identity"):
        if result[field] is not None and (not isinstance(result[field], str) or not result[field].strip()):
            raise ValueError(f"{field} must contain provenance text or null.")
    if result["device"] not in ("cpu", "cuda", "mps"):
        raise ValueError("Predictor device must be explicitly cpu, cuda or mps.")
    if type(result["timeout_seconds"]) is not int or not 1 <= result["timeout_seconds"] <= 3600:
        raise ValueError("timeout_seconds must be an integer from 1 to 3600.")
    scope = result["scope"]
    if not isinstance(scope, dict) or set(scope) != {"elements", "material_classes", "structure_states", "max_atoms"}:
        raise ValueError("Scope needs explicit elements, material_classes, structure_states and max_atoms.")
    for field, allowed in (("elements", ELEMENTS), ("material_classes", MATERIAL_CLASSES), ("structure_states", STRUCTURE_STATES)):
        values = scope[field]
        if not isinstance(values, list) or any(not isinstance(v, str) or v not in allowed for v in values) or len(set(values)) != len(values):
            raise ValueError(f"Scope {field} must be a unique list from the registered vocabulary.")
        scope[field] = sorted(values)
    if type(scope["max_atoms"]) is not int or not 1 <= scope["max_atoms"] <= 4096:
        raise ValueError("scope.max_atoms must be an integer from 1 to 4096.")
    domain = result["validation_domain"]
    if (not isinstance(domain, dict) or set(domain) != {"status", "description", "source"}
            or domain["status"] not in ("declared", "unknown")
            or any(not isinstance(domain[k], str) or not domain[k].strip() for k in ("description", "source"))):
        raise ValueError("validation_domain needs status=declared/unknown, description and source.")
    if result["uncertainty"] != UNCERTAINTY:
        raise ValueError("This adapter has no calibrated uncertainty; retain its explicit not_calibrated declaration.")
    return result


def _known(value) -> bool:
    return isinstance(value, str) and value.strip().lower() not in {"", "unknown", "unverified", "not_measured"}


def _scope_check(record: dict, config: dict, material_class: str, structure_state: str) -> dict:
    from pymatgen.core import Structure

    if not isinstance(record, dict) or any(not isinstance(record.get(k), str) or not record[k]
                                          for k in ("candidate_id", "structure_version_id", "path", "sha256")):
        raise ValueError("Predictor requires an authoritative candidate and structure-version record with artifact identity.")
    verify_artifact(record)
    crystal = Structure.from_file(record["path"])
    elements = sorted(x.symbol for x in crystal.composition.elements)
    reasons = []
    scope = config["scope"]
    registered_state = ("original_generated" if record.get("parent_version_id") is None and record.get("transformation", "generated") == "generated"
                        else "relaxed" if record.get("parent_version_id") and record.get("transformation") in {"relaxed", "relaxation"}
                        else "unknown")
    if not set(elements) <= set(scope["elements"]):
        reasons.append("Actual CIF elements exceed the explicitly declared element scope.")
    if material_class not in scope["material_classes"]:
        reasons.append("Declared material class is outside the operator's predictor scope.")
    if structure_state not in scope["structure_states"] or structure_state != registered_state:
        reasons.append("Declared structure state is outside scope or differs from registered version lineage.")
    if not crystal.is_ordered:
        reasons.append("Partially occupied or disordered structures are outside this adapter's scope.")
    if len(crystal) > scope["max_atoms"]:
        reasons.append("Structure exceeds the configured atom budget.")
    return {"status": "unsupported" if reasons else "within_declared_scope", "reasons": reasons,
            "actual_elements": elements, "num_atoms": len(crystal), "material_class": material_class,
            "structure_state": structure_state, "registered_structure_state": registered_state,
            "material_class_basis": "explicit declaration; not automatically inferred or independently validated",
            "scientific_validation": "not_established_by_scope_membership"}


def _write_immutable(path: Path, content: str) -> None:
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ValueError(f"Prepared artifact differs from its immutable plan: {path.name}")
    else:
        path.write_text(content, encoding="utf-8", newline="")


def prepare_static_prediction(record: dict, config: dict | None, work_root: Path,
                              *, material_class: str, structure_state: str) -> dict:
    """Prepare local manifests only; this function never invokes a subprocess."""
    config = validate_predictor_config(config)
    if not isinstance(material_class, str) or not isinstance(structure_state, str):
        raise ValueError("Material class and structure state must be explicit strings.")
    blockers = []
    if not config["enabled"]:
        blockers.append(("disabled", "Static prediction is disabled by operator configuration."))
    checkpoint = Path(config["checkpoint_path"]) if config["checkpoint_path"] else None
    if checkpoint is None or not checkpoint.is_file():
        blockers.append(("weights_missing", "Configured local checkpoint is missing; no download is attempted."))
    elif config["checkpoint_sha256"] is None or _sha(checkpoint) != config["checkpoint_sha256"]:
        blockers.append(("weights_mismatch", "Local checkpoint does not match the configured SHA256."))
    if config["python_executable"] is None or not Path(config["python_executable"]).is_file():
        blockers.append(("environment_missing", "Configured independent Python interpreter is missing."))
    if (not _known(config["training_data_identity"]) or not _known(config["checkpoint_source"])
            or config["validation_domain"]["status"] != "declared"
            or not _known(config["validation_domain"]["source"])):
        blockers.append(("needs_review", "Training identity, checkpoint source and validation-domain declaration must be explicit."))
    scope = _scope_check(record, config, material_class, structure_state)
    if scope["status"] != "within_declared_scope":
        blockers.append(("unsupported", "; ".join(scope["reasons"])))
    common = {"adapter_id": ADAPTER_ID, "scope_evaluation": scope,
              "scientific_accuracy": "not_validated", "real_prediction_performed": False,
              "package_verification": "Deferred to isolated worker; preparation does not import CHGNet."}
    if blockers:
        return {**common, "status": blockers[0][0], "reasons": [x[1] for x in blockers],
                "blockers": [{"status": code, "reason": reason} for code, reason in blockers]}
    bound_record = {k: deepcopy(record[k]) for k in ("candidate_id", "structure_version_id", "path", "sha256")}
    bound_record.update(parent_version_id=record.get("parent_version_id"), transformation=record.get("transformation", "generated"))
    bound_record["path"] = str(Path(bound_record["path"]).resolve())
    input_manifest = {"schema_version": 1, "adapter_id": ADAPTER_ID, "operator_config": config,
                      "operator_config_sha256": canonical_hash(config), "record": bound_record,
                      "scope_evaluation": scope, "task": "efs", "batch_size": 1,
                      "checkpoint": {"path": str(checkpoint), "sha256": config["checkpoint_sha256"]},
                      "worker_sha256": WORKER_SHA256}
    plan_sha = canonical_hash(input_manifest)
    base = Path(work_root).resolve()
    directory = (base / ("p_" + plan_sha[:24])).resolve()
    if not directory.is_relative_to(base):
        raise ValueError("Prepared prediction directory escapes the trusted operator work root.")
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path, worker_path, output_path = (directory / name for name in ("input_manifest.json", "worker.py", "raw_prediction.json"))
    manifest = {"plan_sha256": plan_sha, "input": input_manifest}
    _write_immutable(manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    _write_immutable(worker_path, _WORKER)
    argv = [config["python_executable"], "-I", str(worker_path), str(manifest_path), str(output_path)]
    return {**common, "status": "ready", "reasons": [], "plan_sha256": plan_sha, "input_manifest": input_manifest,
            "manifest_artifact": {"path": str(manifest_path), "sha256": _sha(manifest_path)},
            "worker_artifact": {"path": str(worker_path), "sha256": _sha(worker_path)},
            "output_path": str(output_path), "work_dir": str(directory), "argv": argv,
            "execution_note": "Prepared only. The operator must separately authorize static inference; no relaxation or DFT exists in this worker."}


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Prediction {name} must be finite numeric evidence.")
    return value


def _validated_output(plan: dict, raw: dict) -> list[dict]:
    manifest = plan["input_manifest"]
    record, config = manifest["record"], manifest["operator_config"]
    expected = {"schema_version": 1, "adapter_id": ADAPTER_ID, "plan_sha256": plan["plan_sha256"],
                "package_version": PACKAGE_VERSION, "checkpoint_sha256": config["checkpoint_sha256"],
                "candidate_id": record["candidate_id"], "structure_version_id": record["structure_version_id"],
                "input_sha256": record["sha256"], "task": "efs", "convergence": "not_applicable",
                "units": {"energy_per_atom": "eV/atom", "forces": "eV/angstrom", "stress": "GPa"},
                "uncertainty": UNCERTAINTY}
    if not isinstance(raw, dict) or any(raw.get(k) != v for k, v in expected.items()):
        raise ValueError("Prediction output identity, version, units or declared semantics differ from the prepared plan.")
    energy = _finite(raw.get("energy_per_atom"), "energy_per_atom")
    forces, stress = raw.get("forces"), raw.get("stress")
    for name, data, count in (("forces", forces, manifest["scope_evaluation"]["num_atoms"]), ("stress", stress, 3)):
        if not isinstance(data, list) or len(data) != count or any(not isinstance(row, list) or len(row) != 3 for row in data):
            raise ValueError(f"Prediction {name} shape differs from the structure or tensor contract.")
        for row in data:
            for value in row:
                _finite(value, name)
    max_force = max(math.hypot(*row) for row in forces)
    max_stress = max(abs(value) for row in stress for value in row)
    _finite(max_force, "max_force")
    artifact = {"path": plan["output_path"], "sha256": _sha(Path(plan["output_path"]))}
    method = {"name": "CHGNet", "version": PACKAGE_VERSION, "weights_identity": "sha256:" + config["checkpoint_sha256"],
              "parameters": {"adapter_id": ADAPTER_ID, "checkpoint_model_version": CHECKPOINT_MODEL_VERSION,
                             "task": "efs", "batch_size": 1, "device": config["device"],
                             "scope": config["scope"], "training_data_identity": config["training_data_identity"],
                             "validation_domain": config["validation_domain"], "uncertainty": UNCERTAINTY,
                             "max_force_definition": "maximum atomic Euclidean force norm",
                             "max_abs_stress_definition": "maximum absolute Cartesian stress-tensor component"}}
    results = []
    for prop, value, unit in (("energy_per_atom", energy, "eV/atom"), ("max_force", max_force, "eV/angstrom"), ("max_abs_stress", max_stress, "GPa")):
        metric = {"candidate_id": record["candidate_id"], "structure_version_id": record["structure_version_id"],
                  "property": prop, "value": value, "unit": unit, "method": deepcopy(method),
                  "input_artifact": {"path": record["path"], "sha256": record["sha256"]},
                  "raw_output": artifact, "convergence": "not_applicable", "reference_set": None}
        results.append(validate_metric(metric, record))
    return results


def execute_static_prediction(plan: dict, config: dict | None, work_root: Path, *, runner=None) -> dict:
    """Operator-only execution. ``runner`` injection exists for CPU contract tests.

    Callers must supply the trusted work root and resolve registered input IDs;
    never let an LLM supply operator config, paths, arbitrary argv or a runner.
    """
    config = validate_predictor_config(config)
    if not config["enabled"]:
        raise ValueError("Static prediction is disabled; no subprocess was started.")
    if not isinstance(plan, dict) or plan.get("status") != "ready":
        raise ValueError("Only a prepared ready plan can execute.")
    manifest = plan["input_manifest"]
    if (canonical_hash(manifest) != plan["plan_sha256"] or manifest["operator_config_sha256"] != canonical_hash(config)
            or manifest["operator_config"] != config or manifest["worker_sha256"] != WORKER_SHA256):
        raise ValueError("Plan, operator configuration or worker identity changed after preparation.")
    scope = manifest["scope_evaluation"]
    current = prepare_static_prediction(manifest["record"], config, work_root,
                                        material_class=scope["material_class"], structure_state=scope["structure_state"])
    fields = ("status", "plan_sha256", "input_manifest", "manifest_artifact", "worker_artifact", "output_path", "work_dir", "argv")
    if any(current.get(k) != plan.get(k) for k in fields):
        raise ValueError("Prepared artifacts, scope, input or paths no longer match the trusted plan.")
    verify_artifact(plan["manifest_artifact"])
    verify_artifact(plan["worker_artifact"])
    if Path(plan["output_path"]).exists():
        raise ValueError("Prediction output already exists; archived evidence will not be overwritten.")
    execute = subprocess.run if runner is None else runner
    try:
        completed = execute(plan["argv"], cwd=plan["work_dir"], shell=False, check=False,
                            capture_output=True, text=True, timeout=config["timeout_seconds"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"status": "failed", "reason": str(exc), "metrics": [], "plan_sha256": plan["plan_sha256"],
                "real_prediction_performed": "unknown", "scientific_accuracy": "not_validated"}
    if completed.returncode != 0:
        return {"status": "failed", "reason": "Independent prediction process failed.", "returncode": completed.returncode,
                "stderr_tail": (completed.stderr or "")[-4000:], "metrics": [], "plan_sha256": plan["plan_sha256"],
                "real_prediction_performed": "unknown", "scientific_accuracy": "not_validated"}
    output_path = Path(plan["output_path"])
    if not output_path.is_file():
        raise ValueError("Prediction process returned no raw output artifact.")
    raw = json.loads(output_path.read_text(encoding="utf-8"))
    metrics = _validated_output(plan, raw)
    verify_artifact(manifest["record"])
    verify_artifact(manifest["checkpoint"])
    return {"status": "completed", "plan_sha256": plan["plan_sha256"], "metrics": metrics,
            "raw_output": {"path": str(output_path), "sha256": _sha(output_path)}, "scope_evaluation": scope,
            "execution_source": "independent_process" if runner is None else "injected_runner_contract_test",
            "real_prediction_performed": runner is None, "scientific_accuracy": "not_validated",
            "interpretation": "Static model predictions only. Per-atom energy is not hull distance; force/stress are readiness signals, not proof of stability or relaxed convergence."}
