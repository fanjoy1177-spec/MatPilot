"""Supplemental product-pipeline and same-priority-pool comparison; CPU only."""
from pathlib import Path
from collections import Counter
from datetime import datetime,timezone
import argparse,hashlib,json,random,statistics,sys,time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from matpilot.relaxation_triage import recommend_for_relaxation
from compare_candidate_recommendations import metrics,sha
FROZEN_TRIAGE_SHA='1a4b19bb00400cf8581f79dc64b1eb5ab8c259068070e7401ed6d761e8797727'
FROZEN_RANKING_SHA='92c248586ef91caffa62bcf5555168d1005fd425e422bb61bdd925be4d20fb86'
def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 a=p.parse_args();m=a.manifest.resolve();out=a.output.resolve()
 if out.exists():raise FileExistsError('Use a new output filename; evidence is not overwritten')
 for filename,wanted in [('relaxation_triage.py',FROZEN_TRIAGE_SHA),('recommendations.py',FROZEN_RANKING_SHA)]:
  assert sha(ROOT/'src/matpilot'/filename)==wanted,'Frozen implementation changed'
 manifest=json.loads(m.read_text(encoding='utf-8-sig'));records=[]
 for row in manifest['records']:
  f=Path(row['path']);f=f if f.is_absolute() else m.parent/f
  assert sha(f)==row['sha256']
  records.append({'candidate_id':row['candidate_id'],'path':str(f.resolve()),'sha256':row['sha256'],'structure_version_id':row.get('structure_version_id')})
 started=time.perf_counter();result=recommend_for_relaxation(records,manifest['expected_elements'],4);elapsed=time.perf_counter()-started
 rows={r['candidate_id']:r for r in result['records']};pool=sorted(cid for cid,r in rows.items() if r['qualified']);k=min(4,len(pool))
 chosen={'same_priority_pool_first_k':pool[:k],'same_priority_pool_smallest_k':sorted(pool,key=lambda cid:(rows[cid]['site_count'],cid))[:k],'matpilot_full_pipeline':result['selected_candidate_ids']}
 comparisons={key:{'candidate_ids':ids,'metrics':metrics(ids,rows)} for key,ids in chosen.items()}
 rng=random.Random(20261026);samples=[metrics(rng.sample(pool,k),rows) for _ in range(1000)]
 keys=['formula_count','space_group_count','size_bin_count','category_count_sum','site_count_sum']
 rand={key:{'mean':statistics.mean(r[key] for r in samples),'min':min(r[key] for r in samples),'max':max(r[key] for r in samples),'fraction_equal_to_plugin':sum(r[key]==comparisons['matpilot_full_pipeline']['metrics'][key] for r in samples)/1000} for key in keys}
 def raw_description(ids):
  rr=[rows[cid] for cid in ids]
  return {'candidate_ids':ids,'count':len(ids),'structural_precheck_representatives':sum(r['structural_representative_qualified'] for r in rr),'priority_relaxation_count':sum(r['relaxation_priority']['tier']=='priority_relaxation' for r in rr),'manual_review_count':sum(r['relaxation_priority']['tier']=='manual_review' for r in rr),'not_eligible_count':sum(r['relaxation_priority']['tier']=='not_eligible' for r in rr),'input_sites_known_sum':sum(r.get('site_count',0) for r in rr),'unknown_site_count':sum('site_count' not in r for r in rr)}
 ids=[r['candidate_id'] for r in records]
 public_records=[{k:v for k,v in r.items() if k!='path'} for r in result['records']]
 report={'schema_version':'matpilot-relaxation-triage-comparison/v1','date':'2026-10-26','created_at':datetime.now(timezone.utc).isoformat(),'scope':'Supplemental product verification after user feedback; reuse the same 12 newly generated CIFs, not independent new evidence or a preregistered experiment','input':{'manifest_sha256':sha(m),'candidate_count':len(records),'historical128_included':False,'input_hashes':[{k:r[k] for k in ['candidate_id','sha256']} for r in records]},'implementation':{'triage_sha256':FROZEN_TRIAGE_SHA,'ranking_sha256':FROZEN_RANKING_SHA,'benchmark_sha256':sha(__file__)},'rule':result['relaxation_triage_policy'],'stage_counts':result['stage_counts'],'records':public_records,'selected_candidate_ids':result['selected_candidate_ids'],'product_pipeline_comparison':{'raw_all':raw_description(ids),'raw_first_four':raw_description(ids[:4]),'plugin_selected':raw_description(result['selected_candidate_ids']),'interpretation':'Raw-first-four was added after user feedback and is an exploratory use scenario. It does not represent expert use or all MatterGen users. Qualification ratios refer only to explicit workflow criteria, not scientific quality.'},'same_priority_pool':{'candidate_ids':pool,'requested_k':4,'actual_k':k,'strategies':comparisons,'random_1000':rand,'random_seed':20261026,'interpretation':'All strategies share the newly completed priority triage. This isolates selection strategy within the same pool; retain ties, gains and costs.'},'execution':{'cpu_wall_seconds':elapsed,'api_calls':0,'new_generation_runs':0,'relaxation_runs':0,'dft_runs':0,'human_active_seconds':None},'limitations':result['limitations']+['Reusing this same small pool is an exploratory correction, not independent confirmation of a general advantage.','Old dev9 real-LLM output did not use this priority policy; new protocol testing is separate.','Counts and input-site sums are workflow outcomes, not measured time, expense, energy or material properties.']}
 for r in records:assert sha(r['path'])==r['sha256']
 out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
 print(json.dumps({'stage_counts':report['stage_counts'],'selected':report['selected_candidate_ids'],'comparisons':comparisons,'random':rand},ensure_ascii=False))
if __name__=='__main__':main()
