"""MatterGen capability contract and strict, lossless request validation.

MG-Flow's condition names and argument-array approach are retained. Unlike its
best-subset selector, this adapter never silently drops an input constraint.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Protocol


CHECKPOINTS = {
    "mattergen_base": [], "mp_20_base": [],
    "chemical_system": ["chemical_system"],
    "space_group": ["space_group"],
    "dft_band_gap": ["dft_band_gap"],
    "ml_bulk_modulus": ["ml_bulk_modulus"],
    "dft_mag_density": ["dft_mag_density"],
    "dft_mag_density_hhi_score": ["dft_mag_density", "hhi_score"],
    "chemical_system_energy_above_hull": ["chemical_system", "energy_above_hull"],
}
UNITS = {"energy_above_hull": "eV/atom", "dft_band_gap": "eV", "ml_bulk_modulus": "GPa",
         "dft_mag_density": "mu_B/angstrom^3", "hhi_score": "dimensionless", "space_group": "number"}
CONVERSIONS = {("meV/atom", "eV/atom"): 0.001, ("meV", "eV"): 0.001, ("MPa", "GPa"): 0.001}
ELEMENTS = set("H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og".split())
VERSION = "mattergen-contract-2026-10-21-v1"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class ModelAdapter(Protocol):
    def describe(self) -> dict: ...
    def validate(self, request: dict, max_samples: int = 32, settings: Any = None) -> dict: ...
    def build_command(self, plan: dict, output: str, count: int, seed: int, settings: Any) -> list[str]: ...


class MatterGenAdapter:
    def describe(self) -> dict:
        return {"model_id": "mattergen", "adapter_version": VERSION,
                "tasks": ["inorganic_crystal_generation"], "checkpoints": CHECKPOINTS,
                "units": UNITS, "condition_semantics": "Numeric values are conditioning targets, not guaranteed bounds or measured properties.",
                "limitations": ["Cannot enforce arbitrary combinations of conditions with the listed checkpoints.",
                    "chemical_system selects elements, not fixed stoichiometry.",
                    "Catalysis, corrosion, synthesis and device performance require downstream evidence."],
                "source": "https://github.com/microsoft/mattergen", "license": "Check upstream code and checkpoint licenses separately."}

    def validate(self, request: dict, max_samples: int = 32, settings: Any = None) -> dict:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        issues, conditions = [], {}
        allowed = {"raw_request", "model_id", "checkpoint", "conditions", "review_targets", "num_samples", "batch_size", "seed", "unconditional", "clarifications", "requirements", "search_mode", "valid_candidates_target"}
        for key in set(request) - allowed:
            issues.append({"code": "unknown_field", "field": key, "message": "Unknown field; no requirement has been discarded."})
        raw = request.get("raw_request", "")
        if not isinstance(raw, str) or len(raw) > 20000:
            issues.append({"code": "invalid_request", "message": "raw_request must be text up to 20000 characters."})
        if request.get("model_id", "mattergen") != "mattergen":
            issues.append({"code": "unsupported_model", "message": "This adapter accepts model_id=mattergen only."})
        incoming = request.get("conditions", {})
        if not isinstance(incoming, dict):
            issues.append({"code": "invalid_conditions", "message": "conditions must be an object."})
            incoming = {}
        for key, supplied in incoming.items():
            try:
                if key == "chemical_system":
                    if not isinstance(supplied, str):
                        raise ValueError("Use element symbols separated by hyphens, such as Fe-Ni-O.")
                    elements = supplied.split("-")
                    if not elements or any(e not in ELEMENTS for e in elements):
                        raise ValueError("Use valid element symbols; chemical formulae such as LiFePO4 are not chemical_system values.")
                    if len(set(elements)) != len(elements):
                        raise ValueError("Duplicate elements require correction.")
                    conditions[key] = "-".join(sorted(elements))
                    continue
                if key not in UNITS:
                    raise ValueError("Unsupported generation condition. Keep downstream goals in review_targets only with an explicit explanation.")
                if isinstance(supplied, dict):
                    if set(supplied) - {"value", "unit", "relation"}:
                        raise ValueError("Unknown condition metadata.")
                    if supplied.get("relation", "target") != "target":
                        raise ValueError("A bound or range cannot be silently converted to a conditioning target. Clarify first.")
                    number = supplied.get("value")
                    unit = supplied.get("unit", UNITS[key])
                else:
                    number, unit = supplied, UNITS[key]
                if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                    raise ValueError("A finite numeric target is required.")
                if unit != UNITS[key]:
                    if (unit, UNITS[key]) not in CONVERSIONS:
                        raise ValueError(f"Expected unit {UNITS[key]}.")
                    number *= CONVERSIONS[unit, UNITS[key]]
                if number < 0:
                    raise ValueError("This target must be nonnegative.")
                if key == "space_group":
                    if int(number) != number or not 1 <= number <= 230:
                        raise ValueError("Space group must be an integer from 1 to 230.")
                    number = int(number)
                conditions[key] = number
            except (ValueError, TypeError) as exc:
                issues.append({"code": "invalid_condition", "field": key, "message": str(exc)})
        checkpoint = request.get("checkpoint")
        keys = set(conditions)
        if checkpoint is not None and checkpoint not in CHECKPOINTS:
            issues.append({"code": "unknown_checkpoint", "message": "Checkpoint is not in the versioned capability registry."})
        elif checkpoint is not None and not keys <= set(CHECKPOINTS[checkpoint]):
            issues.append({"code": "incompatible_conditions", "message": f"{checkpoint} cannot accept all the supplied conditions."})
        elif checkpoint is None:
            matches = [name for name, fields in CHECKPOINTS.items() if keys == set(fields)]
            if matches:
                checkpoint = matches[0]
            else:
                issues.append({"code": "incompatible_conditions", "message": "No checkpoint supports this complete combination. Choose a revised task explicitly."})
        unconditional = request.get("unconditional", False)
        if not isinstance(unconditional, bool):
            issues.append({"code": "invalid_unconditional", "message": "unconditional must be boolean."})
        if not incoming and unconditional is not True:
            issues.append({"code": "clarification_required", "message": "Provide generation conditions or explicitly request unconditional generation."})
        if unconditional is True and incoming:
            issues.append({"code": "conflicting_mode", "message": "Unconditional generation cannot also have conditions."})
        values = {}
        for name, default, low, high in [("num_samples", 16, 1, max_samples), ("batch_size", 2, 1, max_samples), ("seed", 42, 0, 2**31-1)]:
            value = request.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                issues.append({"code": "invalid_budget", "field": name, "message": f"Expected integer in [{low}, {high}]."})
                value = default
            values[name] = value
        review = request.get("review_targets", [])
        if not isinstance(review, list) or len(review) > 30 or any(not isinstance(x, str) or not x.strip() or len(x) > 1000 for x in review):
            issues.append({"code": "invalid_review_targets", "message": "review_targets must be a list of up to 30 nonempty strings."})
            review = []
        clarifications = request.get("clarifications", [])
        if not isinstance(clarifications, list) or any(not isinstance(x, str) for x in clarifications):
            issues.append({"code": "invalid_clarifications", "message": "clarifications must be a list of strings."})
        elif clarifications:
            issues.extend({"code": "clarification_required", "message": x} for x in clarifications)
        search_mode = request.get("search_mode", "direct")
        if search_mode not in {"direct", "generate_then_filter"}:
            issues.append({"code": "invalid_search_mode", "message": "Choose direct or explicitly authorized generate_then_filter search."})
        valid_target = request.get("valid_candidates_target")
        if valid_target is not None and (type(valid_target) is not int or not 1 <= valid_target <= values["num_samples"]):
            issues.append({"code": "invalid_valid_target", "message": "Valid-candidate target must be a positive integer no larger than the maximum sampling budget."})
        blocked = any(i["code"] != "clarification_required" for i in issues)
        plan = {"schema_version": 1, "model_id": "mattergen", "adapter_version": VERSION,
                "raw_request": raw, "checkpoint": checkpoint, "conditions": conditions,
                "review_targets": review, **values, "search_mode": search_mode, "valid_candidates_target": valid_target,
                "state": "blocked" if blocked else "needs_clarification" if issues else "ready",
                "issues": issues, "original_request": request,
                "scientific_status": "Generation targets do not prove material properties. Structural checks are not DFT or experimental validation."}
        plan["plan_id"] = canonical_hash(plan)
        return plan

    def build_command(self, plan: dict, output: str, count: int, seed: int, settings: Any) -> list[str]:
        args = list(settings.command) + [output]
        local = settings.checkpoint_paths.get(plan["checkpoint"])
        args += [f"--model_path={local}" if local else f"--pretrained-name={plan['checkpoint']}",
                 f"--batch_size={count}", "--num_batches=1", "--record_trajectories=False"]
        if settings.seed_supported:
            args.append(f"--seed={seed}")
        if plan["conditions"]:
            args += [f"--properties_to_condition_on={repr(plan['conditions'])}", "--diffusion_guidance_factor=2.0"]
        if settings.backend == "wsl":
            prefix = ["wsl.exe", "-d", settings.wsl_distribution]
            if settings.wsl_user:
                prefix += ["-u", settings.wsl_user]
            supervisor = wsl_path(Path(__file__).resolve().parents[1] / "compute_process.py")
            return prefix + ["--exec", "/usr/bin/python3", supervisor,
                             "--control-dir", output, "--timeout", str(settings.batch_timeout_seconds), "--"] + args
        return args


def wsl_path(value: Path) -> str:
    value = value.resolve()
    if not value.drive or value.drive.startswith("\\"):
        raise ValueError("WSL output must use a local Windows drive path.")
    return "/mnt/" + value.drive[0].lower() + value.as_posix()[2:]
