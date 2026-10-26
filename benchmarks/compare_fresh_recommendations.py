"""Read-only CPU comparison of fixed shortlist rules on new generated outputs.

The input manifest supplies candidate_id, path, sha256, source_group and expected
elements. Source groups are reported but never supplied to the ranking function.
No historical priority labels, LLM, generation, relaxation or DFT are used.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import random
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from matpilot.recommendations import recommend_candidates
from compare_candidate_recommendations import by_id, metrics, quantile, sha, write_csv, METRICS

# Pinned before the 12-candidate fresh pool was available. Changes to this
# implementation require a new declared protocol, not an unnoticed rerun.
FROZEN_RECOMMENDATION_SHA256 = "92c248586ef91caffa62bcf5555168d1005fd425e422bb61bdd925be4d20fb86"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--random-repeats", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261026)
    args = parser.parse_args()
    if not 1 <= args.limit <= 256 or args.random_repeats < 1:
        raise ValueError("Positive limit <=256 and random-repeats required")
    manifest_path, output = args.manifest.resolve(), args.output.resolve()
    names = {key: output.with_name(output.stem + suffix) for key, suffix in {
        "candidates": ".candidates.csv", "selections": ".selections.csv", "random": ".random.csv",
        "recommendation": ".recommendation.json", "summary": ".summary.md"}.items()}
    if any(p.exists() for p in [output, *names.values()]):
        raise FileExistsError("Use a new output stem; comparison evidence cannot be overwritten")
    manifest_hash = sha(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("provenance"), dict):
        raise ValueError("Expected schema_version=1 and explicit provenance object")
    source_records = manifest["records"]
    identities = by_id(source_records, "fresh manifest")
    if any(r.get("source_group") not in {"direct", "plugin"} for r in source_records):
        raise ValueError("Each fresh candidate needs source_group direct or plugin")
    module = ROOT / "src/matpilot/recommendations.py"
    if sha(module) != FROZEN_RECOMMENDATION_SHA256:
        raise ValueError("Recommendation implementation changed since protocol was fixed; do not tune to fresh outcomes")
    records, source_hashes = [], {}
    for r in source_records:
        file = Path(r["path"])
        file = (manifest_path.parent / file).resolve() if not file.is_absolute() else file.resolve()
        actual = sha(file)
        if actual != r["sha256"]:
            raise ValueError("Fresh candidate SHA mismatch: " + r["candidate_id"])
        source_hashes[str(file)] = actual
        records.append({"candidate_id": r["candidate_id"], "path": str(file), "sha256": actual,
                        "structure_version_id": r.get("structure_version_id", "fresh-original-" + actual[:16])})
    if str(output) in source_hashes or output == manifest_path:
        raise ValueError("Output must not overwrite a source artifact")
    started = time.perf_counter()
    recommendation = recommend_candidates(records, manifest["expected_elements"], args.limit,
        symprec=0.1, angle_tolerance=5.0)
    elapsed = time.perf_counter() - started
    rows = by_id(recommendation["records"], "recommendation")
    pool = sorted(r["candidate_id"] for r in rows.values() if r["qualified"])
    actual_limit = min(args.limit, len(pool))
    strategies = {
        "qualified_first_k": pool[:actual_limit],
        "qualified_smallest_k": sorted(pool, key=lambda cid: (rows[cid]["site_count"], cid))[:actual_limit],
        "matpilot_diversity_k": recommendation["selected_candidate_ids"],
    }
    measured = {name: {"candidate_ids": ids, "metrics": metrics(ids, rows),
        "source_group_counts": dict(Counter(identities[cid]["source_group"] for cid in ids)),
        "same_qualified_unique_pool": True} for name, ids in strategies.items()}
    if any(len(ids) != actual_limit for ids in strategies.values()):
        raise ValueError("Matched-size baseline mismatch")
    rng = random.Random(args.seed)
    random_rows = []
    for repetition in range(1, args.random_repeats + 1):
        selected = rng.sample(pool, actual_limit)
        values = metrics(selected, rows)
        group_counts = Counter(identities[cid]["source_group"] for cid in selected)
        random_rows.append({"repeat": repetition, "candidate_ids": ";".join(selected),
            "direct_count": group_counts["direct"], "plugin_count": group_counts["plugin"],
            **{key: values[key] for key in METRICS}})
    random_summary = {}
    for key in METRICS:
        values = [r[key] for r in random_rows]
        observed = measured["matpilot_diversity_k"]["metrics"][key]
        random_summary[key] = {"mean": statistics.mean(values), "median": statistics.median(values),
            "p05": quantile(values, .05), "p95": quantile(values, .95), "min": min(values), "max": max(values),
            "fraction_random_less_than_matpilot": sum(v < observed for v in values) / len(values),
            "fraction_random_equal_to_matpilot": sum(v == observed for v in values) / len(values),
            "fraction_random_greater_than_matpilot": sum(v > observed for v in values) / len(values)}
    if sha(manifest_path) != manifest_hash or sha(module) != FROZEN_RECOMMENDATION_SHA256:
        raise ValueError("Manifest or implementation changed during this comparison")
    if any(sha(file) != digest for file, digest in source_hashes.items()):
        raise ValueError("Fresh candidate bytes changed during comparison")
    source_group_observations = {}
    for group in ("direct", "plugin"):
        group_rows = [rows[r["candidate_id"]] for r in source_records if r["source_group"] == group]
        source_group_observations[group] = {"raw_count": len(group_rows),
            "precheck_pass_before_dedup": sum(not (set(r["exclusion_reasons"]) - {"equivalent_to_qualified_representative"}) for r in group_rows),
            "common_pool_representatives": sum(r["qualified"] for r in group_rows),
            "exclusion_reason_counts": dict(Counter(reason for r in group_rows for reason in r["exclusion_reasons"]))}
    report = {"format": "matpilot-fresh-recommendation-comparison/v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "current_session_generated_candidates_cpu_postprocessing_fixed_pool_comparison",
        "source": {"manifest_sha256": manifest_hash,
            "provenance_sha256": hashlib.sha256(json.dumps(manifest["provenance"], ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "input_records": [{"candidate_id": r["candidate_id"], "sha256": r["sha256"], "source_group": r["source_group"]} for r in source_records],
            "provenance_limit": "Candidate identities and bytes verified against caller manifest; generation and environment verification are separate records"},
        "implementation": {"script_sha256": sha(__file__), "shared_metric_code_sha256": sha(Path(__file__).with_name("compare_candidate_recommendations.py")),
            "recommendation_module_sha256": FROZEN_RECOMMENDATION_SHA256,
            "python": sys.version.split()[0], "pymatgen": importlib.metadata.version("pymatgen"), "spglib": importlib.metadata.version("spglib")},
        "protocol": {"requested_k": args.limit, "actual_matched_k": actual_limit,
            "target_met": len(pool) >= args.limit, "no_auto_resampling": True,
            "common_pool": "Same exact element set, explicit full occupancy, basic geometry/symmetry checks and structural deduplication for every strategy",
            "source_groups_hidden_from_ranking_function": True, "historical_priority_labels_used": False,
            "priority_interpretation": "Passing representatives may be considered for later evaluation, but are not scientifically certified as suitable for relaxation or stable",
            "policy": recommendation["policy"], "random_repeats": args.random_repeats, "random_seed": args.seed,
            "random_sampling": "Fixed-seed independent samples from the same pool; without replacement inside each sample",
            "metric_definitions": {"formula_count": "Distinct reduced formulae", "space_group_count": "Distinct symmetry-analyzed SG numbers",
                "size_bin_count": "Distinct fixed input-cell site bins 1-10, 11-20, 21+",
                "category_count_sum": "Sum of the above three category counts, the designed objective rather than independent scientific quality",
                "site_count_sum": "Input sites sum: scale proxy, not measured computing time/cost",
                "ineligible_count": "Structural qualification failures among selected items, excluding duplicate aliases",
                "within_shortlist_duplicate_count": "Repeated structural representatives in the selection"}},
        "counts": {"raw_candidates": len(records), "qualified_unique_pool": len(pool),
            "exclusion_reason_counts": dict(Counter(reason for r in rows.values() for reason in r["exclusion_reasons"]))},
        "source_group_observations": source_group_observations,
        "source_group_observation_caveat": "Small independent generated samples and canonical representative assignment cannot establish generator quality differences or plugin causality",
        "common_pool_candidate_ids": pool, "common_pool_metrics": metrics(pool, rows), "strategies": measured,
        "random_baseline": {"same_qualified_unique_pool": True, "repeats": args.random_repeats, "metrics": random_summary,
            "interpretation": "Fixed-pool empirical distribution; fractions are not inferential p-values or material performance estimates"},
        "execution": {"cpu_recommendation_wall_seconds": elapsed, "api_requests": 0, "api_cny": 0,
            "new_material_generation_runs_in_this_script": 0, "relaxation_runs": 0, "property_calculations": 0,
            "source_inputs_unchanged": True, "human_active_seconds": None},
        "limitations": recommendation["limitations"] + [
            "Fresh generation is documented separately; this comparison only performs CPU postprocessing.",
            "No claim that the diversity strategy must beat first, smallest or random selections; every outcome and tie is retained.",
            "Common preprocessing benefits apply equally to every baseline and are not diversity-ranking gains.",
            "Small-sample source-group counts do not demonstrate model improvements, materials quality, speedup or LLM reasoning benefits."]}
    candidate_rows = [{"candidate_id": cid, "source_group": identities[cid]["source_group"], "sha256": r["sha256"],
        "qualified": r["qualified"], "representative_id": r.get("representative_id"), "reduced_formula": r.get("reduced_formula"),
        "space_group_number": r.get("space_group_number"), "site_count": r.get("site_count"), "size_bin": r.get("size_bin"),
        "selected": r["selected"], "exclusion_reasons": ";".join(r["exclusion_reasons"])} for cid, r in sorted(rows.items())]
    selection_rows = [{"strategy": name, "rank": rank, "candidate_id": cid,
        "source_group": identities[cid]["source_group"], "sha256": rows[cid]["sha256"],
        "reduced_formula": rows[cid]["reduced_formula"], "space_group_number": rows[cid]["space_group_number"],
        "site_count": rows[cid]["site_count"], "size_bin": rows[cid]["size_bin"]}
        for name, ids in strategies.items() for rank, cid in enumerate(ids, 1)]
    output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(names["candidates"], candidate_rows, list(candidate_rows[0]))
    write_csv(names["selections"], selection_rows, ["strategy", "rank", "candidate_id", "source_group", "sha256", "reduced_formula", "space_group_number", "site_count", "size_bin"])
    write_csv(names["random"], random_rows, list(random_rows[0]))
    text = json.dumps(recommendation, ensure_ascii=False, indent=2)
    for artifact in source_hashes:
        text = text.replace(artifact, "<verified-candidate>").replace(artifact.replace("\\", "/"), "<verified-candidate>")
    names["recommendation"].write_text(text + "\n", encoding="utf-8")
    lines = ["# 本轮新生成候选的同池推荐对比", "", f"原始候选 {len(records)} 个，核验并去重后代表 {len(pool)} 个；请求 {args.limit} 个，实际同池比较 {actual_limit} 个。",
        "不足数量时如实保留，没有自动补算；不读取任何历史优先标签。", "",
        "| 策略 | 组成数 | 空间群数 | 规模档数 | 类别总覆盖 | 输入sites合计 | 不合格 | 重复 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, data in measured.items():
        lines.append("| " + name + " | " + " | ".join(str(data["metrics"][key]) for key in METRICS) + " |")
    lines.append("| random均值 | " + " | ".join(f"{random_summary[key]['mean']:.3f}" for key in METRICS) + " |")
    lines += ["", "所有策略使用同一个资格过滤且去重后的池；来源direct/plugin不输入推荐函数，仅在产物中保留以便核查。", "",
        "多样性是预先固定的覆盖目标，不等于稳定性、可合成性或催化性能；输入sites不是实测费用或运行时间。", "",
        "本脚本不执行模型生成、弛豫、DFT或API。生成来源证据另列。该小样本不能证明生成质量提升或人工提速。", ""]
    names["summary"].write_text("\n".join(lines), encoding="utf-8")
    report["artifacts"] = {key: {"filename": file.name, "sha256": sha(file)} for key, file in names.items()}
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2); handle.write("\n")
    print(json.dumps({"output": str(output), "sha256": sha(output), "counts": report["counts"],
        "target_met": report["protocol"]["target_met"], "strategies": measured, "random": random_summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
