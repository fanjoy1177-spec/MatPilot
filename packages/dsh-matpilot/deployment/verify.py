"""Fixed deployment checks. Import/connect checks never invoke material inference."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path


def atomic(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    temp.replace(path)


def config(root, mode, enabled=False):
    recipe = json.loads((root / 'input/recipe.json').read_text())
    data = {'backend': 'native' if mode == 'mattergen' else 'demo',
            'real_compute_enabled': enabled if mode == 'mattergen' else False,
            'data_dir': str(root / 'jobs'), 'max_samples': 4,
            'batch_timeout_seconds': 1800, 'seed_supported': True,
            'command': [str(root / 'mattergen/bin/mattergen-generate')],
            'checkpoint_paths': {'chemical_system': str(root / 'weights/chemical_system')},
            'model_identities': {'mattergen': {'code': {'version': recipe['mattergen_version'], 'lock_sha256': recipe['mattergen_lock_sha256']},
                                             'weights': {'revision': recipe['weights_revision'], 'files': recipe['weights']}}}}
    atomic(root / 'config.json', data)
    return data


async def mcp_check(root):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = {**os.environ, 'MATPILOT_CONFIG': str(root / 'config.json')}
    params = StdioServerParameters(command=sys.executable, args=['-m', 'matpilot', 'serve'], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool('get_capabilities', {})
            if result.isError:
                raise RuntimeError('MCP capability query failed')
            caps = json.loads(next(c.text for c in result.content if c.type == 'text'))
            assert len(tools.tools) == 9
            assert caps['version'] == '0.2.0.dev8'
            return {'mcp': True, 'tool_count': len(tools.tools), 'capabilities_version': caps['version'], 'backend': caps['backend']}


def check(root, mode):
    recipe = json.loads((root / 'input/recipe.json').read_text())
    assert version('matpilot-core') == recipe['core_version']
    result = {'core_import': True, 'real_generation': False, 'scientific_validation': False}
    if mode == 'mattergen':
        code = "import json,torch,mattergen; from importlib.metadata import version; assert version('mattergen')=='%s'; assert torch.cuda.is_available(); x=torch.ones(1,device='cuda'); assert x.item()==1; print(json.dumps({'mattergen':version('mattergen'),'torch':torch.__version__,'cuda':torch.version.cuda,'gpu':torch.cuda.get_device_name(0)}))" % recipe['mattergen_version']
        proc = subprocess.run([str(root / 'mattergen/bin/python'), '-c', code], capture_output=True, text=True, timeout=120)
        if proc.returncode:
            raise RuntimeError('CUDA/import verification failed: ' + proc.stderr[-4000:])
        result['compute_environment'] = json.loads(proc.stdout.strip().splitlines()[-1])
        for entry in recipe['weights']:
            path = root / 'weights/chemical_system' / entry['relative_path']
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hash_stream(stream)
            assert digest == entry['sha256'], 'Weight checksum mismatch: ' + path.name
        result['weights_verified'] = True
    result.update(asyncio.run(asyncio.wait_for(mcp_check(root), timeout=60)))
    return result


def hash_stream(stream):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(block)
    return h.hexdigest()


def smoke(root, mode, key):
    if mode != 'mattergen':
        raise ValueError('Real verification requires the MatterGen environment')
    from matpilot.config import Settings
    from matpilot.service import Service
    # Persistent isolated budget and plan: resuming a failed deployment never starts another sample.
    marker = root / ('smoke-' + key + '.json')
    cfg = json.loads((root / 'config.json').read_text())
    cfg.update(real_compute_enabled=True, max_samples=1, data_dir=str(root / ('smoke-data-' + key)))
    cfg_path = root / ('smoke-config-' + key + '.json')
    atomic(cfg_path, cfg)
    service = Service(Settings.load(str(cfg_path)))
    if marker.exists():
        saved = json.loads(marker.read_text())
    else:
        plan = service.prepare_plan({'conditions': {'chemical_system': 'Fe-Ni-O'}, 'num_samples': 1, 'batch_size': 1})
        if plan['state'] != 'ready':
            raise RuntimeError('Smoke plan is not ready: ' + json.dumps(plan))
        saved = {'plan_id': plan['plan_id']}
        atomic(marker, saved)
    job = service.submit_job(saved['plan_id'], 'deployment-' + key, saved['plan_id'])
    deadline = time.monotonic() + 1900
    while job['status'] in ('queued', 'running') and time.monotonic() < deadline:
        time.sleep(2)
        job = service.get_job(job['job_id'])
    if job['status'] in ('queued', 'running'):
        service.control_job(job['job_id'], 'stop')
        raise RuntimeError('Smoke timeout; stop requested. Inspect job before retrying.')
    screen = service.screen_candidates(job['job_id'])
    report = service.build_report(job['job_id'])
    evidence = {'passed': job['status'] == 'completed' and screen['valid_candidate_count'] >= 1,
                'job_id': job['job_id'], 'status': job['status'], 'provenance': job['provenance'],
                'valid_candidates': screen['valid_candidate_count'], 'report': report,
                'scientific_status': 'unvalidated_candidates', 'sampling_budget': 1}
    atomic(root / ('smoke-result-' + key + '.json'), evidence)
    if not evidence['passed']:
        raise RuntimeError('One-sample verification did not pass: ' + json.dumps(evidence))
    return evidence


if __name__ == '__main__':
    action, location, mode, flag = sys.argv[1:5]
    root = Path(location)
    if action == 'configure':
        result = {'configuration_written': True, 'real_compute_enabled': config(root, mode, flag == 'true')['real_compute_enabled']}
    elif action == 'check':
        result = check(root, mode)
    elif action == 'smoke':
        result = smoke(root, mode, flag)
    else:
        raise ValueError('Unknown fixed verification action')
    print(json.dumps(result, ensure_ascii=False), flush=True)
