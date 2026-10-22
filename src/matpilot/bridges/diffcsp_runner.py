"""Standalone isolated-environment runner. Importing this module never loads a model."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import zipfile

WRAPPER = "src/utils/generative_models/diffcsp/diffcsp_wrapper.py"


def verify_checkout(root, expected):
    root = Path(root).resolve()
    actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if actual != expected:
        raise ValueError("Upstream git revision differs from the locked revision.")
    dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"], text=True).strip()
    if dirty:
        raise ValueError("Upstream checkout has modifications or untracked files; use a clean pinned checkout.")
    return actual


def verify_weights(model_dir, manifest):
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("An explicit per-file weight/config manifest is required.")
    required = {"hparams.yaml"} | {p.name for p in model_dir.glob("*.ckpt")} | {p.name for p in model_dir.glob("*scaler.pt")}
    if len(list(model_dir.glob("*.ckpt"))) != 1 or set(manifest) != required:
        raise ValueError("Use exactly one checkpoint; manifest must include hparams and all checkpoint/scaler files.")
    for name, digest in manifest.items():
        if Path(name).name != name or not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise ValueError("Invalid manifest identity.")
        if hashlib.sha256((model_dir / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Weight/config checksum mismatch: " + name)


def verify_symmetry(template):
    from pyxtal.symmetry import Group
    from pymatgen.core import Composition, Lattice
    group = Group(template["space_group"])
    lattice = Lattice(template["lattice_angstrom"])
    composition = {}
    if len(template["wyckoff_letters"]) != len(template["atom_types"]):
        raise ValueError("Wyckoff/element counts differ.")
    orbits = template["orbit_fractional_positions"]
    if len(orbits) != len(template["wyckoff_letters"]):
        raise ValueError("Missing template orbit positions.")
    for label, element, positions in zip(template["wyckoff_letters"], template["atom_types"], orbits):
        match = re.fullmatch(r"([1-9][0-9]*)([a-zA-Z])", label)
        if not match:
            raise ValueError("Invalid Wyckoff label.")
        operations = group[match[2]].ops
        if len(operations) != int(match[1]) or len(positions) != int(match[1]):
            raise ValueError("Template and PyXtal Wyckoff multiplicity disagree.")
        generated = [op.operate(positions[0]) for op in operations]
        distances = lattice.get_all_distances(generated, positions)
        if any(distances.min(axis=0) > 0.01) or any(distances.min(axis=1) > 0.01):
            raise ValueError("PyXtal setting/origin differs from the template; review the orbit mapping explicitly.")
        composition[element] = composition.get(element, 0) + int(match[1])
    if sum(composition.values()) != template["site_count"] or not Composition(composition).reduced_composition.almost_equals(Composition(template["chemical_formula"]).reduced_composition):
        raise ValueError("Wyckoff-derived composition differs from the reviewed template.")


def collect_outputs(result, output, requested):
    paths = result.get("structures")
    if not isinstance(paths, list) or result.get("num_generated") != len(paths) or len(paths) > requested:
        raise ValueError("Upstream output count is inconsistent.")
    checked = []
    for raw in paths:
        path = Path(raw).resolve()
        path.relative_to(output.resolve())
        if path.suffix.lower() != ".cif" or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Invalid generated CIF artifact.")
        if path in checked:
            raise ValueError("Duplicate artifact path in upstream results.")
        checked.append(path)
    with zipfile.ZipFile(output / "generated_crystals_cif.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for i, path in enumerate(checked):
            archive.write(path, f"structure_{i:04d}.cif")
    return len(checked)


def run(payload):
    # No model imports, checkpoint deserialization or inference before this gate.
    if payload.get("real_compute_enabled") is not True:
        raise ValueError("Real scientific computation is disabled by operator policy.")
    conf = payload["upstream"]
    upstream = Path(conf["upstream_root"]).resolve()
    model_root = Path(conf["model_root"]).resolve()
    verify_checkout(upstream, payload["expected_wrapper_commit"])
    verify_checkout(model_root, conf["model_revision"])
    wrapper_path = upstream / WRAPPER
    if hashlib.sha256(wrapper_path.read_bytes()).hexdigest() != payload["expected_wrapper_sha256"]:
        raise ValueError("Wrapper source differs from the audited file.")
    checkpoint = payload["checkpoint"]
    if checkpoint not in {"mp_csp", "perov_csp", "mpts_csp"}:
        raise ValueError("Unsupported checkpoint.")
    verify_weights(model_root / "checkpoints" / checkpoint, conf["weight_manifest"])
    verify_symmetry(payload["template"])
    count, seed = payload["count"], payload["seed"]
    if type(count) is not int or not 1 <= count <= 32 or type(seed) is not int or not 0 <= seed < 2**31:
        raise ValueError("Invalid bounded sampling parameters.")
    output = Path(payload["output"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.chdir(model_root)
    os.environ["DIFFCSP_REPO"] = str(model_root)
    os.environ["USE_WANDB_LOGGING"] = "0"
    import numpy as np
    import torch
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    spec = importlib.util.spec_from_file_location("matpilot_pinned_diffcsp_wrapper", wrapper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    wrapper = module.DiffCSPWrapper(model_name=checkpoint, device=conf.get("device", "auto"))
    t = payload["template"]
    result = wrapper.generate_with_symmetry(spacegroup=t["space_group"], wyckoff_letters=t["wyckoff_letters"],
        atom_types=t["atom_types"], num_samples=count, batch_size=count, step_lr=1e-5, output_dir=str(output))
    received = collect_outputs(result, output, count)
    (output / "bridge-evidence.json").write_text(json.dumps({"requested": count, "returned": received,
        "status": "complete" if received == count else "partial", "seed": seed, "template": t,
        "wrapper_commit": payload["expected_wrapper_commit"], "wrapper_sha256": payload["expected_wrapper_sha256"],
        "model_revision": conf["model_revision"], "weight_manifest": conf["weight_manifest"],
        "scientific_status": "unvalidated_generated_structures"}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    run(json.loads(Path(args.input).read_text(encoding="utf-8")))
