"""Compare shortlist policies on one immutable historical MatterGen candidate pool.

CPU parsing, symmetry analysis and structure matching only. No model, LLM,
relaxation or property calculator is invoked. All non-naive selection baselines
share the same verified, explicitly priority-selected, deduplicated pool.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import random
import statistics
import sys
import time
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from matpilot.recommendations import recommend_candidates

METRICS = ("formula_count", "space_group_count", "size_bin_count", "category_count_sum",
           "site_count_sum", "ineligible_count", "within_shortlist_duplicate_count")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def by_id(rows, label):
    result = {row["candidate_id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"Duplicate candidate IDs in {label}")
    return result


def quantile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (position - low) * (values[high] - values[low])


def metrics(ids, rows):
    selected = [rows[value] for value in ids]
    representatives = [r.get("representative_id", r["candidate_id"]) for r in selected]
    formulae = sorted({r["reduced_formula"] for r in selected})
    groups = sorted({r["space_group_number"] for r in selected if r.get("space_group_number") is not None})
    bins = sorted({r["size_bin"] for r in selected})
    # An equivalent candidate is an alias of a qualifying structure, not invalid
    # chemistry. Duplicate loss is counted within the chosen shortlist only.
    invalid = sum(bool(set(r["exclusion_reasons"]) - {"equivalent_to_qualified_representative"}) for r in selected)
    duplicates = len(representatives) - len(set(representatives))
    return {"selected_count": len(ids), "formula_count": len(formulae),
            "space_group_count": len(groups), "size_bin_count": len(bins),
            "category_count_sum": len(formulae) + len(groups) + len(bins),
            "formula_values": formulae, "space_group_values": groups, "size_bin_values": bins,
            "site_count_sum": sum(r["site_count"] for r in selected),
            "site_count_mean": statistics.mean(r["site_count"] for r in selected) if selected else None,
            "ineligible_count": invalid, "ineligible_rate": invalid / len(ids) if ids else None,
            "within_shortlist_duplicate_count": duplicates,
            "within_shortlist_duplicate_rate": duplicates / len(ids) if ids else None,
            "missing_space_group_count": sum(r.get("space_group_number") is None for r in selected)}


def write_csv(path, rows, fields):
    with path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--random-repeats", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261026)
    parser.add_argument("--symprec", type=float, default=0.1)
    parser.add_argument("--angle-tolerance", type=float, default=5.0)
    args = parser.parse_args()
    source = args.source_run.resolve()
    output = args.output.resolve()
    if output.is_relative_to(source):
        raise ValueError("Comparison outputs must not be written inside the historical source run")
    if args.random_repeats < 1:
        raise ValueError("random-repeats must be positive")
    outputs = {key: output.with_name(output.stem + suffix) for key, suffix in {
        "candidates": ".candidates.csv", "selections": ".selections.csv", "random": ".random.csv",
        "summary": ".summary.md", "recommendation": ".recommendation.json"}.items()}
    if any(path.exists() for path in [output, *outputs.values()]):
        raise FileExistsError("Use new output names; existing evidence is never overwritten")
    relative_files = ["generation_config.json", "run_manifest.json", "combined_cifs.zip",
                      "experiment_handoff/candidate_index.json", "experiment_handoff/structure_screening.json",
                      "experiment_handoff/structure_screening.csv", "dft_preparation/candidate_catalog.csv",
                      "dft_preparation/shortlist.csv", "dft_preparation/preparation_summary.json"]
    source_hashes = {relative: sha(source / relative) for relative in relative_files}
    config = json.loads((source / "generation_config.json").read_text(encoding="utf-8"))
    manifest = json.loads((source / "run_manifest.json").read_text(encoding="utf-8"))
    index = by_id(json.loads((source / "experiment_handoff/candidate_index.json").read_text(encoding="utf-8")), "index")
    screening = by_id(json.loads((source / "experiment_handoff/structure_screening.json").read_text(encoding="utf-8")), "screening JSON")
    screening_csv = by_id(load_csv(source / "experiment_handoff/structure_screening.csv"), "screening CSV")
    catalog = by_id(load_csv(source / "dft_preparation/candidate_catalog.csv"), "catalog CSV")
    historical = load_csv(source / "dft_preparation/shortlist.csv")
    if not (set(index) == set(screening) == set(screening_csv) == set(catalog)):
        raise ValueError("Candidate source tables disagree on IDs")
    if config["properties_to_condition_on"] != {"chemical_system": "Ni-Fe-O"}:
        raise ValueError("This benchmark expects the explicitly documented Ni-Fe-O historical run")
    if manifest["status"] != "completed" or len(manifest["batches"]) != len(index):
        raise ValueError("Historical generation manifest is incomplete")
    if any(b["status"] != "completed" or b["return_code"] != 0 for b in manifest["batches"]):
        raise ValueError("Historical generation has unsuccessful batches; review provenance first")
    records = []
    priority_ids = []
    with zipfile.ZipFile(source / "combined_cifs.zip") as archive:
        for candidate_id in sorted(index):
            row = index[candidate_id]
            path = (source / "experiment_handoff" / row["cif_file"]).resolve()
            if not path.is_relative_to(source / "experiment_handoff/cifs"):
                raise ValueError("Candidate path leaves the declared source directory")
            if path.read_bytes() != archive.read(row["source_member"]):
                raise ValueError(f"CIF bytes differ from combined generation archive: {candidate_id}")
            digest = sha(path)
            source_hashes[path.relative_to(source).as_posix()] = digest
            records.append({"candidate_id": candidate_id, "path": str(path), "sha256": digest,
                            "structure_version_id": "historical-original-" + digest[:16]})
            labels = [screening[candidate_id]["priority"], screening_csv[candidate_id]["priority"], catalog[candidate_id]["priority"]]
            if len(set(labels)) != 1:
                raise ValueError(f"Conflicting historical priority labels: {candidate_id}")
            if screening[candidate_id]["full_occupancy"] != (screening_csv[candidate_id]["full_occupancy"] == "True"):
                raise ValueError("JSON and CSV occupancy declarations disagree")
            if labels[0] == "优先弛豫":
                priority_ids.append(candidate_id)
    historical.sort(key=lambda r: int(r["selection_order"]))
    historical_ids = [r["candidate_id"] for r in historical]
    if len(set(historical_ids)) != len(historical_ids) or len(historical_ids) != args.limit:
        raise ValueError("Historical shortlist must contain exactly limit distinct IDs")
    for item in historical:
        if item != catalog[item["candidate_id"]] or item["selected"] != "是":
            raise ValueError("Historical shortlist differs from source catalog selection")
    basis = ("Caller explicitly selects the historical label 优先弛豫; label agreement checked across "
             "experiment_handoff/structure_screening.json SHA256=" + source_hashes["experiment_handoff/structure_screening.json"]
             + ", experiment_handoff/structure_screening.csv SHA256=" + source_hashes["experiment_handoff/structure_screening.csv"]
             + ", dft_preparation/candidate_catalog.csv SHA256=" + source_hashes["dft_preparation/candidate_catalog.csv"]
             + ". This is a historical heuristic preference, not scientific validation or a measured property.")
    module_path = ROOT / "src/matpilot/recommendations.py"
    module_hash = sha(module_path)
    started = time.perf_counter()
    recommendation = recommend_candidates(records, ["Ni", "Fe", "O"], args.limit,
        priority_candidate_ids=priority_ids, priority_selection_basis=basis,
        symprec=args.symprec, angle_tolerance=args.angle_tolerance)
    analysis_seconds = time.perf_counter() - started
    rows = by_id(recommendation["records"], "recommendation")
    pool = sorted(r["candidate_id"] for r in recommendation["records"] if r["qualified"])
    if len(pool) < args.limit:
        raise ValueError("Too few eligible unique candidates for a matched-size comparison")
    # Recalculate SG for excluded raw-prefix candidates solely for descriptive
    # weak-baseline metrics. This never promotes them into the common pool.
    from pymatgen.core import Structure
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    paths = {r["candidate_id"]: r["path"] for r in records}
    raw_ids = sorted(rows)[:args.limit]
    for candidate_id in raw_ids:
        if rows[candidate_id].get("space_group_number") is None:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    structure = Structure.from_file(paths[candidate_id])
                    rows[candidate_id]["space_group_number"] = SpacegroupAnalyzer(structure,
                        symprec=args.symprec, angle_tolerance=args.angle_tolerance).get_space_group_number()
            except Exception:
                pass
    historical_mapping = []
    for candidate_id in historical_ids:
        row = rows[candidate_id]
        canonical = row.get("representative_id")
        if canonical not in pool or set(row["exclusion_reasons"]) - {"equivalent_to_qualified_representative"}:
            raise ValueError(f"Historical choice is not in an eligible structural equivalence class: {candidate_id}")
        historical_mapping.append({"historical_candidate_id": candidate_id, "canonical_candidate_id": canonical})
    canonical_historical = [r["canonical_candidate_id"] for r in historical_mapping]
    if len(set(canonical_historical)) != args.limit:
        raise ValueError("Historical shortlist contains equivalent candidates under current tolerance; do not silently replace them")
    selected = {
        "raw_first_k_weak_baseline": (raw_ids, False),
        "qualified_first_k": (pool[:args.limit], True),
        "qualified_smallest_k": (sorted(pool, key=lambda value: (rows[value]["site_count"], value))[:args.limit], True),
        "matpilot_diversity_k": (recommendation["selected_candidate_ids"], True),
        "historical_mgflow_k_canonicalized": (canonical_historical, True),
        "historical_mgflow_original_k_descriptive": (historical_ids, False),
    }
    strategies = {name: {"same_qualified_unique_pool": same, "candidate_ids": ids,
                         "metrics": metrics(ids, rows)} for name, (ids, same) in selected.items()}
    rng = random.Random(args.seed)
    random_rows = []
    for repeat in range(args.random_repeats):
        ids = rng.sample(pool, args.limit)
        values = metrics(ids, rows)
        random_rows.append({"repeat": repeat + 1, "candidate_ids": ";".join(ids),
                            **{key: values[key] for key in METRICS}})
    random_summary = {}
    for key in METRICS:
        values = [r[key] for r in random_rows]
        observed = strategies["matpilot_diversity_k"]["metrics"][key]
        random_summary[key] = {"mean": statistics.mean(values), "median": statistics.median(values),
            "p05": quantile(values, .05), "p95": quantile(values, .95), "min": min(values), "max": max(values),
            "fraction_random_less_than_matpilot": sum(x < observed for x in values) / len(values),
            "fraction_random_equal_to_matpilot": sum(x == observed for x in values) / len(values),
            "fraction_random_greater_than_matpilot": sum(x > observed for x in values) / len(values)}
    # Hash all raw inputs again: no historical file is changed by analysis.
    if any(sha(source / path) != digest for path, digest in source_hashes.items()) or sha(module_path) != module_hash:
        raise ValueError("An input or recommendation implementation changed during analysis")
    report = {"format": "matpilot-candidate-recommendation-comparison/v1",
        "created_at": datetime.now(timezone.utc).isoformat(), "evidence_scope": "historical_generated_structures_cpu_selection_comparison",
        "source": {"run_label": source.name, "source_file_sha256": source_hashes,
            "archive_to_cif_bytes_verified": len(records), "historical_generation_status": manifest["status"],
            "historical_model": config["model_name"], "historical_condition": config["properties_to_condition_on"],
            "provenance_limit": "Local archived manifest and CIF identity verified; no new validation of historical GPU logs or scientific properties."},
        "implementation": {"script_sha256": sha(__file__), "recommendation_module_sha256": module_hash,
            "python": sys.version.split()[0], "pymatgen": importlib.metadata.version("pymatgen"),
            "spglib": importlib.metadata.version("spglib")},
        "protocol": {"limit": args.limit, "common_pool": "Exact expected elements, explicit full occupancy, basic geometry and symmetry, caller-selected historical priority label, then common structural deduplication",
            "priority_basis": basis, "random_repeats": args.random_repeats, "random_seed": args.seed,
            "random_sampling": "Python random.Random(seed).sample(sorted common pool, K), independently repeated without replacement within each sample",
            "policy": recommendation["policy"],
            "metric_definitions": {"formula_count": "Distinct pymatgen reduced_formula values in selected input CIFs",
                "space_group_count": "Distinct symmetry-analyzed SG numbers, using common stated tolerances",
                "size_bin_count": "Distinct fixed input-cell site bins: 1-10, 11-20, 21+",
                "category_count_sum": "Unweighted sum of three category counts; the algorithm's designed coverage objective, not independent material quality",
                "site_count_sum": "Sum of input-CIF site counts, a scale proxy; not DFT timing, cost or validated runtime estimate",
                "ineligible_count": "Fails explicit priority selector or structural checks; equivalent-to-representative alone is not chemical ineligibility",
                "within_shortlist_duplicate_count": "Selected count minus distinct qualifying representative IDs; duplicates outside qualification pool are not fully analyzed"},
            "historical_mapping_policy": "Map each historical chosen structure to the common current representative before matched-pool comparison; preserve original IDs and original metrics separately",
            "objective_independence": "Development comparison on a known historical dataset; category objective chosen for requested coverage, not independent physical outcomes"},
        "counts": {"raw_candidates": len(records), "historical_full_occupancy": sum(r["full_occupancy"] for r in screening.values()),
            "historical_complete_elements": sum(not r["missing_elements"] for r in screening.values()),
            "explicit_priority_candidates": len(priority_ids), "qualified_unique_pool": len(pool),
            "exclusion_reason_counts": dict(Counter(reason for r in recommendation["records"] for reason in r["exclusion_reasons"]))},
        "common_pool_candidate_ids": pool, "common_pool_metrics": metrics(pool, rows),
        "strategies": strategies, "historical_to_common_mapping": historical_mapping,
        "random_baseline": {"same_qualified_unique_pool": True, "repeats": args.random_repeats, "metrics": random_summary,
            "interpretation": "Empirical distribution over this fixed finite pool; fractions are not p-values or generalization claims"},
        "execution": {"cpu_recommendation_wall_seconds": analysis_seconds, "api_requests": 0, "api_cny": 0,
            "new_material_generation_runs": 0, "relaxation_runs": 0, "property_calculations": 0,
            "historical_inputs_unchanged": True, "human_active_seconds": None},
        "limitations": recommendation["limitations"] + [
            "The raw-prefix strategy is deliberately labeled weak and is not the only baseline.",
            "All qualified baselines receive identical preprocessing; its benefits cannot be credited only to diversity selection.",
            "Historical MG-Flow already implemented diverse selection; new MatPilot packages and automates it, rather than claiming the idea was absent.",
            "No workflow speedup, AI reasoning gain, user-time reduction or physical performance gain is estimated by this CPU comparison."]}
    candidate_rows = [{"candidate_id": value, "sha256": rows[value]["sha256"],
        "source_member": index[value]["source_member"], "historical_priority": screening[value]["priority"],
        "reduced_formula": rows[value].get("reduced_formula"), "space_group_number": rows[value].get("space_group_number"),
        "site_count": rows[value].get("site_count"), "size_bin": rows[value].get("size_bin"),
        "qualified": rows[value]["qualified"], "representative_id": rows[value].get("representative_id"),
        "selected": rows[value]["selected"], "exclusion_reasons": ";".join(rows[value]["exclusion_reasons"])} for value in sorted(rows)]
    selection_rows = [{"strategy": name, "same_qualified_unique_pool": same, "rank": rank,
        "candidate_id": value, "reduced_formula": rows[value]["reduced_formula"],
        "space_group_number": rows[value].get("space_group_number"), "site_count": rows[value]["site_count"],
        "size_bin": rows[value]["size_bin"], "sha256": rows[value]["sha256"]}
        for name, (ids, same) in selected.items() for rank, value in enumerate(ids, 1)]
    output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(outputs["candidates"], candidate_rows, list(candidate_rows[0]))
    write_csv(outputs["selections"], selection_rows, list(selection_rows[0]))
    write_csv(outputs["random"], random_rows, list(random_rows[0]))
    # The module returns no artifact paths. Do not publish raw source table rows,
    # manifests or their embedded personal filesystem paths.
    recommendation_text = json.dumps(recommendation, ensure_ascii=False, indent=2)
    for spelling in (str(source), source.as_posix(), str(ROOT), ROOT.as_posix()):
        recommendation_text = recommendation_text.replace(spelling, "<local-source>")
    outputs["recommendation"].write_text(recommendation_text + "\n", encoding="utf-8")
    lines = ["# 同一历史候选池的筛选推荐比较", "", "范围：128 个历史 MatterGen 候选的 CPU 重分析；未新增生成、弛豫、物性计算或 API 调用。", "",
        f"共同池：{len(records)} 个原始候选 → {len(priority_ids)} 个历史“优先弛豫”显式选择 → {len(pool)} 个重新核验并去重的代表结构。该标签只是调用方采用的历史启发式偏好。", "",
        "| 策略 | 同池 | 组成数 | 空间群数 | 规模档数 | 类别总覆盖 | 输入原子数合计 | 不合格 | 重复 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, data in strategies.items():
        m = data["metrics"]
        lines.append(f"| {name} | {data['same_qualified_unique_pool']} | " + " | ".join(str(m[k]) for k in METRICS) + " |")
    lines.append("| qualified_random_k 均值 | True | " + " | ".join(f"{random_summary[k]['mean']:.3f}" for k in METRICS) + " |")
    lines += ["", "随机基线固定 seed=" + str(args.seed) + f"，{args.random_repeats} 次，完整抽样记录见 CSV。", "",
        "历史 MG-Flow 原本已经采用多样性选择；这里把其历史选中项映射到同一代表池，原始选择也完整保留。", "",
        "类别覆盖是算法主动优化的目标，不能循环论证为物理质量。输入原子数只是规模代理，不能称计算费用或提速。", "",
        "本次不测人类操作时间、不证明 LLM 推理增益，也不评价稳定性、可合成性或催化活性。所有合理基线和不利指标均保留。", ""]
    outputs["summary"].write_text("\n".join(lines), encoding="utf-8")
    report["artifacts"] = {key: {"filename": path.name, "sha256": sha(path)} for key, path in outputs.items()}
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"output": str(output), "sha256": sha(output), "counts": report["counts"],
        "strategies": {k: v["metrics"] for k, v in strategies.items()}, "random": random_summary,
        "api_requests": 0, "new_material_generation_runs": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
