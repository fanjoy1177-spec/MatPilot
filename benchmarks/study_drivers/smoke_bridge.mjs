// Explicit CPU-only bridge -> actual broker smoke. This is not an LLM evaluation.
// Run: node benchmarks/study_drivers/smoke_bridge.mjs [--python /path/to/python]
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { open } from './harness_bridge.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const SHA = value => createHash('sha256').update(value).digest('hex');
const read = filename => JSON.parse(fs.readFileSync(filename, 'utf8'));
const relative = filename => path.relative(ROOT, filename).split(path.sep).join('/');
const required = (ok, message) => assert.ok(ok, message);
const mgRequest = { model_id: 'mattergen', conditions: { chemical_system: 'Fe-Ni-O' }, num_samples: 4, batch_size: 2, review_targets: ['OER catalytic activity'] };
const dcRequest = { model_id: 'diffcsp', conditions: { chemical_formula: 'NaCl', space_group: 221 }, template_id: 'cubic_nacl', num_samples: 1, batch_size: 1 };
const scenarios = [
  { id: 'u-mattergen-odd-budget', group: 'U', engine: 'mattergen', fixture: 'development_control_semantics_0', cap: 3, upstream: true },
  { id: 'uplus-mattergen-odd-budget', group: 'U+', engine: 'mattergen', fixture: 'development_control_semantics_0', cap: 3, upstream: true },
  { id: 'u-diffcsp-template', group: 'U', engine: 'diffcsp', fixture: 'development_missing_information_0', cap: 1, upstream: true },
  { id: 'uplus-diffcsp-template', group: 'U+', engine: 'diffcsp', fixture: 'development_missing_information_0', cap: 1, upstream: true },
  { id: 'm-mattergen-plan-execute-check', group: 'M', engine: 'mattergen', fixture: 'development_control_semantics_0', cap: 4 },
  { id: 'm-diffcsp-plan-execute-check', group: 'M', engine: 'diffcsp', fixture: 'development_missing_information_0', cap: 1 },
  { id: 'm-without-mapping', group: 'M_without_mapping', engine: 'mattergen', fixture: 'development_control_semantics_0', cap: 4 },
  { id: 'm-without-evidence', group: 'M_without_evidence', engine: 'mattergen', fixture: 'development_control_semantics_0', cap: 4 },
];

function sourceSnapshot() {
  const files = new Set();
  const add = p => { if (fs.statSync(p).isFile()) files.add(path.resolve(p)); };
  const walk = directory => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.name === '__pycache__') continue;
      const p = path.join(directory, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (/\.(py|mjs|json|patch|md|txt)$/i.test(entry.name) && !entry.name.endsWith('.test.mjs')) add(p);
    }
  };
  walk(path.join(ROOT, 'src/matpilot'));
  walk(path.join(ROOT, 'benchmarks/study_drivers'));
  for (const name of ['benchmarks/baseline/source-lock.json', 'benchmarks/baseline/atomisticskills-uplus.patch', 'benchmarks/study/fixtures.development.json', 'examples/config.multimodel.demo.json']) add(path.join(ROOT, name));
  const templates = read(path.join(ROOT, 'examples/config.multimodel.demo.json')).templates;
  for (const template of Object.values(templates)) add(path.resolve(ROOT, 'examples', template.path));
  return [...files].sort().map(filename => ({ path: relative(filename), sha256: SHA(fs.readFileSync(filename)) }));
}

function scrub(value) {
  if (Array.isArray(value)) return value.map(scrub);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, scrub(v)]));
  if (typeof value === 'string') {
    let clean = value;
    for (const [prefix, label] of [[ROOT, '<repository>'], [process.env.USERPROFILE, '<home>'], [process.env.HOME, '<home>']]) {
      if (prefix) for (const form of [prefix, prefix.replaceAll('\\', '/')]) clean = clean.split(form).join(label);
    }
    return clean;
  }
  return value;
}

function identityForReport(identity) {
  const u = identity.upstream;
  return {
    group: identity.group, real_compute_allowed: identity.real_compute_allowed,
    scientific_defaults: identity.scientific_defaults, execution_backend: identity.execution_backend,
    upstream: Object.fromEntries(['source_revision', 'source_manifest_sha256', 'applied_patch_sha256', 'source_files', 'executed_function_hashes', 'template_config_sha256', 'science_computation_performed', 'limitations'].filter(k => k in u).map(k => [k, u[k]])),
    checking_layer: identity.checking_layer,
  };
}

function executionSummary(result) {
  return {
    upstream_result: Object.fromEntries(['num_generated', 'sampling_attempts', 'status', 'error', 'raised_by_upstream_function'].filter(k => k in result.upstream_result).map(k => [k, result.upstream_result[k]])),
    engine_calls: result.generator_calls.map(c => ({ batch_size: c.batch_size, actual_engine_request: c.actual_engine_request, returned_candidate_ids: (c.replay_result?.records ?? []).map(r => r.candidate_id) })),
    replay_status: result.replay_status,
    candidate_versions: (result.candidate_mapping ?? []).map(c => ({ candidate_id: c.candidate_id, structure_version_id: c.structure_version_id, parent_version_id: c.parent_version_id, sha256: c.sha256, metric_inheritance: c.metric_inheritance })),
  };
}

export async function smoke({ pythonPath, reportPath = path.join(ROOT, 'artifacts/study-driver-cpu-validation.json') } = {}) {
  pythonPath ??= path.join(ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const manifest = path.join(ROOT, 'benchmarks/study/fixtures.development.json');
  const templateConfig = path.join(ROOT, 'examples/config.multimodel.demo.json');
  const manifestSha256 = SHA(fs.readFileSync(manifest));
  const workRoot = path.join(ROOT, '.work', 'study-cpu-smoke-' + new Date().toISOString().replace(/[-:.]/g, '') + '-' + randomUUID().slice(0, 8));
  fs.mkdirSync(workRoot, { recursive: false });
  const before = sourceSnapshot(), results = [], start = performance.now();
  const summary = { format: 'matpilot-study-cpu-validation/v1', evidence_scope: 'cpu_tool_contract_only', acceptance: 'not_harness_end_to_end_acceptance', started_at: new Date().toISOString(), passed: false, api_requests: 0, api_cny: 0, real_material_model_runs: 0, scientific_values: 'synthetic, not material performance evidence', structured_entry: 'Programmatically supplied requests without raw_request; no AI semantic correctness is tested.', fixture_manifest_sha256: manifestSha256, source_files: before, source_manifest_sha256: SHA(JSON.stringify(before)), runtime: { node: process.versions.node, platform: process.platform }, cases: results, private_evidence_directory: relative(workRoot), limitations: ['No LLM or actual Harness agent was invoked.', 'No independently reviewed fixture set or formal U/U+/M outcome is established.', 'Actual upstream functions and MatPilot checks run; heavy generative engines and DiffCSP symmetry setup are replaced by explicitly synthetic CPU fixtures.', 'Wall time includes local process startup and is not evidence of AI task acceleration.'] };
  fs.writeFileSync(path.join(workRoot, 'source-before.json'), JSON.stringify(before, null, 2));
  for (const scenario of scenarios) {
    const caseDir = path.join(workRoot, scenario.id); fs.mkdirSync(caseDir);
    const caseStart = performance.now(), checks = [], raw = [], record = { ...scenario, passed: false, checks };
    results.push(record);
    let driver;
    const journal = event => fs.appendFileSync(path.join(caseDir, 'bridge.jsonl'), JSON.stringify(event) + '\n');
    try {
      driver = await open({ run: { group: scenario.group, run_id: scenario.id, mode: 'development' }, task: { fixture_id: scenario.fixture }, shared: { sampling_cap: scenario.cap, scientific_defaults: { guidance_scale: 2, step_lr: 1e-5 } }, replay: { manifest_path: manifest, manifest_sha256: manifestSha256 }, options: { implementation_group: scenario.group, python_path: pythonPath, broker_path: path.join(ROOT, 'benchmarks/study_drivers/broker.py'), template_config: templateConfig, request_timeout_ms: 30000 }, artifactDir: path.join(caseDir, 'artifacts'), signal: new AbortController().signal, journal });
      record.identity = identityForReport(driver.identity);
      const toolMap = new Map(driver.tools.map(t => [t.name, t]));
      const call = async (name, args = {}) => {
        required(toolMap.has(name), `missing tool ${name}`);
        const result = await toolMap.get(name).execute(args, { signal: new AbortController().signal });
        raw.push({ name, arguments: args, result });
        fs.writeFileSync(path.join(caseDir, 'calls.json'), JSON.stringify(raw, null, 2));
        return result;
      };
      for (const name of ['get_candidates', 'get_metrics', 'read_artifact', 'list_templates', 'list_upstream_artifacts']) required(toolMap.has(name), 'all groups retain common read access: ' + name);
      required(!toolMap.has('sample'), 'raw replay sample is not agent-visible');
      checks.push('shared read access and raw-sample exclusion');
      let generated;
      if (scenario.upstream) {
        generated = scenario.engine === 'mattergen'
          ? await call('generate_structures', { num_structures: 3, batch_size: 2, model_name: 'chemical_system', chemical_system: 'Fe-Ni-O', guidance_scale: 2, device: 'cpu' })
          : await call('generate_structures_with_symmetry', { num_samples: 1, batch_size: 1, model_name: 'mp_csp', spacegroup: 221, wyckoff_letters: '1a,1b', atom_types: 'Na,Cl', step_lr: 1e-5 });
        if (scenario.engine === 'mattergen' && scenario.group === 'U') {
          required(Boolean(generated.upstream_result.error), 'unpatched rounded-up request is rejected');
          assert.equal(generated.generator_calls[0].actual_engine_request.num_samples, 4);
          assert.equal(generated.replay_status.attempts_used, 0);
          assert.equal(generated.replay_status.requested_samples, 4);
          checks.push('original U requests 4 for num_structures=3,batch_size=2; hard cap=3 rejects before any attempt');
        } else if (scenario.engine === 'mattergen') {
          assert.deepEqual(generated.generator_calls.map(c => c.batch_size), [2, 1]);
          assert.equal(generated.upstream_result.num_generated, 3);
          assert.equal(generated.replay_status.attempts_used, 3);
          checks.push('actual U+ wrapper executes exact 2+1 batches under the same cap=3');
        } else {
          assert.equal(generated.upstream_result.num_generated, 1);
          assert.equal(generated.replay_status.attempts_used, 1);
          required(Boolean(generated.identity.executed_function_hashes['diffcsp.wrapper.generate_with_symmetry']), 'actual DiffCSP wrapper function is identified');
          checks.push('actual DiffCSP server and wrapper return the registered-template synthetic candidate');
        }
      } else {
        const request = structuredClone(scenario.engine === 'mattergen' ? mgRequest : dcRequest);
        const prepared = await call('prepare_plan', { request });
        assert.equal(prepared.plan.state, 'ready', JSON.stringify(prepared.plan.issues));
        generated = await call('execute_plan', { plan_id: prepared.plan.plan_id });
        assert.equal(generated.upstream_result.num_generated, request.num_samples);
        const candidates = await call('get_candidates');
        const candidateIds = candidates.records.map(c => c.candidate_id);
        assert.equal(candidateIds.length, request.num_samples);
        const checked = await call('check_candidates', { plan_id: prepared.plan.plan_id, candidate_ids: candidateIds });
        assert.equal(checked.checks_performed, scenario.group !== 'M_without_evidence');
        assert.equal(prepared.study_checks.requirement_mapping, scenario.group !== 'M_without_mapping');
        assert.equal(prepared.study_checks.evidence_checks, scenario.group !== 'M_without_evidence');
        if (scenario.group === 'M_without_mapping') { assert.deepEqual(prepared.plan.requirement_mappings, []); assert.equal(prepared.plan.coverage.program_mapping_coverage, null); }
        if (scenario.group === 'M_without_evidence') { assert.equal(checked.valid_candidate_count, null); required(!Object.hasOwn(checked.records[0], 'all_hard_requirements_pass'), 'ablated evidence produces raw data, not passed checks'); }
        record.plan = { state: prepared.plan.state, model_id: prepared.plan.model_id, checkpoint: prepared.plan.checkpoint, num_samples: prepared.plan.num_samples, requirement_mapping_count: prepared.plan.requirement_mappings.length, coverage: prepared.plan.coverage };
        record.candidate_checks = { checks_performed: checked.checks_performed, valid_candidate_count: checked.valid_candidate_count, unique_valid_candidate_count: checked.unique_valid_candidate_count, records: checked.records.map(c => ({ candidate_id: c.candidate_id, structure_version_id: c.structure_version_id, all_hard_requirements_pass: c.all_hard_requirements_pass ?? null, checks: (c.requirement_checks ?? []).map(r => ({ requirement_id: r.requirement_id, status: r.status })) })) };
        checks.push('actual MatPilot structured prepare -> actual patched upstream execute -> candidate-ID checks');
        if (scenario.group !== 'M') checks.push('ablation changes executed feature flags and observable checking behavior');
      }
      record.execution = executionSummary(generated);
      const status = await call('status');
      assert.ok(status.attempts_used <= scenario.cap);
      checks.push('shared sampling cap never exceeded');
      const artifactRows = await call('list_upstream_artifacts');
      record.upstream_artifact_count = artifactRows.artifacts.length;
      record.passed = true;
    } catch (e) { record.error = scrub({ code: e.code ?? e.name, message: e.message }); }
    finally {
      try { await driver?.close(); } catch (e) { record.passed = false; record.close_error = scrub(String(e)); }
      record.wall_seconds = (performance.now() - caseStart) / 1000;
      fs.writeFileSync(path.join(caseDir, 'result.json'), JSON.stringify(scrub(record), null, 2));
      process.stdout.write(`${record.passed ? 'PASS' : 'FAIL'} ${scenario.id}${record.error ? ': ' + record.error.message : ''}\n`);
    }
  }
  const after = sourceSnapshot();
  summary.source_files_unchanged_during_run = JSON.stringify(before) === JSON.stringify(after);
  summary.passed = results.every(r => r.passed) && summary.source_files_unchanged_during_run;
  summary.completed_at = new Date().toISOString(); summary.wall_seconds = (performance.now() - start) / 1000;
  summary.passed_cases = results.filter(r => r.passed).length; summary.total_cases = results.length;
  fs.writeFileSync(path.join(workRoot, 'source-after.json'), JSON.stringify(after, null, 2));
  const publicReport = scrub(summary), text = JSON.stringify(publicReport, null, 2) + '\n';
  required(!text.includes(ROOT) && (!process.env.USERPROFILE || !text.includes(process.env.USERPROFILE)), 'public report must not contain local user paths');
  fs.mkdirSync(path.dirname(path.resolve(reportPath)), { recursive: true });
  if (fs.existsSync(reportPath)) fs.copyFileSync(reportPath, path.join(workRoot, 'previous-public-report.json'));
  fs.writeFileSync(reportPath, text);
  fs.writeFileSync(path.join(workRoot, 'report.json'), text);
  return { passed: summary.passed, passed_cases: summary.passed_cases, total_cases: summary.total_cases, evidence_scope: summary.evidence_scope, report: relative(path.resolve(reportPath)) };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2), options = {};
  for (let i = 0; i < args.length; i += 2) {
    if (!['--python', '--report'].includes(args[i]) || !args[i + 1]) throw new Error('Usage: node smoke_bridge.mjs [--python path] [--report path]');
    options[args[i] === '--python' ? 'pythonPath' : 'reportPath'] = path.resolve(args[i + 1]);
  }
  const result = await smoke(options); process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  if (!result.passed) process.exitCode = 1;
}
