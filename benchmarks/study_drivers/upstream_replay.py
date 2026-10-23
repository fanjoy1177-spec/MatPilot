"""Isolated U/U+ source execution with explicitly synthetic scientific engines.

The actual MatterGen server, wrapper and constructor execute. DiffCSP executes
its actual server and generation wrapper, with registered-template CPU symmetry
data and diffusion replay replacing its heavy model setup. This is NOT
real-model, PyXtal/PyTorch equivalence or full MCP transport acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import types

from matpilot.adapters.mattergen import canonical_hash
from matpilot.study_replay import ReplayError, ReplaySession, verify_trace
from matpilot.templates import resolve_template


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASELINE = ROOT / "benchmarks/baseline"
WRAPPER = "src/utils/generative_models/mattergen/mattergen_wrapper.py"
SERVER = "src/mcp_server/mattergen_server.py"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized(path: Path) -> bytes:
    return path.read_text(encoding="utf-8").encode("utf-8")


def exclusive_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def prepare_sources(group: str, target: Path) -> dict:
    """Copy only exact locked upstream files, then apply the exact published patch."""
    if group not in {"U", "U+"}:
        raise ValueError("This prototype implements real upstream U and U+ only.")
    lock_path = HERE / "upstream-source.json"
    lock = json.loads(lock_path.read_bytes())
    baseline_lock = json.loads((BASELINE / "source-lock.json").read_bytes())
    if lock["commit"] != baseline_lock["commit"]:
        raise ValueError("Source and patch commits differ.")
    target.mkdir(parents=True, exist_ok=False)
    for entry in lock["files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid locked source path.")
        data = normalized(HERE / "upstream_snapshot" / relative)
        if sha(data) != entry["sha256_utf8_lf"]:
            raise ValueError(f"Source hash mismatch: {entry['path']}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    for entry in baseline_lock["files"]:
        if sha(normalized(target / entry["path"])) != entry["original_utf8_lf_sha256"]:
            raise ValueError("Published patch source differs from the execution snapshot.")
    patch_path = BASELINE / "atomisticskills-uplus.patch"
    patch_bytes = normalized(patch_path)
    if sha(patch_bytes) != baseline_lock["patch_sha256"]:
        raise ValueError("Published patch identity mismatch.")
    # Avoid host checkout newline settings while preserving published patch bytes.
    local_patch = target / "published-uplus.patch"
    local_patch.write_bytes(patch_bytes)
    if group == "U+":
        result = subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(target), "apply", str(local_patch)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError("Published patch failed: " + result.stderr)
        for entry in baseline_lock["files"]:
            if sha(normalized(target / entry["path"])) != entry["patched_sha256"]:
                raise ValueError("Patched source identity mismatch.")
    return {"group": group, "upstream": lock["upstream"], "source_revision": lock["commit"],
            "source_manifest_sha256": sha(lock_path.read_bytes()),
            "applied_patch_sha256": sha(patch_bytes) if group == "U+" else None,
            "available_uplus_patch_sha256": sha(patch_bytes),
            "source_files": {entry["path"]: sha(normalized(target / entry["path"])) for entry in lock["files"] if entry["path"].endswith(".py")},
            "normal_instructions": {entry["path"]: {"path": str(target / entry["path"]), "sha256_utf8_lf": entry["sha256_utf8_lf"]}
                                    for entry in lock["files"] if entry["path"].endswith("SKILL.md")}}


def no_network_or_process(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
        raise RuntimeError("The CPU replay worker prohibits network and external execution.")


class UpstreamWorker:
    def __init__(self, args):
        self.root = Path(args.output_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.source_root = self.root / "upstream"
        self.identity = prepare_sources(args.group, self.source_root)
        self.session = ReplaySession(args.manifest, args.fixture_id, self.root / "replay", args.run_id,
                                     args.sampling_cap, formal=args.formal,
                                     expected_manifest_sha256=args.expected_manifest_sha256,
                                     review_path=args.review, expected_review_sha256=args.expected_review_sha256)
        self.replay = self.session
        self.generator_calls = []
        self.returned_records = []
        self.upstream_artifacts = {}
        self.tool_call_index = 0
        os.environ["ATOMISTIC_WORKSPACE"] = str(self.root / "workspace")
        fake_checkout = self.root / "synthetic-mattergen-checkout"
        (fake_checkout / "sampling_conf").mkdir(parents=True)
        os.environ["MATTERGEN_REPO"] = str(fake_checkout)
        sys.path.insert(0, str(self.source_root))
        # Preserve the real configuration implementation but isolate its paths.
        config = importlib.import_module("src.utils.config_utils")
        config.CONFIG_FILE = self.root / "absent-atomistic-config.yaml"
        config.OLD_CONFIG_FILE = self.root / "absent-legacy-config.yaml"
        for key in list(os.environ):
            if key.startswith("MLIP_"):
                del os.environ[key]
        # CPU parser imports query the Windows version via platform/subprocess;
        # complete those ordinary library imports before blocking all execution.
        from pymatgen.core import Structure  # noqa: F401
        from pymatgen.io.cif import CifWriter  # noqa: F401
        config_path = Path(getattr(args, "template_config", None) or ROOT / "examples/config.multimodel.demo.json").resolve()
        template_config = json.loads(config_path.read_bytes())
        registrations = template_config.get("templates", {})
        for registration in registrations.values():
            registration["path"] = str((config_path.parent / registration["path"]).resolve())
        self.templates = {key: resolve_template(key, types.SimpleNamespace(templates=registrations)) for key in registrations}
        self.template_config_sha256 = sha(config_path.read_bytes())
        self._install_synthetic_dependencies(fake_checkout)
        sys.addaudithook(no_network_or_process)
        self.wrapper_module = importlib.import_module("src.utils.generative_models.mattergen.mattergen_wrapper")
        # Import performs the original FastMCP registration and stdout isolation.
        self.server = importlib.import_module("src.mcp_server.mattergen_server")
        self._install_diffcsp_replay()
        self.diffcsp_server = importlib.import_module("src.mcp_server.diffcsp_server")
        self.identity.update(
            mode="upstream_functions_with_synthetic_engine",
            adapter_source_sha256=sha(Path(__file__).read_bytes()),
            science_computation_performed=False,
            backend=self.session.identity,
            template_config_sha256=self.template_config_sha256,
            public_templates=self.templates,
            executed_function_hashes={
                "server.generate_structures": sha(inspect.getsource(self.server.generate_structures).encode()),
                "wrapper.__init__": sha(inspect.getsource(self.wrapper_module.MatterGenWrapper.__init__).encode()),
                "wrapper.generate_structures": sha(inspect.getsource(self.wrapper_module.MatterGenWrapper.generate_structures).encode()),
                "diffcsp.server.generate_structures_with_symmetry": sha(inspect.getsource(self.diffcsp_server.generate_structures_with_symmetry).encode()),
                "diffcsp.wrapper.generate_with_symmetry": sha(inspect.getsource(self.diffcsp_wrapper.DiffCSPWrapper.generate_with_symmetry).encode())},
            substitutions=["torch.cuda.is_available returns False; no torch runtime loaded",
                           "MatterGenCheckpointInfo.from_hf_hub returns a named synthetic checkpoint; no weights or downloads",
                           "CrystalGenerator.generate consumes the condition-bound shared ReplaySession",
                           "DiffCSP constructor bypasses checkpoints; _build_syminfo_data uses only the registered CPU-verified template; _run_diffusion consumes the same ReplaySession",
                           "Configuration file paths redirected inside the isolated run; MLIP environment removed"],
            limitations=["This JSONL transport calls the actual registered function; it does not test the MCP JSON-RPC transport.",
                         "No real MatterGen checkpoint, CUDA runtime or scientific inference is tested.",
                         "DiffCSP PyXtal operations, torch geometric tensors and real checkpoint loading are not tested or claimed equivalent; only the registered template subset is available.",
                         "Skills are supplied verbatim; this worker cannot establish that an LLM followed them.",
                         "All synthetic fixture numbers and structures remain workflow test data, not material evidence."])
        exclusive_json(self.root / "driver-identity.json", self.identity)

    def _install_synthetic_dependencies(self, checkout: Path):
        worker = self
        torch = types.ModuleType("torch")
        torch.cuda = types.SimpleNamespace(is_available=lambda: False)
        torch.__spec__ = importlib.machinery.ModuleSpec("torch", loader=None)
        sys.modules["torch"] = torch
        for name in ("mattergen", "mattergen.common", "mattergen.common.utils", "mattergen.common.utils.data_classes", "mattergen.generator"):
            module = types.ModuleType(name)
            module.__file__ = str(checkout / (name.replace(".", "/") + ".py"))
            module.__path__ = []
            sys.modules[name] = module

        class SyntheticCheckpoint:
            def __init__(self, model_name):
                self.model_name = model_name

            @classmethod
            def from_hf_hub(cls, model_name, config_overrides):
                if config_overrides:
                    raise ValueError("Unreviewed checkpoint overrides are unavailable in replay.")
                return cls(model_name)

        class SyntheticCrystalGenerator:
            def __init__(self, checkpoint_info, properties_to_condition_on, batch_size,
                         num_batches, diffusion_guidance_factor, record_trajectories):
                self.model_name = checkpoint_info.model_name
                self.conditions = properties_to_condition_on
                self.guidance = diffusion_guidance_factor

            def generate(self, *, batch_size, num_batches, output_dir):
                from pymatgen.core import Structure
                request = {"engine": "mattergen", "model_name": self.model_name,
                           "conditions": self.conditions, "parameters": {"guidance_scale": self.guidance},
                           "num_samples": batch_size * num_batches}
                call = {"batch_size": batch_size, "num_batches": num_batches, "output_dir": output_dir,
                        "actual_engine_request": request}
                worker.generator_calls.append(call)
                try:
                    result = worker.session.invoke("sample", request)
                    call["replay_result"] = result
                    worker.returned_records.extend(result["records"])
                    return [Structure.from_file(record["path"]) for record in result["records"]]
                except ReplayError as exc:
                    call["replay_error"] = {"code": exc.code, "message": str(exc)}
                    raise

        sys.modules["mattergen.common.utils.data_classes"].MatterGenCheckpointInfo = SyntheticCheckpoint
        sys.modules["mattergen.generator"].CrystalGenerator = SyntheticCrystalGenerator

    def _install_diffcsp_replay(self):
        self.diffcsp_wrapper = importlib.import_module("src.utils.generative_models.diffcsp.diffcsp_wrapper")
        worker = self

        def synthetic_init(wrapper, model_name="mp_csp", device="auto", **kwargs):
            if kwargs or model_name not in {"mp_csp", "perov_csp", "mpts_csp"}:
                raise ReplayError("request_unavailable", "Only the common registered-template CSP subset is available.")
            wrapper.model_name, wrapper.device = model_name, "cpu"
            wrapper._is_gen_model = False

        def registered_symmetry(wrapper, spacegroup, wyckoff_letters, atom_types=None):
            letters = list(wyckoff_letters)
            atoms = list(atom_types or [])
            for template_id, template in worker.templates.items():
                if spacegroup != template["space_group"] or atoms != template["atom_types"] or len(letters) != len(template["wyckoff_letters"]):
                    continue
                if all(actual in {expected, expected[-1]} for actual, expected in zip(letters, template["wyckoff_letters"])):
                    return {"template_id": template_id, "template_sha256": template["sha256"],
                            "conditions": {"spacegroup": spacegroup, "wyckoff_letters": [x[-1] for x in template["wyckoff_letters"]], "atom_types": atoms}}
            raise ReplayError("template_unavailable", "The requested symmetry is outside the explicitly registered CPU template subset.")

        def replay_diffusion(wrapper, data_list, step_lr, batch_size):
            from pymatgen.core import Structure
            if not data_list or any(data != data_list[0] for data in data_list):
                raise ReplayError("request_unavailable", "A nonempty homogeneous registered-template batch is required.")
            request = {"engine": "diffcsp", "model_name": wrapper.model_name, "conditions": data_list[0]["conditions"],
                       "parameters": {"step_lr": step_lr}, "num_samples": len(data_list)}
            call = {"engine": "diffcsp", "batch_size": batch_size, "actual_engine_request": request,
                    "registered_template": {k: data_list[0][k] for k in ("template_id", "template_sha256")}}
            worker.generator_calls.append(call)
            try:
                result = worker.session.invoke("sample", request)
                call["replay_result"] = result
                worker.returned_records.extend(result["records"])
                records = {record["candidate_id"]: record for record in result["records"]}
                return [Structure.from_file(records[outcome["candidate_id"]]["path"]) if outcome["candidate_id"] else None for outcome in result["outcomes"]]
            except ReplayError as exc:
                call["replay_error"] = {"code": exc.code, "message": str(exc)}
                raise

        self.diffcsp_wrapper.DiffCSPWrapper.__init__ = synthetic_init
        self.diffcsp_wrapper.DiffCSPWrapper._build_syminfo_data = registered_symmetry
        self.diffcsp_wrapper.DiffCSPWrapper._run_diffusion = replay_diffusion

    def _output_dir(self, supplied):
        path = self.root / "upstream_outputs" / f"call-{self.tool_call_index:04d}" if supplied is None else Path(supplied)
        if not path.is_absolute():
            path = self.root / "upstream_outputs" / path
        path = path.resolve()
        if not path.is_relative_to(self.root / "upstream_outputs") or path.exists():
            raise ReplayError("invalid_output", "Use a new output directory inside this run's upstream_outputs directory.")
        return str(path)

    def generate(self, arguments: dict, engine="mattergen") -> dict:
        self.tool_call_index += 1
        passed = dict(arguments)
        passed["output_dir"] = self._output_dir(passed.get("output_dir"))
        start_call, start_record = len(self.generator_calls), len(self.returned_records)
        try:
            original = self.server.generate_structures(**passed) if engine == "mattergen" else self.diffcsp_server.generate_structures_with_symmetry(**passed)
        except Exception as exc:
            original = {"error": str(exc), "exception_type": type(exc).__name__, "raised_by_upstream_function": True}
        mapping = []
        if "error" not in original:
            records = self.returned_records[start_record:]
            paths = original.get("structures", [str(path) for path in sorted(Path(passed["output_dir"]).glob("structure_*.cif"))])
            if len(records) != len(paths):
                raise ReplayError("identity_mismatch", "Actual upstream outputs do not match replay candidate count.")
            for path, record in zip(paths, records):
                digest = sha(Path(path).read_bytes())
                mapping.append({"candidate_id": record["candidate_id"], "path": path, "sha256": digest,
                                "structure_version_id": "s_" + canonical_hash([record["candidate_id"], digest, record["structure_version_id"], "upstream_cif_serialization"]),
                                "parent_version_id": record["structure_version_id"],
                                "transformation": "upstream_cif_serialization", "source_artifact": record["path"],
                                "provenance": record["provenance"], "evidence_scope": record["evidence_scope"],
                                "metric_inheritance": "none; property evidence remains bound to its explicit input version"})
        for path in Path(passed["output_dir"]).rglob("*"):
            if path.is_file():
                relative = path.relative_to(self.root / "upstream_outputs").as_posix()
                self.upstream_artifacts[relative] = {"relative_path": relative, "path": str(path), "sha256": sha(path.read_bytes())}
        result = {"upstream_result": original, "generator_calls": self.generator_calls[start_call:],
                  "candidate_mapping": mapping, "replay_status": self.session.invoke("status"),
                  "identity": self.identity}
        exclusive_json(self.root / f"upstream-call-{self.tool_call_index:04d}.json", {"arguments": passed, **result})
        return result

    def invoke(self, operation, arguments):
        if operation == "identity" and not arguments:
            return self.identity
        if operation == "describe_tools" and not arguments:
            tool = self.server.mcp._tool_manager.get_tool("generate_structures")
            diff_tool = self.diffcsp_server.mcp._tool_manager.get_tool("generate_structures_with_symmetry")
            return {"tools": [{"name": item.name, "description": item.description, "parameters": item.parameters} for item in (tool, diff_tool)],
                    "normal_instructions": self.identity["normal_instructions"], "identity": self.identity}
        if operation == "generate_structures":
            return self.generate(arguments)
        if operation == "generate_structures_with_symmetry":
            return self.generate(arguments, "diffcsp")
        if operation == "get_templates" and not arguments:
            return {"templates": list(self.templates.values())}
        if operation == "list_upstream_artifacts" and not arguments:
            return {"artifacts": list(self.upstream_artifacts.values())}
        if operation == "read_upstream_artifact" and set(arguments) == {"relative_path"}:
            artifact = self.upstream_artifacts.get(arguments["relative_path"])
            if not artifact:
                raise ReplayError("artifact_unavailable", "Choose an artifact returned by list_upstream_artifacts.")
            content = Path(artifact["path"]).read_bytes()
            if sha(content) != artifact["sha256"]:
                raise ReplayError("artifact_changed", "Upstream artifact changed after tool completion.")
            return {**artifact, "content": content.decode("utf-8")}
        if operation == "replay" and set(arguments) <= {"operation", "arguments"}:
            if arguments.get("operation") not in {"status", "list_artifacts", "read_artifact", "get_candidates", "get_metrics",
                                                   "get_versions", "query_database", "verify_candidate_artifact"}:
                raise ReplayError("invalid_request", "Raw engine access is unavailable; use the actual upstream generation functions.")
            return self.session.invoke(arguments["operation"], arguments.get("arguments", {}))
        if operation == "trace_receipt" and not arguments:
            return verify_trace(self.session.root)
        raise ReplayError("invalid_request", "Unknown worker operation.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=["U", "U+"], required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--sampling-cap", type=int)
    parser.add_argument("--formal", action="store_true")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--review")
    parser.add_argument("--expected-review-sha256")
    parser.add_argument("--template-config")
    args = parser.parse_args(argv)
    # Keep a separate descriptor before the original MCP setup redirects stdout.
    transport = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    worker = UpstreamWorker(args)
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) - {"id", "operation", "method", "arguments"} or ("operation" in request and "method" in request):
                raise ReplayError("invalid_request", "Unexpected JSONL envelope.")
            result = worker.invoke(request.get("operation", request.get("method")), request.get("arguments", {}))
            response = {"id": request.get("id"), "result": result}
        except Exception as exc:
            response = {"id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": getattr(exc, "code", "upstream_worker_error"), "message": str(exc)}}
        transport.write((json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
