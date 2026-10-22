"""Isolated source-behavior probes. No model, GPU, API, or MCP server is run.

Selected function ASTs are taken from the pinned upstream checkout. Only their
heavy dependency boundaries are replaced. Synthetic entries are not materials
discovery results. This is neither an end-to-end test nor an agent benchmark.
"""
import ast
import contextlib
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import traceback
from types import SimpleNamespace
from typing import Any, Dict, Optional, List

HERE = Path(__file__).resolve().parent
LOCAL = HERE.parents[1]
UP = Path(os.environ.get("ATOMISTICSKILLS_ROOT", str(LOCAL / "benchmarks/baseline/upstream_snapshot")))
SHA = "6afdd465a35088136e4875d7d2fbaefc633f4adf"
sources = {}


def selected_function(path, name, remove_imports=False):
    source = path.read_text(encoding="utf-8")
    identity = "AtomisticSkills/" + path.relative_to(UP).as_posix() if path.is_relative_to(UP) else "MatPilot/" + path.relative_to(LOCAL).as_posix()
    sources[identity] = hashlib.sha256(path.read_bytes()).hexdigest()
    tree = ast.parse(source)
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(functions) == 1, (path, name)
    function = functions[0]
    function.decorator_list = []
    if remove_imports:
        class StripImports(ast.NodeTransformer):
            def visit_ImportFrom(self, node):
                return ast.copy_location(ast.Pass(), node)
            def visit_Import(self, node):
                return ast.copy_location(ast.Pass(), node)
        function = StripImports().visit(function)
    return compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(path), "exec")


def run():
    observations = []
    with tempfile.TemporaryDirectory(prefix="probe-", dir=HERE) as temp:
        namespace = dict(globals(), recursive_tolist=lambda x: x)
        captured = {}

        class StubWrapper:
            def __init__(self, **kwargs):
                captured.update(kwargs)
            def generate_structures(self, **kwargs):
                return {"num_generated": kwargs["num_structures"], "probe_only": True}

        namespace["MatterGenWrapper"] = StubWrapper
        exec(selected_function(UP / "src/mcp_server/mattergen_server.py", "generate_structures", True), namespace)
        namespace["generate_structures"](
            num_structures=3, model_name="dft_mag_density", chemical_system="Fe-Ni-O",
            properties={"dft_mag_density": 0.15}, output_dir=temp,
        )
        assert captured["model_name"] == "chemical_system"
        assert captured["properties_to_condition_on"] == {"chemical_system": "Fe-Ni-O"}
        observations.append({"id": "P1", "kind": "isolated_function_with_stub",
            "observation": "chemical_system replaces the supplied checkpoint and properties",
            "captured_wrapper_arguments": captured,
            "limit": "The tool documents this precedence. This does not measure how often an agent misuses it."})

        class FakeStructure:
            def to(self, filename, fmt):
                Path(filename).write_text("PROBE ONLY - NOT A CIF\n", encoding="utf-8")

        generation_call = {}
        class FakeGenerator:
            def generate(self, **kwargs):
                generation_call.update(kwargs)
                return [FakeStructure() for _ in range(kwargs["batch_size"] * kwargs["num_batches"])]

        wrapper_self = SimpleNamespace(is_loaded=True, generator=FakeGenerator(), model_name="probe",
            properties_to_condition_on={}, guidance_scale=0.0)
        namespace = dict(globals(), Structure=FakeStructure, logger=logging.getLogger("probe"))
        exec(selected_function(UP / "src/utils/generative_models/mattergen/mattergen_wrapper.py", "generate_structures", True), namespace)
        result = namespace["generate_structures"](wrapper_self, num_structures=3, batch_size=2, output_dir=temp)
        assert generation_call["num_batches"] == 2 and result["num_generated"] == 4
        observations.append({"id": "P2", "kind": "isolated_wrapper_with_stub_generator",
            "requested": 3, "batch_size": 2, "batches_passed_to_generator": 2,
            "stub_structures_returned": result["num_generated"],
            "limit": "Confirms ceil batching and no truncation in this wrapper; actual model behavior and cost were not measured."})

        from pymatgen.core import Composition
        from pymatgen.entries.computed_entries import ComputedEntry
        from pymatgen.analysis.phase_diagram import PhaseDiagram
        namespace = dict(globals(), Composition=Composition, ComputedEntry=ComputedEntry, PhaseDiagram=PhaseDiagram)
        exec(selected_function(UP / "skills/mat-stability/scripts/compute_ehull.py", "compute_hull_analysis"), namespace)
        pure = [ComputedEntry("Li", -1.0, entry_id="Li"), ComputedEntry("Cl", -1.0, entry_id="Cl")]
        reference = ComputedEntry("LiCl", -2.5, entry_id="reference")
        candidate = ComputedEntry("LiCl", -2.0, entry_id="candidate")
        with contextlib.redirect_stdout(io.StringIO()):
            first = namespace["compute_hull_analysis"](pure + [reference, candidate], "LiCl")
            second = namespace["compute_hull_analysis"](pure + [candidate, reference], "LiCl")
        assert abs(first["energy_above_hull_meV"]) < 1e-8
        assert abs(second["energy_above_hull_meV"] - 250) < 1e-8
        observations.append({"id": "P3", "kind": "isolated_function_real_pymatgen_synthetic_energies",
            "reference_first_meV_per_atom": first["energy_above_hull_meV"],
            "candidate_first_meV_per_atom": second["energy_above_hull_meV"],
            "observation": "Formula-only target selection picks the first matching entry, so candidate identity can be ambiguous.",
            "limit": "Two synthetic same-composition entries; no physical energy calculation was performed."})

        adapter_path = LOCAL / "src/matpilot/adapters/mattergen.py"
        source = adapter_path.read_text(encoding="utf-8")
        sources[str(adapter_path)] = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
        namespace = {"__file__": str(adapter_path), "__name__": "matpilot_adapter_probe"}
        exec(compile(source, str(adapter_path), "exec"), namespace)
        adapter = namespace["MatterGenAdapter"]()
        conflict = adapter.validate({"raw_request": "Fe-Ni-O and band gap target 2 eV",
            "conditions": {"chemical_system": "Fe-Ni-O", "dft_band_gap": {"value": 2, "unit": "eV"}}})
        omitted = adapter.validate({"raw_request": "Fe-Ni-O and band gap target 2 eV",
            "conditions": {"chemical_system": "Fe-Ni-O"}})
        assert conflict["state"] == "blocked" and omitted["state"] == "ready"
        observations.append({"id": "P4", "kind": "current_MatPilot_validator_no_model",
            "complete_conflicting_conditions": conflict["state"],
            "condition_omitted_before_validation": omitted["state"],
            "observation": "Current validation handles supplied structured constraints but cannot guarantee coverage of raw language.",
            "limit": "Does not imply a real LLM omitted this condition; it exposes the validator boundary."})

    report = {"upstream_commit": SHA, "scope": "CPU-only isolated source probes; no agent benchmark or GPU inference",
              "observations": observations, "source_sha256": sources}
    target = HERE / "probe_results.json"
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    run()
