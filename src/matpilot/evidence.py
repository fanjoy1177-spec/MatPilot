"""Candidate identity, method comparability and three-valued requirement checks."""
from __future__ import annotations

import hashlib
import importlib.metadata
import math
import re
from pathlib import Path

from .adapters.mattergen import canonical_hash


def candidate_identity(job_id: str, batch_index: int, record_index: int, record: dict) -> dict:
    candidate_id = "c_" + canonical_hash([job_id, batch_index, record_index])[:24]
    version_id = "s_" + canonical_hash([candidate_id, record["sha256"], None, "generated"])
    return {**record, "candidate_id": candidate_id, "structure_version_id": version_id,
            "parent_version_id": None, "transformation": "generated"}


def verify_artifact(artifact: dict) -> None:
    if not isinstance(artifact, dict) or not artifact.get("path") or not artifact.get("sha256"):
        raise ValueError("Artifact needs an explicit path and SHA256.")
    file = Path(artifact["path"])
    if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != artifact["sha256"]:
        raise ValueError("Artifact is missing or its checksum has changed.")


def validate_metric(metric: dict, version: dict) -> dict:
    required = {"candidate_id", "structure_version_id", "property", "value", "unit", "method",
                "input_artifact", "raw_output", "convergence", "reference_set"}
    if not isinstance(metric, dict) or set(metric) != required:
        raise ValueError("Metric must contain the complete evidence schema; unknown metadata remains null.")
    if metric["candidate_id"] != version["candidate_id"] or metric["structure_version_id"] != version["structure_version_id"]:
        raise ValueError("Metric belongs to another candidate or structure version.")
    value = metric["value"]
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
        raise ValueError("Metric value must be finite or explicitly unknown.")
    if not isinstance(metric["property"], str) or not metric["property"]:
        raise ValueError("Metric property is required.")
    if metric["unit"] is not None and not isinstance(metric["unit"], str):
        raise ValueError("Unit must be text or null.")
    if not isinstance(metric["method"], dict) or set(metric["method"]) != {"name", "version", "weights_identity", "parameters"}:
        raise ValueError("Method name, version, weights identity and parameters must be recorded, using null when unknown.")
    if any(v is not None and not isinstance(v, str) for k, v in metric["method"].items() if k != "parameters") or (metric["method"]["parameters"] is not None and not isinstance(metric["method"]["parameters"], dict)):
        raise ValueError("Method identities must be strings or null; parameters must be an object or null.")
    if metric["convergence"] not in {"converged", "not_converged", "unknown", "not_applicable"}:
        raise ValueError("Unknown convergence status.")
    if metric["reference_set"] is not None and (not isinstance(metric["reference_set"], dict) or set(metric["reference_set"]) != {"id", "sha256"}):
        raise ValueError("Reference set needs an identity and digest, or null.")
    refs = metric["reference_set"]
    if refs is not None and (not isinstance(refs["id"], str) or not refs["id"].strip() or not isinstance(refs["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", refs["sha256"])):
        raise ValueError("Reference-set identity and SHA256 must be explicit and valid.")
    verify_artifact(metric["input_artifact"])
    verify_artifact(metric["raw_output"])
    if metric["input_artifact"]["sha256"] != version["sha256"]:
        raise ValueError("Evaluator input is not the selected structure version.")
    return {**metric, "metric_id": "m_" + canonical_hash(metric)}


def _known_identity(value) -> bool:
    return isinstance(value, str) and value.strip().lower() not in {"", "unknown", "unverified", "not_measured"}


def _known_method(metric: dict) -> bool:
    method = metric.get("method", {})
    return all(_known_identity(method.get(k)) for k in ("name", "version", "weights_identity")) and isinstance(method.get("parameters"), dict)


def metric_usable(metric: dict) -> bool:
    if metric.get("value") is None or not _known_identity(metric.get("unit")) or not _known_method(metric):
        return False
    if metric.get("convergence") not in {"converged", "not_applicable"}:
        return False
    if metric["property"] in {"energy_above_hull", "formation_energy"}:
        refs = metric.get("reference_set")
        if not refs or not _known_identity(refs.get("id")) or not isinstance(refs.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", refs["sha256"]):
            return False
    return True


def compare_metrics(metrics: list[dict]) -> dict:
    """Comparability is reported; no ranking of method-mismatched numbers is emitted."""
    reasons = []
    if len(metrics) < 2:
        reasons.append("At least two metric records are required.")
    if not all(metric_usable(m) for m in metrics):
        reasons.append("One or more measurements have missing method, convergence, unit or reference metadata.")
    keys = {canonical_hash({k: m.get(k) for k in ("property", "unit", "method", "reference_set")}) for m in metrics}
    if len(keys) != 1:
        reasons.append("Property, units, scientific method or reference sets differ.")
    return {"comparable": not reasons, "reasons": reasons, "metric_ids": [m.get("metric_id") for m in metrics],
            "interpretation": "Comparable metadata does not establish a universal quality ranking or a measured catalytic/synthesis outcome."}


def check_requirements(plan: dict, record: dict, metrics: list[dict]) -> list[dict]:
    from pymatgen.core import Composition, Structure
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    results = []
    for r in plan.get("requirements", []):
        kind, prop, expected = r["kind"], r["property"], r["normalized_value"]
        if kind in {"sample_budget", "execution_parameter", "template"}:
            continue
        outcome = {"requirement_id": r["requirement_id"], "candidate_id": record["candidate_id"],
                   "structure_version_id": record["structure_version_id"], "status": "unknown", "strength": r["strength"],
                   "property": prop, "expected": expected, "observed": None, "method": None, "reason": "Property has not been evaluated."}
        try:
            if kind == "element_set":
                actual = sorted(record["elements"])
                outcome.update(observed=actual, status="pass" if set(actual) == set(expected.split("-")) else "fail", method="pymatgen element equality", reason=None)
            elif kind == "stoichiometry":
                actual = Composition(record["formula"]).reduced_composition
                matches = actual.almost_equals(Composition(expected).reduced_composition, rtol=1e-6, atol=1e-8)
                outcome.update(observed=actual.reduced_formula, status="pass" if matches else "fail", method="pymatgen reduced composition; rtol=1e-6, atol=1e-8", reason=None)
            elif kind == "symmetry":
                crystal = Structure.from_file(record["path"])
                actual = SpacegroupAnalyzer(crystal, symprec=0.01, angle_tolerance=5).get_space_group_number()
                outcome.update(observed=int(actual), status="pass" if actual == expected else "fail", method="pymatgen SpacegroupAnalyzer; symprec=0.01 angstrom, angle_tolerance=5 degrees", reason=None)
            elif kind in {"numeric_target", "numeric_bound"}:
                available = [m for m in metrics if m.get("candidate_id") == record["candidate_id"] and m.get("structure_version_id") == record["structure_version_id"] and m["property"] == prop and m["unit"] == r["normalized_unit"] and metric_usable(m)]
                if len(available) == 1:
                    metric = available[0]
                    relation, actual = r["relation"], metric["value"]
                    passed = actual <= expected if relation == "at_most" else actual >= expected if relation == "at_least" else expected[0] <= actual <= expected[1] if relation == "range" else math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-8)
                    outcome.update(observed=actual, status="pass" if passed else "fail", method=metric["method"], metric_id=metric["metric_id"], reason=None)
                elif len(available) > 1:
                    outcome["reason"] = "Multiple eligible measurements exist; choose an explicit method/metric rather than selecting a favorable value."
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            outcome["reason"] = f"Check unavailable: {exc}"
        results.append(outcome)
    return results


def screen_records(plan: dict, records: list[dict], metrics_by_version: dict) -> dict:
    from pymatgen.core import Structure
    from pymatgen.analysis.structure_matcher import StructureMatcher
    matcher = StructureMatcher(ltol=0.2, stol=0.3, angle_tol=5, primitive_cell=True, scale=True, attempt_supercell=False)
    seen_bytes, groups, screened = {}, [], []
    for record in records:
        checks = check_requirements(plan, record, metrics_by_version.get(record["structure_version_id"], []))
        hard_checks = [c for c in checks if c["strength"] == "hard"]
        basic = bool(record["parse_valid"] and record["basic_geometry_pass"] and record.get("composition_match") is not False)
        eligible = basic and "requirements" in plan and all(c["status"] == "pass" for c in hard_checks)
        duplicate = seen_bytes.get(record["sha256"])
        seen_bytes.setdefault(record["sha256"], record["candidate_id"])
        equivalent, equivalence_status, equivalence_error = None, "distinct_within_checked_set", None
        try:
            crystal = Structure.from_file(record["path"])
            for prior_id, prior_crystal in groups:
                if matcher.fit(crystal, prior_crystal):
                    equivalent, equivalence_status = prior_id, "equivalent"
                    break
            if equivalent is None:
                groups.append((record["candidate_id"], crystal))
        except Exception as exc:
            equivalence_status, equivalence_error = "unknown", str(exc)
        screened.append({**record, "requirement_checks": checks, "exact_file_duplicate": duplicate is not None,
                         "exact_duplicate_of": duplicate, "equivalent_to_candidate_id": equivalent,
                         "structural_equivalence_status": equivalence_status, "equivalence_error": equivalence_error,
                         "basic_checks_pass": basic, "all_hard_requirements_pass": eligible,
                         "research_goals_verified": all(c["status"] == "pass" for c in checks if c["strength"] == "goal") if any(c["strength"] == "goal" for c in checks) else None})
    valid_groups = {r["equivalent_to_candidate_id"] or r["candidate_id"] for r in screened if r["all_hard_requirements_pass"] and r["structural_equivalence_status"] != "unknown"}
    return {"records": screened, "requirements_audited": "requirements" in plan,
            "valid_candidate_count": sum(r["all_hard_requirements_pass"] for r in screened),
            "unique_valid_candidate_count": len(valid_groups),
            "equivalence_unknown_count": sum(r["structural_equivalence_status"] == "unknown" for r in screened),
            "methods": {"pymatgen_version": importlib.metadata.version("pymatgen"), "byte_identity": "SHA256",
                        "structure_equivalence": {"algorithm": "pymatgen.StructureMatcher", "ltol": 0.2, "stol": 0.3, "angle_tol": 5, "primitive_cell": True, "scale": True, "attempt_supercell": False}},
            "limitations": "Structural equivalence depends on stated tolerances. No database novelty, energy, catalytic, corrosion or synthesis conclusion follows from these checks."}
