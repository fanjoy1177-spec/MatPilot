"""Re-evaluate disclosed fixed rankings, offline, using only Python's standard library.

Usage: python benchmarks/ranking100/reproduce_metrics.py FRESH_RECEIPT_JSON
This is metric arithmetic reproduction, not another production or blind run.
"""
from pathlib import Path
import hashlib
import json
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
if len(sys.argv) != 2:
    raise SystemExit(__doc__)
output = Path(sys.argv[1])
if output.exists():
    raise SystemExit('Output must not exist')
def guard(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system'):
        raise PermissionError('Offline metric replay forbids network and subprocesses')
sys.addaudithook(guard)
read = lambda name: json.loads((ROOT / name).read_text(encoding='utf-8'))
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
for line in (ROOT / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
    digest, name = line.split('  ', 1)
    assert sha(ROOT / name) == digest, name
from evaluate100 import evaluate100
rows = read('candidates.json')['records']
ranking = read('rankings.json')
primary = ranking['primary_method']
prediction_values = {}
for method, model in [(primary, 'wrenformer'), ('wrenformer_direct_scalar', 'wrenformer'),
                      ('mace_archived_scalar_convergence_unknown', 'mace-mp-0')]:
    prediction_values[method] = {r['candidate_id']: r['predicted_energy_above_reference_hull_eV_per_atom'][model]
                                for r in rows if r['predicted_energy_above_reference_hull_eV_per_atom'][model] is not None}
result = evaluate100({r['candidate_id']: r['disclosed_reference_dft_target_eV_per_atom'] for r in rows},
    ranking['rankings'], primary, truth_tie_keys={r['candidate_id']: r['material_id'] for r in rows},
    eligible_ids={primary: ranking['service_eligible_ids']}, prediction_values=prediction_values,
    truth_tie_seed=read('protocol_excerpt.json')['metrics']['truth_tie_seed'])
checks = {key: result[key] == value for key, value in read('expected_evaluation.json').items()}
receipt = {'status': 'passed' if all(checks.values()) else 'failed', 'checks': checks,
    'scope': 'fixed_ranking_metrics_only_not_production_replay', 'primary_hits': result['primary']['hit_count'],
    'secondary_MACE_hits': result['methods']['mace_archived_scalar_convergence_unknown']['hit_count'],
    'candidate_count': len(rows), 'ranking_count': len(ranking['rankings']), 'network_denied': True}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
print(json.dumps(receipt))
if not all(checks.values()):
    raise SystemExit(1)
