"""Run the published U+ patch against fixed source; models/network are replaced with fixtures."""
import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback
import types
import typing
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "benchmarks/baseline"


def extracted(path, name, scope=None, globals_extra=None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = next(n.body for n in tree.body if isinstance(n, ast.ClassDef) and n.name == scope) if scope else tree.body
    node = next(n for n in body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    env = {"Dict": typing.Dict, "Any": typing.Any, "Optional": typing.Optional, "List": typing.List,
           "Path": Path, "os": os, "sys": sys, "json": json, "traceback": traceback,
           "recursive_tolist": lambda x: x, "logger": types.SimpleNamespace(info=lambda *a: None, error=lambda *a: None)}
    env.update(globals_extra or {})
    exec(compile(module, str(path), "exec"), env)
    return env[name]


class BaselinePatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = ROOT / ".work/test-temp"
        base.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=base)
        cls.root = Path(cls.temp.name)
        shutil.copytree(BASELINE / "upstream_snapshot", cls.root / "upstream")
        cls.upstream = cls.root / "upstream"
        lock = json.loads((BASELINE / "source-lock.json").read_text())
        for item in lock["files"]:
            if hashlib.sha256((cls.upstream / item["path"]).read_bytes()).hexdigest() != item["original_sha256"]:
                raise ValueError("Baseline snapshot identity mismatch")
        subprocess.run(["git", "-C", str(cls.upstream), "apply", str(BASELINE / "atomisticskills-uplus.patch")], check=True, capture_output=True)
        for item in lock["files"]:
            if hashlib.sha256((cls.upstream / item["path"]).read_text(encoding="utf-8").encode("utf-8")).hexdigest() != item["patched_sha256"]:
                raise ValueError("Applied baseline patch identity mismatch")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_combined_conditions_retained_or_explicitly_rejected(self):
        captured = []
        class Wrapper:
            def __init__(self, **kw):
                captured.append(kw)
            def generate_structures(self, **kw):
                return {"fixture": True}
        fake = types.ModuleType("src.utils.generative_models.mattergen.mattergen_wrapper")
        fake.MatterGenWrapper = Wrapper
        fn = extracted(self.upstream / "src/mcp_server/mattergen_server.py", "generate_structures")
        with patch.dict(sys.modules, {fake.__name__: fake}), contextlib.redirect_stderr(io.StringIO()):
            bad = fn(chemical_system="Fe-O", model_name="dft_mag_density", properties={"dft_mag_density": .1}, output_dir=str(self.root / "result"))
            self.assertIn("error", bad)
            self.assertFalse(captured)
            good = fn(chemical_system="Fe-O", properties={"energy_above_hull": .05}, output_dir=str(self.root / "result"))
        self.assertTrue(good["fixture"])
        self.assertEqual(captured[0]["properties_to_condition_on"], {"chemical_system": "Fe-O", "energy_above_hull": .05})
        self.assertEqual(captured[0]["model_name"], "chemical_system_energy_above_hull")

    def test_exact_odd_sampling_budget_and_partial_return(self):
        calls = []
        class Crystal:
            def to(self, filename, fmt):
                Path(filename).write_text("FIXTURE ONLY")
        class Generator:
            short = False
            def generate(self, **kw):
                calls.append(kw["batch_size"])
                return [Crystal()] * (1 if self.short else kw["batch_size"])
        generator = Generator()
        wrapper = types.SimpleNamespace(is_loaded=True, generator=generator, model_name="test", properties_to_condition_on={}, guidance_scale=0)
        fn = extracted(self.upstream / "src/utils/generative_models/mattergen/mattergen_wrapper.py", "generate_structures", "MatterGenWrapper")
        result = fn(wrapper, num_structures=3, batch_size=2, output_dir=str(self.root / "counts"))
        self.assertEqual(calls, [2, 1])
        self.assertEqual(result["num_generated"], 3)
        self.assertEqual(result["sampling_attempts"], 3)
        generator.short = True
        calls.clear()
        result = fn(wrapper, num_structures=3, batch_size=2, output_dir=str(self.root / "partial"))
        self.assertEqual(calls, [2])
        self.assertEqual(result["status"], "partial")

    def test_same_formula_selection_requires_unique_candidate(self):
        from pymatgen.core import Composition
        class PhaseDiagramFixture:
            stable_entries = []
            def __init__(self, entries): pass
            def get_e_above_hull(self, entry): return entry.fixture_value
            def get_decomposition(self, comp): return {}
        entries = [types.SimpleNamespace(entry_id="c_a", composition=Composition("LiCl"), fixture_value=.0), types.SimpleNamespace(entry_id="c_b", composition=Composition("LiCl"), fixture_value=.25)]
        fn = extracted(self.upstream / "skills/mat-stability/scripts/compute_ehull.py", "compute_hull_analysis", globals_extra={"ComputedEntry": object, "Composition": Composition, "PhaseDiagram": PhaseDiagramFixture})
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                fn(entries, "LiCl")
            result = fn(entries, "LiCl", target_id="c_b")
            reverse = fn(list(reversed(entries)), "LiCl", target_id="c_b")
        self.assertEqual(result["target_id"], "c_b")
        self.assertEqual(result["energy_above_hull_meV"], reverse["energy_above_hull_meV"])

    def test_query_failure_is_unknown_not_novel(self):
        import argparse
        from pymatgen.core import Structure, Lattice
        from pymatgen.analysis.structure_matcher import StructureMatcher
        crystal = Structure(Lattice.cubic(4), ["Na", "Cl"], [[0,0,0],[.5,.5,.5]])
        class MPResterFixture:
            def __enter__(self):
                self.materials = types.SimpleNamespace(summary=types.SimpleNamespace(search=self.search))
                return self
            def __exit__(self, *args): pass
            def search(self, **kw): raise RuntimeError("injected database unavailable")
        client = types.ModuleType("mp_api.client")
        client.MPRester = MPResterFixture
        config = types.ModuleType("src.utils.config_utils")
        config.save_skill_inputs = lambda *a: None
        lit = types.ModuleType("src.utils.literature_utils")
        lit.query_openalex = lambda *a, **kw: self.fail("Must not infer novelty after failed database query")
        output = self.root / "match.json"
        fn = extracted(self.upstream / "skills/mat-structure-novelty/scripts/match_structure.py", "main", globals_extra={"argparse": argparse, "Structure": Structure, "StructureMatcher": StructureMatcher, "load_structures": lambda p: [crystal], "get_identifier": lambda s, default: "fixture"})
        with patch.dict(sys.modules, {client.__name__: client, config.__name__: config, lit.__name__: lit}), patch.object(sys, "argv", ["test", "fixture.cif", "MP", "--output", str(output)]), contextlib.redirect_stdout(io.StringIO()) as printed:
            fn()
        results = json.loads(output.read_text())
        self.assertEqual(results[0]["status"], "UNKNOWN")
        self.assertIsNone(results[0]["match_found"])
        self.assertNotIn("NOVEL", printed.getvalue())


if __name__ == "__main__":
    unittest.main()
