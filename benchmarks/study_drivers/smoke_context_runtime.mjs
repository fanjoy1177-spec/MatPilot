// Explicit development-only CPU checks through the REAL Harness ToolRuntime.
// No agent loop, provider, account service, LLM request or material model is used.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { loadRuntimePack } from '../harness_study.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const hash = value => createHash('sha256').update(value).digest('hex');
const read = filename => JSON.parse(fs.readFileSync(filename, 'utf8'));
const ids = ['T0502', 'T0505', 'T0603', 'T0605', 'T0606'];
const groups = ['U', 'U+', 'M'];
const sharedNames = ['status', 'list_artifacts', 'read_artifact', 'get_candidates', 'get_metrics',
  'get_versions', 'query_database', 'verify_candidate_artifact', 'get_input_capabilities',
  'list_templates', 'list_upstream_artifacts', 'read_upstream_artifact'];
const permitted = new Set([...sharedNames, 'prepare_plan', 'check_candidates', 'compare_candidate_metrics', 'get_capabilities']);
const saveNew = (filename, value) => fs.writeFileSync(filename, JSON.stringify(value, null, 2) + '\n', { flag: 'wx' });

function sanitize(value) {
  if (Array.isArray(value)) return value.map(sanitize);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, sanitize(v)]));
  if (typeof value !== 'string') return value;
  let text = value;
  for (const prefix of [ROOT, process.env.USERPROFILE, process.env.HOME]) {
    if (prefix) for (const form of [prefix, prefix.replaceAll('\\', '/')]) text = text.split(form).join('<local>');
  }
  return text.replace(/[A-Za-z]:[\\/][^\s"'<>]+/g, '<local-path>').replace(/\bBearer\s+\S+/gi, 'Bearer [REDACTED]');
}

function factIdentity(record) {
  return Object.fromEntries(['candidate_id', 'structure_version_id', 'version_key', 'parent_version_id',
    'transformation', 'sha256'].filter(k => k in record).map(k => [k, record[k]]));
}

async function loadHarness(modules) {
  const directory = path.resolve(modules, '@deepseek-ai');
  const packages = {}, imported = {};
  for (const name of ['cordis', 'dsh-system-prompt', 'dsh-tools']) {
    const base = path.join(directory, name), packageFile = path.join(base, 'package.json'), entry = path.join(base, 'lib/index.js');
    packages[name] = { version: read(packageFile).version, package_sha256: hash(fs.readFileSync(packageFile)), entrypoint_sha256: hash(fs.readFileSync(entry)) };
    imported[name] = await import(pathToFileURL(entry).href);
  }
  return { packages, Context: imported.cordis.Context, SystemPrompt: imported['dsh-system-prompt'].default,
    ToolRuntime: imported['dsh-tools'].default };
}

export async function smokeContext({ packPath, harnessModulesPath, outputDir, reportPath }) {
  const loaded = loadRuntimePack(packPath), { pack, tasks, drivers } = loaded;
  assert.equal(pack.mode, 'development', 'This smoke must never create or promote formal acceptance.');
  assert.equal(pack.real_compute_allowed, false);
  assert.equal(pack.rows.length, ids.length * groups.length);
  assert.deepEqual([...new Set(pack.rows.map(r => r.task_id))].sort(), [...ids].sort());
  for (const id of ids) for (const group of groups) assert.equal(pack.rows.filter(r => r.task_id === id && r.group === group).length, 1);
  const base = path.dirname(loaded.packPath);
  assert.equal(hash(fs.readFileSync(path.resolve(base, pack.replay.manifest_path))), pack.replay.manifest_sha256);
  const out = path.resolve(outputDir);
  fs.mkdirSync(out, { recursive: false });
  fs.mkdirSync(path.join(out, 'runs'));
  const harness = await loadHarness(harnessModulesPath);
  assert.equal(harness.packages['dsh-tools'].version, pack.shared.host_version);
  // This process composes only tools and system-prompt services. A future edit
  // that accidentally calls fetch is refused instead of reaching a provider.
  const originalFetch = globalThis.fetch;
  let refusedNetworkCalls = 0;
  globalThis.fetch = async () => { refusedNetworkCalls++; throw new Error('CPU-only smoke forbids network requests'); };
  const report = { format: 'matpilot-shared-context-harness-tools-cpu/v1', evidence_scope: 'cpu_tool_contract_only',
    acceptance: 'real_harness_tool_runtime_not_llm_end_to_end', formal_ready: false, passed: false,
    started_at: new Date().toISOString(), runtime_pack_sha256: loaded.packSha256,
    source_manifest_sha256: Object.fromEntries(groups.map(group => [group, drivers.get(group).source_manifest_sha256])),
    fixture_manifest_sha256: pack.replay.manifest_sha256, harness_packages: harness.packages,
    api_requests: 0, api_cny: 0, real_material_model_runs: 0, llm_agent_created: false,
    credentials_service_loaded: false, cost_ledger_opened: false, cases: [], comparisons: [],
    invocation: 'node benchmarks/study_drivers/smoke_context_runtime.mjs --pack <new-pack>/runtime.json --harness-modules <harness>/node_modules --output <new-evidence-directory> --report <new-report.json>',
    limitations: ['Developer-seen pending-review cases; not a formal freeze or independent assessment.',
      'Programmatic tool arguments are fixed CPU assertions, not language-model decisions or scores.',
      'Real Harness ToolRuntime registration, guards, execution and result normalization are exercised; no agent loop or desktop UI session is exercised.',
      'Execution receipts and timestamps inside fixtures are synthetic observations, not live worker-stop or GPU acceptance.',
      'Passing does not establish material properties, AI task accuracy, task-time reduction or a comparison effect.',
      'Host hashes identify loaded package entrypoints, not an independently certified full dependency closure.'] };
  const start = performance.now();
  try {
    for (const row of pack.rows) {
      const runDir = path.join(out, 'runs', row.run_id); fs.mkdirSync(runDir);
      const record = { task_id: row.task_id, group: row.group, passed: false, checks: [] };
      report.cases.push(record);
      let driver, ctx, sequence = 0, notifications = 0;
      const journal = event => fs.appendFileSync(path.join(runDir, 'bridge.jsonl'), JSON.stringify(event) + '\n');
      try {
        const definition = drivers.get(row.group), module = await import(pathToFileURL(definition.modulePath).href);
        driver = await module.open({ run: row, task: tasks.get(row.task_id), shared: pack.shared, replay: pack.replay,
          options: definition.options, artifactDir: path.join(runDir, 'artifacts'), signal: new AbortController().signal, journal });
        assert.equal(driver.identity.shared_context_contract, 'matpilot-shared-replay-context/v2');
        ctx = new harness.Context();
        await ctx.plugin(harness.SystemPrompt, { includeHarnessIdentity: false }).await();
        await ctx.plugin(harness.ToolRuntime, { mode: 'native' }).await();
        assert.equal(ctx.get('llm'), undefined);
        ctx.on('tools/result', (execution, result) => {
          notifications++;
          fs.appendFileSync(path.join(runDir, 'harness-results.jsonl'), JSON.stringify({ callId: execution.callId, name: execution.name, arguments: execution.arguments, result }) + '\n');
        });
        for (const tool of driver.tools) ctx.tools.register(tool);
        ctx.tools.guard(execution => permitted.has(execution.name) ? undefined : 'CPU context smoke permits only read operations and non-executing plan/check tools.');
        const schemas = ctx.tools.schemas();
        for (const name of sharedNames) assert.ok(schemas.some(s => s.name === name), `missing common tool ${name}`);
        assert.ok(!schemas.some(s => s.name === 'sample'));
        record.shared_tool_schema_sha256 = hash(JSON.stringify(schemas.filter(s => sharedNames.includes(s.name)).sort((a, b) => a.name.localeCompare(b.name))));
        record.tool_count = schemas.length;
        const call = async (name, args = {}, shouldFail = false) => {
          const result = await ctx.tools.execute({ callId: 'cpu-' + ++sequence, name, arguments: args, signal: AbortSignal.timeout(35000) });
          assert.equal(result.isError, shouldFail, JSON.stringify({ name, error: result.error }));
          return shouldFail ? result : result.value;
        };
        const readArtifact = async artifact => {
          const result = await call('read_artifact', { artifact_id: artifact.artifact_id });
          const bytes = Buffer.from(result.content, result.encoding === 'base64' ? 'base64' : 'utf8');
          assert.equal(hash(bytes), result.sha256); assert.equal(result.sha256, artifact.sha256);
          return result;
        };
        const initial = await call('status');
        const blocked = await call(row.group === 'M' ? 'execute_plan' : 'generate_structures', {}, true);
        assert.match(JSON.stringify(blocked.error), /CPU context smoke/);
        record.generation_guard_error_code = blocked.error.code;
        record.checks.push('actual Harness registry, canonical result event and shared tool schemas; generation guard blocks dispatch');
        const facts = {};
        if (row.task_id === 'T0502') {
          const versions = (await call('get_versions', { candidate_id: 'c_a' })).records;
          const original = versions.find(v => v.version_key === 's_a1'), relaxed = versions.find(v => v.version_key === 's_a2');
          assert.ok(original && relaxed); assert.equal(relaxed.parent_version_id, original.structure_version_id);
          assert.notEqual(original.structure_version_id, relaxed.structure_version_id);
          facts.versions = versions.map(factIdentity);
          facts.metrics = [];
          for (const version of [original, relaxed]) {
            const metrics = (await call('get_metrics', { candidate_id: 'c_a', structure_version_id: version.structure_version_id })).metrics;
            assert.equal(metrics.length, 1);
            const metric = metrics[0]; assert.equal(metric.structure_version_id, version.structure_version_id);
            assert.equal(metric.input_artifact.sha256, version.sha256);
            await readArtifact(version);
            facts.metrics.push({ structure_version_id: metric.structure_version_id, property: metric.property, value: metric.value, unit: metric.unit, method: metric.method, input_sha256: metric.input_artifact.sha256, raw_sha256: metric.raw_output.sha256 });
          }
          assert.deepEqual(facts.metrics.map(m => m.value), [1.1, 1.6]);
          if (row.group === 'M') {
            const selection = versions.map(v => ({ candidate_id: v.candidate_id, structure_version_id: v.structure_version_id }));
            const comparison = await call('compare_candidate_metrics', { candidate_ids: ['c_a'], property_name: 'dft_band_gap', structure_versions: selection });
            assert.equal(comparison.comparable, true); assert.deepEqual(comparison.metrics.map(m => m.value), [1.1, 1.6]);
            const prepared = await call('prepare_plan', { request: { conditions: { chemical_system: 'Na-Cl' }, num_samples: 1 } });
            const checked = await call('check_candidates', { plan_id: prepared.plan.plan_id, candidate_ids: ['c_a'], structure_versions: [{ candidate_id: 'c_a', structure_version_id: relaxed.structure_version_id }] });
            assert.equal(checked.checks_performed, true); assert.equal(checked.records[0].structure_version_id, relaxed.structure_version_id);
            record.checks.push('M explicit-version production evidence checks retain s_a2 and compare one metric per candidate/version');
          }
          record.checks.push('s_a1 and s_a2 remain distinct; exact-version metrics and CIF bytes are accessible');
        } else if (row.task_id === 'T0505') {
          const result = await call('query_database', { database: 'materials_project', query: { candidate_id: 'c_a', formula: 'NaCl' } });
          assert.equal(result.status, 'failed'); assert.equal(result.novelty, 'unknown'); assert.equal(result.records, null);
          const raw = await readArtifact(result.raw_output);
          facts.database = { status: result.status, novelty: result.novelty, records: result.records, error: result.error, raw_sha256: raw.sha256 };
          record.checks.push('failed database query retains unknown novelty, null records and readable failed-response bytes');
        } else if (row.task_id === 'T0603' || row.task_id === 'T0605') {
          const execution = initial.execution;
          assert.equal(execution.termination_evidence_scope, 'synthetic_observation_not_live_process_supervision');
          const stopped = row.task_id === 'T0603';
          assert.equal(execution.state, stopped ? 'stopped' : 'running');
          assert.equal(execution.termination_confirmed, stopped);
          assert.equal(execution.heartbeat_late, !stopped);
          if (!stopped) assert.equal(execution.resume_allowed, false);
          const raw = await readArtifact(execution.raw_output), attempts = await readArtifact(execution.attempt_ledger_artifact);
          const candidates = (await call('get_candidates')).records;
          facts.execution = { state: execution.state, termination_confirmed: execution.termination_confirmed,
            heartbeat_late: execution.heartbeat_late, resume_allowed: execution.resume_allowed,
            observed_at: execution.observed_at, heartbeat_at: execution.heartbeat_at,
            raw_sha256: raw.sha256, attempt_ledger_sha256: attempts.sha256, candidates: candidates.map(factIdentity) };
          record.checks.push(stopped ? 'confirmed stop exposes completed artifacts without restarting execution' : 'late heartbeat remains running/unconfirmed and cannot justify resuming the same job');
        } else {
          const verification = await call('verify_candidate_artifact', { candidate_id: 'c_a' });
          assert.equal(verification.intact, false); assert.notEqual(verification.expected_sha256, verification.observed_sha256);
          const original = await readArtifact(verification.original_artifact), observed = await readArtifact(verification.observed_artifact);
          assert.equal(original.sha256, verification.expected_sha256); assert.equal(observed.sha256, verification.observed_sha256);
          await readArtifact(verification.observation_receipt);
          const versions = (await call('get_versions', { candidate_id: 'c_a' })).records;
          assert.ok(versions.length); await call('get_metrics', { candidate_id: 'c_a' }, true);
          await call('get_candidates', { candidate_ids: ['c_a'] }, true);
          facts.integrity = { intact: verification.intact, expected_sha256: verification.expected_sha256, observed_sha256: verification.observed_sha256, versions: versions.map(factIdentity) };
          if (row.group === 'M') {
            const prepared = await call('prepare_plan', { request: { conditions: { chemical_system: 'Na-Cl' }, num_samples: 1 } });
            await call('check_candidates', { plan_id: prepared.plan.plan_id, candidate_ids: ['c_a'], structure_versions: [{ candidate_id: 'c_a', structure_version_id: versions[0].structure_version_id }] }, true);
            record.checks.push('M check refuses to endorse changed CIF using the old version evidence');
          }
          record.checks.push('all groups can inspect original/observed bytes and metadata while stale metric reads fail');
        }
        const final = await call('status');
        assert.equal(final.attempts_used, initial.attempts_used); assert.equal(final.requested_samples, initial.requested_samples);
        assert.equal(notifications, sequence);
        record.tool_executions = sequence; record.harness_result_events = notifications;
        record.shared_facts = facts; record.shared_facts_sha256 = hash(JSON.stringify(facts));
        record.sampling_attempts_delta = final.attempts_used - initial.attempts_used;
        record.passed = true;
      } catch (error) { record.error = sanitize({ code: error.code ?? error.name, message: error.message }); }
      finally {
        for (const cleanup of [() => ctx?.fiber.dispose(), () => driver?.close()]) {
          try { await cleanup(); }
          catch (error) { record.passed = false; record.cleanup_error = sanitize(error.message); }
        }
        saveNew(path.join(runDir, 'case.json'), sanitize(record));
        process.stdout.write(`${record.passed ? 'PASS' : 'FAIL'} ${row.run_id}\n`);
      }
    }
    for (const taskId of ids) {
      const rows = report.cases.filter(r => r.task_id === taskId);
      report.comparisons.push({ task_id: taskId, same_shared_tool_schema: rows.length === 3 && new Set(rows.map(r => r.shared_tool_schema_sha256)).size === 1,
        same_shared_facts: rows.every(r => r.passed) && new Set(rows.map(r => r.shared_facts_sha256)).size === 1 });
    }
    assert.equal(loadRuntimePack(packPath).packSha256, loaded.packSha256);
    report.pinned_sources_unchanged = true;
    report.passed = report.cases.every(r => r.passed) && report.comparisons.every(c => c.same_shared_facts && c.same_shared_tool_schema) && refusedNetworkCalls === 0;
  } finally {
    globalThis.fetch = originalFetch;
    report.refused_network_calls = refusedNetworkCalls;
    report.completed_at = new Date().toISOString(); report.wall_seconds = (performance.now() - start) / 1000;
    report.passed_cases = report.cases.filter(r => r.passed).length; report.total_cases = report.cases.length;
    const publicReport = sanitize(report);
    saveNew(path.join(out, 'report.json'), publicReport);
    if (reportPath) { fs.mkdirSync(path.dirname(path.resolve(reportPath)), { recursive: true }); saveNew(path.resolve(reportPath), publicReport); }
  }
  return report;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const options = {}, names = { '--pack': 'packPath', '--harness-modules': 'harnessModulesPath', '--output': 'outputDir', '--report': 'reportPath' };
  for (let i = 2; i < process.argv.length; i += 2) {
    const key = names[process.argv[i]], value = process.argv[i + 1];
    if (!key || !value) throw new Error('Expected --pack, --harness-modules, --output and optional --report paths.');
    options[key] = path.resolve(value);
  }
  for (const key of ['packPath', 'harnessModulesPath', 'outputDir']) assert.ok(options[key], `Missing ${key}`);
  const result = await smokeContext(options);
  process.stdout.write(JSON.stringify({ passed: result.passed, cases: result.passed_cases + '/' + result.total_cases,
    evidence_scope: result.evidence_scope, api_requests: result.api_requests }) + '\n');
  if (!result.passed) process.exitCode = 1;
}
