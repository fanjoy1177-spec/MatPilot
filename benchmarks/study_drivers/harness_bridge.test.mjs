// Actual local Python processes, synthetic echo protocol only. No API or science.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { JsonlBroker, open } from './harness_bridge.mjs';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const venvPython = path.join(project, '.venv', 'Scripts', 'python.exe');
const pythonPath = process.env.MATPILOT_TEST_PYTHON ?? (fs.existsSync(venvPython) ? venvPython : process.platform === 'win32' ? 'py.exe' : 'python3');
const script = String.raw`import argparse, json, os, sys, time
p = argparse.ArgumentParser()
p.add_argument('--group', default='U')
p.add_argument('--guidance-scale')
p.add_argument('--step-lr')
p.add_argument('--formal', action='store_true')
opts, remaining = p.parse_known_args()
for line in sys.stdin:
    request = json.loads(line)
    method, args = request['method'], request['arguments']
    if method == 'describe_tools':
        result = {'identity': {'group': opts.group, 'real_compute_allowed': False}, 'tools': [
            {'name': 'echo', 'description': 'Local protocol fixture', 'parameters': {'type': 'object'}},
            {'name': 'hang', 'description': 'Cancellation fixture', 'parameters': {'type': 'object'}}]}
    elif method == 'hang':
        time.sleep(30)
        result = {'late': True}
    elif method == 'crash':
        print('intentional fixture crash', file=sys.stderr, flush=True)
        os._exit(7)
    elif method == 'malformed':
        print('not-json', flush=True)
        continue
    elif method == 'fail':
        print(json.dumps({'id': request['id'], 'error': {'code': 'FIXTURE_ERROR', 'message': 'explicit tool failure'}}), flush=True)
        continue
    else:
        result = {'arguments': args, 'pid': os.getpid(), 'selected_group': opts.group, 'key_present': 'DEEPSEEK_API_KEY' in os.environ,
                  'guidance_scale': opts.guidance_scale, 'step_lr': opts.step_lr, 'formal': opts.formal,
                  'remaining_cli': remaining}
    print(json.dumps({'id': request['id'], 'result': result}, ensure_ascii=False), flush=True)
`;

function fixture(t, source = script) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'matpilot-broker-contract-'));
  const scriptPath = path.join(root, 'echo_broker.py'); fs.writeFileSync(scriptPath, source);
  const manifest = path.join(root, 'fixtures.json'); fs.writeFileSync(manifest, '{}');
  t.after(() => {
    const resolved = path.resolve(root);
    assert.equal(path.dirname(resolved), path.resolve(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('matpilot-broker-contract-'));
    fs.rmSync(resolved, { recursive: true, force: true });
  });
  return { root, scriptPath, manifest };
}

test('persistent real Python broker correlates requests and does not inherit provider credentials', async t => {
  const f = fixture(t), events = [];
  const previous = process.env.DEEPSEEK_API_KEY; process.env.DEEPSEEK_API_KEY = 'unit-test-sentinel';
  let broker;
  try {
    broker = await JsonlBroker.start({ pythonPath, scriptPath: f.scriptPath, journal: event => events.push(event), requestTimeoutMs: 3000 });
    const results = await Promise.all([broker.request('echo', { 中文: '值' }), broker.request('echo', { count: 2 })]);
    assert.ok(Number.isInteger(results[0].pid) && results[0].pid > 0);
    assert.equal(results[1].pid, results[0].pid);
    broker.verifiedFixtureWorkerPid = results[0].pid;
    assert.equal(results[0].arguments.中文, '值');
    assert.equal(results[1].arguments.count, 2);
    assert.equal(results[0].key_present, false);
    assert.equal(events.filter(e => e.event === 'request').length, 2);
    assert.equal(events.filter(e => e.event === 'response').length, 2);
  } finally {
    if (previous === undefined) delete process.env.DEEPSEEK_API_KEY; else process.env.DEEPSEEK_API_KEY = previous;
    await broker?.close();
  }
  assert.equal(broker.closed, true);
  assert.equal(broker.exit.code, 0);
  assert.throws(() => process.kill(broker.verifiedFixtureWorkerPid, 0), { code: 'ESRCH' });
});

test('real bridge freezes described tools, checks identity and forwards pinned science defaults', async t => {
  const f = fixture(t), events = [];
  const driver = await open({ run: { group: 'M', run_id: 'r1', mode: 'reviewed_replay' }, task: { fixture_id: 'f1' }, shared: { sampling_cap: 4, scientific_defaults: { guidance_scale: 2, step_lr: 1e-5 } }, replay: { manifest_path: f.manifest, manifest_sha256: 'a'.repeat(64), review_path: f.manifest, review_sha256: 'b'.repeat(64) }, options: { implementation_group: 'M', python_path: pythonPath, broker_path: f.scriptPath, request_timeout_ms: 3000, template_config: f.manifest }, artifactDir: path.join(f.root, 'new-artifacts'), signal: new AbortController().signal, journal: e => events.push(e) });
  try {
    assert.deepEqual(driver.tools.map(t => t.name), ['echo', 'hang']);
    const result = await driver.tools[0].execute({ num_samples: 3 }, { signal: new AbortController().signal });
    assert.equal(result.arguments.num_samples, 3);
    assert.equal(result.selected_group, 'M');
    assert.equal(result.guidance_scale, '2');
    assert.equal(result.step_lr, '0.00001');
    assert.equal(result.formal, true);
    assert.ok(result.remaining_cli.includes('--expected-manifest-sha256'));
    assert.ok(result.remaining_cli.includes('--template-config'));
    assert.ok(events.some(e => e.event === 'identity-verified'));
    assert.deepEqual(driver.tools[0].output.render({}, result), [{ type: 'text', text: JSON.stringify(result) }]);
  } finally { await driver.close(); }
});

test('tool error stays a real error while the same Python process remains usable', async t => {
  const f = fixture(t), broker = await JsonlBroker.start({ pythonPath, scriptPath: f.scriptPath, requestTimeoutMs: 3000 });
  try {
    await assert.rejects(broker.request('fail'), /FIXTURE_ERROR: explicit tool failure/);
    assert.equal((await broker.request('echo', { after: 'failure' })).arguments.after, 'failure');
  } finally { await broker.close(); }
});

test('timeout and signal cancellation kill the owned Python child and preserve termination evidence', async t => {
  for (const trigger of ['timeout', 'signal']) {
    const f = fixture(t), events = [], controller = new AbortController();
    const broker = await JsonlBroker.start({ pythonPath, scriptPath: f.scriptPath, journal: e => events.push(e), requestTimeoutMs: trigger === 'timeout' ? 150 : 3000, closeTimeoutMs: 200 });
    const workerPid = (await broker.request('echo')).pid;
    const running = broker.request('hang', {}, controller.signal);
    if (trigger === 'signal') setTimeout(() => controller.abort(), 50);
    await assert.rejects(running, trigger === 'timeout' ? /BROKER_TIMEOUT/ : /CANCELLED/);
    await broker.close();
    assert.equal(broker.closed, true);
    assert.throws(() => process.kill(workerPid, 0), { code: 'ESRCH' });
    assert.ok(events.some(e => e.event === 'terminated'));
    await assert.rejects(broker.request('echo'), /BROKER_TIMEOUT|CANCELLED|BROKER_CLOSED/);
  }
});

test('malformed stdout and abrupt exit fail pending requests instead of fabricating results', async t => {
  for (const method of ['malformed', 'crash']) {
    const f = fixture(t), events = [];
    const broker = await JsonlBroker.start({ pythonPath, scriptPath: f.scriptPath, journal: e => events.push(e), requestTimeoutMs: 3000 });
    await assert.rejects(broker.request(method), method === 'malformed' ? /INVALID_JSONL/ : /BROKER_EXIT/);
    await broker.close();
    assert.equal(broker.closed, true);
    if (method === 'crash') { assert.equal(broker.exit.code, 7); assert.ok(events.some(e => e.event === 'stderr' && e.text.includes('intentional fixture crash'))); }
  }
});

test('closing an uncooperative request waits for the owned child to terminate', async t => {
  const f = fixture(t), broker = await JsonlBroker.start({ pythonPath, scriptPath: f.scriptPath, requestTimeoutMs: 3000, closeTimeoutMs: 100 });
  const workerPid = (await broker.request('echo')).pid;
  const pending = broker.request('hang').catch(e => e);
  await broker.close();
  assert.match(String(await pending), /BROKER_CLOSED/);
  assert.equal(broker.closed, true);
  assert.ok(broker.exit);
  assert.throws(() => process.kill(workerPid, 0), { code: 'ESRCH' });
});

test('incorrect group identity and forbidden raw sample tools are rejected and reaped', async t => {
  for (const source of [script.replace("'group': opts.group", "'group': 'wrong'"), script.replace("'name': 'echo'", "'name': 'sample'")]) {
    const f = fixture(t, source), events = [];
    await assert.rejects(open({ run: { group: 'U', run_id: 'r1' }, task: { fixture_id: 'f1' }, shared: { sampling_cap: 1 }, replay: { manifest_path: f.manifest }, options: { implementation_group: 'U', python_path: pythonPath, broker_path: f.scriptPath }, artifactDir: path.join(f.root, 'new-artifacts'), journal: e => events.push(e) }), /IDENTITY_MISMATCH|INVALID_TOOLS/);
    assert.ok(events.some(e => e.event === 'exit'));
  }
});

test('failed process creation and pre-cancelled setup fail without leaving a live child', async t => {
  const f = fixture(t);
  await assert.rejects(JsonlBroker.start({ pythonPath: path.join(f.root, 'missing-python.exe'), scriptPath: f.scriptPath }), /SPAWN_FAILED/);
  const controller = new AbortController(); controller.abort();
  await assert.rejects(JsonlBroker.start({ pythonPath, scriptPath: f.scriptPath, signal: controller.signal }), /CANCELLED/);
});

test('missing, unknown or mismatched implementation selection fails before creating a child', async t => {
  const f = fixture(t);
  for (const implementationGroup of [undefined, 'unknown', 'U+', 'M_without_mapping']) {
    const events = [];
    await assert.rejects(open({ run: { group: 'U', run_id: 'r1' }, task: { fixture_id: 'f1' }, shared: { sampling_cap: 1 }, replay: { manifest_path: f.manifest }, options: { implementation_group: implementationGroup, python_path: pythonPath, broker_path: f.scriptPath }, artifactDir: path.join(f.root, 'new-artifacts'), journal: e => events.push(e) }), /IMPLEMENTATION_GROUP_MISMATCH/);
    assert.deepEqual(events, []);
  }
});

test('a shared bridge really forwards the separately pinned U and U+ implementation selector', async t => {
  const f = fixture(t), selections = [];
  for (const group of ['U', 'U+']) {
    const driver = await open({ run: { group, run_id: 'selected-' + group }, task: { fixture_id: 'f1' }, shared: { sampling_cap: 1 }, replay: { manifest_path: f.manifest }, options: { implementation_group: group, python_path: pythonPath, broker_path: f.scriptPath }, artifactDir: path.join(f.root, 'artifacts-' + group) });
    try {
      const result = await driver.tools.find(t => t.name === 'echo').execute({}, { signal: new AbortController().signal });
      assert.equal(result.selected_group, group);
      assert.equal(driver.identity.group, group);
      selections.push(result.selected_group);
    } finally { await driver.close(); }
  }
  assert.deepEqual(selections, ['U', 'U+']);
});
