// CPU-only contract tests: this fake host never imports a provider or calls an API.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { loadRuntimePack, runStudy, BudgetLedger, clarificationTool, redact, terminalStatus, STUDY_PROMPT_SHADOWS } from './harness_study.mjs';

const hash = value => createHash('sha256').update(value).digest('hex');
const digest = hash('shared-science');
const prices = { input_cny_per_million: 2, output_cny_per_million: 8, max_input_tokens: 1048576, max_output_tokens: 100 };
function workspace(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'matpilot-study-contract-'));
  t.after(() => {
    const resolved = path.resolve(root);
    assert.equal(path.dirname(resolved), path.resolve(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('matpilot-study-contract-'));
    fs.rmSync(resolved, { recursive: true, force: true });
  });
  return root;
}
function fixture(t, change = () => {}) {
  const root = workspace(t), drivers = {};
  const write = (name, content) => { const p = path.join(root, name); const bytes = typeof content === 'string' ? content : JSON.stringify(content); fs.writeFileSync(p, bytes); return { path: p, sha256: hash(bytes) }; };
  const cap = write('capability.json', { engines: ['replay-only'] });
  for (const group of ['U', 'U+', 'M']) {
    const module = write(group + '.mjs', `export async function open(context) { if(context.options.implementation_group!==context.run.group) throw new Error('implementation binding changed'); return {tools:[{name:'sample_${group.replace('+', 'plus')}',description:'Replay fixture only',parameters:{type:'object'},output:{schema:{type:'object'},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},execute:async(args)=>{context.journal({operation:'fixture',args});return {implementation:context.options.implementation_group,fixture_id:context.task.fixture_id};}}]};}`);
    const instruction = write(group + '.txt', 'Ordinary documentation for ' + group);
    const source = write(group + '.source.json', { source_revision: 'pinned-test-' + group, files: [{ path: path.basename(module.path), sha256: module.sha256 }, { path: path.basename(instruction.path), sha256: instruction.sha256 }] });
    drivers[group] = { module_path: module.path, module_sha256: module.sha256, instruction_path: instruction.path, instruction_sha256: instruction.sha256, capability_path: cap.path, capability_sha256: cap.sha256, source_manifest_path: source.path, source_manifest_sha256: source.sha256, source_revision: 'pinned-test-' + group, configuration_hash: hash(group), driver_validated: false, scientific_capability_hash: cap.sha256, replay_fixture_hash: digest, clarification_policy_hash: digest, options: { implementation_group: group } };
  }
  const pack = { format: 'matpilot-study-runtime/v1', mode: 'development', real_compute_allowed: false, provenance: { plan_sha256: digest, suite_sha256: digest }, shared: { provider: 'fake', model: 'fake', reasoning_effort: 'off', max_turns: 2, max_output_tokens: 100, max_requests: 3, timeout_ms: 1000, sampling_cap: 4, scientific_capability_hash: digest, replay_fixture_hash: digest, clarification_policy_hash: digest }, drivers, tasks: [{ id: 'D01', prompt: '请检查回放样本，元素不明确时向我提问。', fixture_id: 'fixture01', fixed_clarifications: [{ slot: 'elements', answer: 'Na Cl' }] }], rows: ['U', 'U+', 'M'].map((group, i) => ({ run_id: 'run' + i, task_id: 'D01', group, repeat: 1, mode: 'development', configuration_hash: drivers[group].configuration_hash })) };
  pack.shared.scientific_capability_hash = cap.sha256;
  change(pack);
  const runtime = write('runtime.json', pack);
  const ledgerPath = path.join(root, 'budget.json');
  const historical = Array.from({ length: 97 }, (_, i) => ({ id: 'history-' + i, accounted_cny: i === 0 ? 3.557394 : 0 }));
  fs.writeFileSync(ledgerPath, JSON.stringify({ records: historical, limit_cny: 19, user_limit_cny: 20 }));
  return { root, pack, historical, config: { packPath: runtime.path, outputDir: path.join(root, 'output'), projectRoot: root, ledgerPath, prices, batch_guard_cny: 10 } };
}

function fakeHarness({ failFirst = false, timeoutFirst = false, incompleteUsage = false, stuckCreateFirst = false, stuckIdleFirst = false, reintroduceProductionPrompt = false } = {}) {
  const global = new Map(), creations = [], prompts = [];
  const globalPromptSections = new Map([
    ['harness:identity', { name: 'harness:identity', text: 'UNRELATED HOST GUIDANCE' }],
    ['mcp:matpilot', { name: 'mcp:matpilot', text: 'PRODUCTION MATPILOT: get_capabilities prepare_plan submit_job approved_plan_id' }],
    ['mcp-resource-servers', { name: 'mcp-resource-servers', text: 'PRODUCTION MCP RESOURCE ADVERTISEMENT' }],
    ['ordinary:skill', { name: 'ordinary:skill', text: 'ORDINARY SHARED SKILL' }],
  ]);
  const ctx = {
    on(name, handler) { global.set(name, handler); return () => global.delete(name); },
    agents: { async create(options) {
      const local = new Map(), tools = new Map(), events = [];
      const ordinal = creations.length;
      const info = { options, tools, restriction: null, promptSections: [] }; creations.push(info);
      if (stuckCreateFirst && ordinal === 0) return new Promise(() => {});
      const session = { snapshotEvents: () => structuredClone(events) };
      const event = (type, data) => { const e = { seq: events.length, type, data }; events.push(e); local.get('session/event')?.(session, e); };
      const agentCtx = {
        tools: {
          restrict(filter) { info.restriction = filter; }, register(tool) { tools.set(tool.name, tool); }, presentAs(mode) { assert.equal(mode, 'native'); }, guard(fn) { info.guard = fn; },
        },
        on(name, handler) { local.set(name, handler); },
        systemPrompt: { getSectionOrder(name) { assert.equal(name, 'MCP_SERVERS'); return 3100; }, section(section) { info.promptSections.push(section); prompts.push(section.text); } },
      };
      await options.setup(agentCtx);
      let pending, finishTimeout;
      const perform = async message => {
        prompts.push(message.content[0].text);
        const sectionMap = new Map([...globalPromptSections].map(([name, section]) => [name, { ...section }]));
        for (const section of info.promptSections) sectionMap.set(section.name, { ...section });
        if (reintroduceProductionPrompt) sectionMap.set('mcp:matpilot', { ...globalPromptSections.get('mcp:matpilot') });
        let assembly = { sections: [...sectionMap.values()], contexts: [], tools: [...tools.keys()], variables: {} };
        const assemble = local.get('system-prompt/assemble');
        if (assemble) assembly = await assemble(assembly, {}, async () => assembly);
        event('system/message', { message: { role: 'system', content: [{ type: 'text', text: assembly.sections.map(s => s.text).filter(Boolean).join('\n\n') }] } });
        event('request/header', { config: { model: 'fake', apiKey: 'private-value', authorization: 'Bearer xyz' }, tools: [...tools.keys()] });
        event('turn/start', { turn: 0 });
        if (timeoutFirst && ordinal === 0) return new Promise(resolve => { finishTimeout = resolve; });
        if (stuckIdleFirst && ordinal === 0) return new Promise(() => {});
        event('step/start', { step: 0 });
        const hook = global.get('llm/stream');
        try {
          for await (const _chunk of hook({ sessionId: options.sessionId, provider: 'fake', model: 'fake', maxTokens: 100 }, async function* () {
            if (incompleteUsage) throw new Error('network ended before usage');
            yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 4 } };
            yield { type: 'finish', reason: 'stop' };
          })) { /* no external model */ }
          if (failFirst && ordinal === 0) throw new Error('deliberate first-run failure');
          for (const [name, tool] of tools) {
            const arguments_ = name === 'ask_clarification' ? { slot: 'elements', question: 'What elements?' } : {};
            assert.equal(info.guard({ name, arguments: arguments_ }), undefined);
            const decision = await local.get('tools/pre-execute')({ name, arguments: arguments_ });
            assert.equal(decision.kind, 'allow');
            const value = await tool.execute(arguments_, { signal: new AbortController().signal });
            const result = { isError: false, value, content: tool.output.render(arguments_, value) };
            local.get('tools/result')({ name, arguments: arguments_ }, result);
            event('tool/result', { name, result });
          }
          event('assistant/message', { content: 'fixture output, not material evidence' });
          event('step/end', { step: 0 });
          event('turn/end', { turn: 0, reason: { kind: 'completed' } });
        } catch (e) { event('turn/end', { turn: 0, reason: { kind: 'error', error: { message: String(e) } } }); throw e; }
      };
      const agent = { session, followup(message) { pending = perform(message); }, whenIdle() { return pending; }, cancel(cause) { event('turn/end', { turn: 0, reason: { kind: 'aborted', reason: cause } }); finishTimeout?.(); } };
      return { agent, async dispose() {} };
    } },
  };
  return { ctx, creations, prompts, global, globalPromptSections };
}
const input = x => x;

test('isolated tools, explicit clarification, full redacted traces and existing cost history', async t => {
  const f = fixture(t), host = fakeHarness();
  const result = await runStudy({ ctx: host.ctx, config: f.config, createUserMessage: input });
  assert.deepEqual(result.outcomes.map(x => x.status), ['completed', 'completed', 'completed']);
  assert.deepEqual(host.creations.map(c => [...c.tools.keys()]), [['sample_U', 'ask_clarification'], ['sample_Uplus', 'ask_clarification'], ['sample_M', 'ask_clarification']]);
  for (const c of host.creations) assert.deepEqual(c.restriction, { allow: [] });
  const trace = JSON.parse(fs.readFileSync(path.join(f.config.outputDir, 'runs/run0/trace.json')));
  assert.ok(trace.events.some(e => e.type === 'request/header'));
  assert.ok(trace.events.some(e => e.type === 'step/end'));
  assert.equal(trace.terminal.turn_end_reason.kind, 'completed');
  assert.equal(trace.clarifications.length, 1);
  assert.ok(trace.redactions.length >= 2);
  assert.ok(!JSON.stringify(trace).includes('private-value'));
  const ledger = JSON.parse(fs.readFileSync(f.config.ledgerPath));
  assert.equal(ledger.records.length, 100);
  assert.deepEqual(ledger.records.slice(0, 97), f.historical);
  assert.ok(ledger.accounted_upper_bound_cny > 3.557394);
  assert.equal(host.global.has('llm/stream'), false);
  const disk = JSON.parse(fs.readFileSync(path.join(f.config.outputDir, 'runs/run0/result.json')));
  assert.equal(disk.correctness, 'ungraded');
  assert.equal(disk.outcome.human_active_seconds, null);
  assert.equal(disk.trace_sha256, hash(fs.readFileSync(path.join(f.config.outputDir, 'runs/run0/trace.json'))));
});

test('only child-agent production MCP sections are shadowed while host and pinned group skills remain', async t => {
  const f = fixture(t), host = fakeHarness();
  const before = structuredClone([...host.globalPromptSections]);
  await runStudy({ ctx: host.ctx, config: f.config, createUserMessage: input });
  assert.deepEqual(STUDY_PROMPT_SHADOWS, ['mcp:matpilot', 'mcp-resource-servers']);
  assert.deepEqual([...host.globalPromptSections], before);
  for (const [index, creation] of host.creations.entries()) {
    const shadows = creation.promptSections.filter(s => STUDY_PROMPT_SHADOWS.includes(s.name));
    assert.deepEqual(shadows.map(s => s.name), STUDY_PROMPT_SHADOWS);
    for (const s of shadows) { assert.equal(s.text, ''); assert.equal(s.order, 3100); assert.equal(s.interpolate, false); assert.notEqual(s.complete, true); }
    const trace = JSON.parse(fs.readFileSync(path.join(f.config.outputDir, `runs/run${index}/trace.json`)));
    const rendered = trace.events.find(e => e.type === 'system/message').data.message.content[0].text;
    assert.ok(!rendered.includes('PRODUCTION MATPILOT'));
    assert.ok(!rendered.includes('PRODUCTION MCP RESOURCE ADVERTISEMENT'));
    assert.ok(rendered.includes('UNRELATED HOST GUIDANCE'));
    assert.ok(rendered.includes('ORDINARY SHARED SKILL'));
    assert.ok(rendered.includes('Ordinary documentation for ' + f.pack.rows[index].group));
    const evidence = trace.driver_events.find(e => e.event === 'prompt-isolation-checked');
    assert.deepEqual(evidence.shadowed_sections, STUDY_PROMPT_SHADOWS);
    assert.ok(evidence.retained_sections.some(s => s.name === 'matpilot-group-instructions'));
  }
});

test('reintroduced inherited production guidance fails before the model request', async t => {
  const f = fixture(t);
  const result = await runStudy({ ctx: fakeHarness({ reintroduceProductionPrompt: true }).ctx, config: f.config, createUserMessage: input, runIds: ['run0'] });
  assert.equal(result.outcomes[0].status, 'failed');
  assert.equal(result.outcomes[0].api_cny, 0);
  assert.equal(JSON.parse(fs.readFileSync(f.config.ledgerPath)).records.length, 97);
  const stored = JSON.parse(fs.readFileSync(path.join(f.config.outputDir, 'runs/run0/result.json')));
  assert.match(stored.outcome.failure, /Inherited production MCP prompt survived/);
});

test('M instructions intentionally pinned in its research section survive the inherited-section shadow', async t => {
  const f = fixture(t), host = fakeHarness(), d = f.pack.drivers.M;
  const intentional = host.globalPromptSections.get('mcp:matpilot').text;
  const text = 'BEGIN INTENTIONAL M RESEARCH INSTRUCTIONS\n' + intentional + '\nEND INTENTIONAL M RESEARCH INSTRUCTIONS';
  fs.writeFileSync(d.instruction_path, text); d.instruction_sha256 = hash(text);
  const manifest = JSON.parse(fs.readFileSync(d.source_manifest_path));
  manifest.files.find(e => e.path === path.basename(d.instruction_path)).sha256 = d.instruction_sha256;
  const bytes = JSON.stringify(manifest); fs.writeFileSync(d.source_manifest_path, bytes); d.source_manifest_sha256 = hash(bytes);
  fs.writeFileSync(f.config.packPath, JSON.stringify(f.pack));
  const result = await runStudy({ ctx: host.ctx, config: f.config, createUserMessage: input, runIds: ['run2'] });
  assert.equal(result.outcomes[0].status, 'completed');
  const trace = JSON.parse(fs.readFileSync(path.join(f.config.outputDir, 'runs/run2/trace.json')));
  const rendered = trace.events.find(e => e.type === 'system/message').data.message.content[0].text;
  assert.ok(rendered.includes(text));
  assert.equal(rendered.split(intentional).length - 1, 1);
  const evidence = trace.driver_events.find(e => e.event === 'prompt-isolation-checked');
  assert.equal(evidence.retained_sections.find(s => s.name === 'matpilot-group-instructions').text_sha256, hash(text));
});

test('five pinned drivers support rows containing only the three M arms with option binding and scoped prompts', async t => {
  const f = fixture(t), host = fakeHarness();
  const groups = ['M', 'M_without_mapping', 'M_without_evidence'];
  // One CPU-only mock module and one identical instruction file for all M arms.
  // The broker's actual switches have separate Python/CPU integration coverage.
  replaceDriver(f, 'M', `export async function open(context) {
    const group = context.options.implementation_group;
    if (group !== context.run.group) throw new Error('implementation binding changed');
    const flags = { requirement_mapping: group !== 'M_without_mapping', evidence_checks: group !== 'M_without_evidence' };
    context.journal({ event: 'mock-implementation-selected', group, ...flags });
    return { tools: [{ name: 'inspect_fixture', description: 'CPU contract fixture only', parameters: { type: 'object' },
      output: { schema: { type: 'object' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      async execute() { return { implementation: group, ...flags, fixture_id: context.task.fixture_id }; }
    }] };
  }`);
  for (const group of groups.slice(1)) {
    f.pack.drivers[group] = { ...f.pack.drivers.M, configuration_hash: hash(group), options: { implementation_group: group } };
  }
  f.pack.rows = groups.map(group => ({ run_id: 'D01--' + group + '--1', task_id: 'D01', group, repeat: 1,
    mode: 'development', configuration_hash: f.pack.drivers[group].configuration_hash }));
  fs.writeFileSync(f.config.packPath, JSON.stringify(f.pack));
  const loaded = loadRuntimePack(f.config.packPath);
  assert.equal(loaded.drivers.size, 5);
  assert.deepEqual(loaded.pack.rows.map(row => row.group), groups);
  assert.equal(new Set(groups.map(group => loaded.drivers.get(group).instruction_sha256)).size, 1);
  const inheritedBefore = structuredClone([...host.globalPromptSections]);
  const result = await runStudy({ ctx: host.ctx, config: f.config, createUserMessage: input });
  assert.deepEqual(result.outcomes.map(row => row.status), ['completed', 'completed', 'completed']);
  assert.deepEqual([...host.globalPromptSections], inheritedBefore);
  assert.equal(host.creations.length, 3);
  for (const [index, group] of groups.entries()) {
    assert.deepEqual([...host.creations[index].tools.keys()], ['inspect_fixture', 'ask_clarification']);
    assert.deepEqual(host.creations[index].restriction, { allow: [] });
    const trace = JSON.parse(fs.readFileSync(path.join(f.config.outputDir, 'runs', f.pack.rows[index].run_id, 'trace.json')));
    const expected = { implementation: group, requirement_mapping: group !== 'M_without_mapping',
      evidence_checks: group !== 'M_without_evidence', fixture_id: 'fixture01' };
    assert.deepEqual(trace.tools.find(tool => tool.name === 'inspect_fixture').result.value, expected);
    const selected = trace.driver_events.find(event => event.event === 'mock-implementation-selected');
    assert.equal(selected.group, group);
    assert.equal(selected.requirement_mapping, expected.requirement_mapping);
    assert.equal(selected.evidence_checks, expected.evidence_checks);
    const rendered = trace.events.find(event => event.type === 'system/message').data.message.content[0].text;
    assert.ok(!rendered.includes('PRODUCTION MATPILOT'));
    assert.ok(!rendered.includes('PRODUCTION MCP RESOURCE ADVERTISEMENT'));
    assert.ok(rendered.includes('UNRELATED HOST GUIDANCE'));
    assert.ok(rendered.includes('ORDINARY SHARED SKILL'));
    assert.ok(rendered.includes('Ordinary documentation for M'));
    const isolation = trace.driver_events.find(event => event.event === 'prompt-isolation-checked');
    assert.deepEqual(isolation.shadowed_sections, STUDY_PROMPT_SHADOWS);
    assert.equal(isolation.retained_sections.find(section => section.name === 'matpilot-group-instructions').text_sha256,
      f.pack.drivers.M.instruction_sha256);
    assert.equal(trace.provenance.module_sha256, f.pack.drivers[group].module_sha256);
  }
});

test('failed and timed-out runs remain recorded while later rows execute', async t => {
  for (const options of [{ failFirst: true }, { timeoutFirst: true }]) {
    const f = fixture(t, p => { p.shared.timeout_ms = 20; });
    const result = await runStudy({ ctx: fakeHarness(options).ctx, config: f.config, createUserMessage: input });
    assert.equal(result.outcomes[0].status, options.failFirst ? 'failed' : 'timeout');
    assert.equal(result.outcomes[1].status, 'completed');
    const record = JSON.parse(fs.readFileSync(path.join(f.config.outputDir, 'runs/run0/result.json')));
    assert.ok(record.outcome.failure);
  }
});

test('budget stop is a retained outcome; missing usage retains conservative reservation', async t => {
  const f = fixture(t); f.config.batch_guard_cny = 0.01;
  const stopped = await runStudy({ ctx: fakeHarness().ctx, config: f.config, createUserMessage: input });
  assert.equal(stopped.outcomes.length, 1);
  assert.equal(stopped.outcomes[0].status, 'budget_stopped');
  assert.equal(JSON.parse(fs.readFileSync(f.config.ledgerPath)).records.length, 97);
  const g = fixture(t);
  const incomplete = await runStudy({ ctx: fakeHarness({ incompleteUsage: true }).ctx, config: g.config, createUserMessage: input, runIds: ['run0'] });
  assert.equal(incomplete.outcomes[0].status, 'failed');
  const cost = JSON.parse(fs.readFileSync(g.config.ledgerPath)).records.at(-1);
  assert.equal(cost.status, 'reservation_retained');
  assert.equal(cost.accounted_cny, (1048576 * 2 + 100 * 8) / 1e6);
});

test('twenty yuan is a notification threshold, not the old hard limit', t => {
  const root = workspace(t), file = path.join(root, 'budget.json'), notifications = [];
  fs.writeFileSync(file, JSON.stringify({ records: [{ accounted_cny: 19.99 }], limit_cny: 19, user_limit_cny: 20 }));
  const budget = new BudgetLedger(file, prices, 10, event => notifications.push(event));
  try {
    const first = budget.reserve('run0', 'session0');
    budget.settle(first, undefined, undefined, new Error('usage absent'));
    const second = budget.reserve('run1', 'session1');
    assert.ok(second);
    assert.equal(notifications.length, 1);
    assert.equal(notifications[0].threshold_cny, 20);
    assert.equal(notifications[0].reservation_included, true);
  } finally { budget.close(); }
});

test('reject gold leakage, prompt-only groups, unvalidated formal groups and changed files', t => {
  const cases = [
    [p => { p.tasks[0].gold = { answer: 'do not show' }; }, /Answer-bearing/],
    [p => { p.drivers['U+'] = { ...p.drivers.U }; }, /differ only in prompt/],
    [p => { p.mode = 'reviewed_replay'; }, /unvalidated/],
  ];
  for (const [change, message] of cases) {
    const f = fixture(t, change);
    assert.throws(() => loadRuntimePack(f.config.packPath), message);
  }
  const f = fixture(t);
  fs.appendFileSync(f.pack.drivers.M.module_path, '\n// unpinned edit');
  assert.throws(() => loadRuntimePack(f.config.packPath), /Hash mismatch/);
});

test('run output cannot be silently overwritten or history silently reset', async t => {
  const f = fixture(t);
  await runStudy({ ctx: fakeHarness().ctx, config: f.config, createUserMessage: input, runIds: ['run0'] });
  const before = fs.readFileSync(path.join(f.config.outputDir, 'runs/run0/trace.json'));
  await assert.rejects(runStudy({ ctx: fakeHarness().ctx, config: f.config, createUserMessage: input, runIds: ['run0'] }), /EEXIST/);
  assert.deepEqual(fs.readFileSync(path.join(f.config.outputDir, 'runs/run0/trace.json')), before);
  const missing = { ...f.config, ledgerPath: path.join(f.root, 'missing.json') };
  await assert.rejects(runStudy({ ctx: fakeHarness().ctx, config: missing, createUserMessage: input }), /Existing cumulative API ledger/);
});

test('reused planned run IDs keep per-session cost separate while the global ledger accumulates both', async t => {
  const f = fixture(t);
  const first = await runStudy({ ctx: fakeHarness().ctx, config: f.config, createUserMessage: input, runIds: ['run0'] });
  const secondConfig = { ...f.config, outputDir: path.join(f.root, 'second-output') };
  const second = await runStudy({ ctx: fakeHarness().ctx, config: secondConfig, createUserMessage: input, runIds: ['run0'] });
  const perCall = (10 * prices.input_cny_per_million + 4 * prices.output_cny_per_million) / 1e6;
  assert.equal(first.outcomes[0].api_cny, perCall);
  assert.equal(second.outcomes[0].api_cny, perCall);
  const firstRecord = JSON.parse(fs.readFileSync(path.join(f.config.outputDir, 'runs/run0/result.json')));
  const secondRecord = JSON.parse(fs.readFileSync(path.join(secondConfig.outputDir, 'runs/run0/result.json')));
  assert.equal(firstRecord.outcome.api_cny, perCall);
  assert.equal(secondRecord.outcome.api_cny, perCall);
  const ledger = JSON.parse(fs.readFileSync(f.config.ledgerPath));
  const repeated = ledger.records.filter(r => r.run_id === 'run0');
  assert.equal(repeated.length, 2);
  assert.notEqual(repeated[0].session_id, repeated[1].session_id);
  assert.notEqual(repeated[0].id, repeated[1].id);
  assert.deepEqual(ledger.records.slice(0, 97), f.historical);
  assert.ok(Math.abs(ledger.accounted_upper_bound_cny - (3.557394 + 2 * perCall)) < 1e-12);
});

test('clarification never invents answers and stops after its predeclared allowance', async () => {
  const log = [], tool = clarificationTool({ fixed_clarifications: [{ slot: 'elements', answer: 'Na Cl' }] }, e => log.push(e));
  assert.equal((await tool.execute({ slot: 'unknown', question: 'x' })).available, false);
  assert.equal((await tool.execute({ slot: 'elements', question: 'x' })).answer, 'Na Cl');
  assert.equal((await tool.execute({ slot: 'elements', question: 'x' })).available, false);
  assert.equal(log.length, 3);
});

test('Chinese clarification slots are supported without exposing fixed answers in tool schemas', t => {
  const f = fixture(t, p => { p.tasks[0].fixed_clarifications = [{ slot: '请补充材料元素', answer: 'Na Cl' }]; });
  const loaded = loadRuntimePack(f.config.packPath);
  const tool = clarificationTool(loaded.tasks.get('D01'), () => {});
  assert.ok(JSON.stringify(tool.parameters).includes('请补充材料元素'));
  assert.ok(!JSON.stringify(tool.parameters).includes('Na Cl'));
});

test('formal replay requires pinned review and validation files and rechecks content', t => {
  const f = fixture(t), pack = f.pack;
  const write = (name, obj) => { const filename = path.join(f.root, name), data = JSON.stringify(obj); fs.writeFileSync(filename, data); return { filename, sha256: hash(data) }; };
  const manifest = write('fixtures.json', { purpose: 'formal_workflow_study', fixtures: { fixture01: {} } });
  const review = write('fixture-review.json', { manifest_sha256: manifest.sha256, approved: true, independent_of_implementation: true, reviewer: 'fixture test only', materials_qualification: 'not a real attestation', reviewed_at: '2026-10-22', notes: 'unit test', fixture_ids: ['fixture01'] });
  pack.mode = 'reviewed_replay'; pack.shared.replay_fixture_hash = manifest.sha256;
  pack.replay = { manifest_path: manifest.filename, manifest_sha256: manifest.sha256, review_path: review.filename, review_sha256: review.sha256 };
  for (const [group, driver] of Object.entries(pack.drivers)) {
    const report = write('validation-' + group + '.json', { passed: true, configuration_sha256: driver.configuration_hash, checks: ['synthetic report for rejection-gate unit testing, not actual acceptance evidence'], evidence_scope: 'harness_replay_end_to_end' });
    driver.driver_validated = true; driver.replay_fixture_hash = manifest.sha256;
    driver.validation_report_path = report.filename; driver.validation_report_sha256 = report.sha256;
    driver.validation_configuration_sha256 = driver.configuration_hash;
  }
  for (const row of pack.rows) row.mode = 'reviewed_replay';
  fs.writeFileSync(f.config.packPath, JSON.stringify(pack));
  assert.equal(loadRuntimePack(f.config.packPath).pack.mode, 'reviewed_replay');
  fs.appendFileSync(review.filename, ' ');
  assert.throws(() => loadRuntimePack(f.config.packPath), /Hash mismatch/);
});

test('idle alone is not success; all non-completed turn reasons fail', () => {
  assert.equal(terminalStatus([]).status, 'failed');
  for (const kind of ['error', 'aborted', 'max-tokens', 'blocked', 'interrupted', 'forked']) assert.equal(terminalStatus([{ type: 'turn/end', data: { reason: { kind } } }]).status, 'failed');
  assert.equal(terminalStatus([{ type: 'turn/end', data: { reason: { kind: 'completed' } } }]).status, 'completed');
});

test('redaction retains ordinary usage counters and records credential removals', () => {
  const paths = [], value = redact({ inputTokens: 12, authorization: 'secret', nested: { api_key: 'secret', message: 'Bearer abc sk-abcdefghijklmnop' } }, paths);
  assert.equal(value.inputTokens, 12);
  assert.ok(!JSON.stringify(value).includes('secret'));
  assert.equal(paths.length, 3);
});

function replaceDriver(f, group, code) {
  const d = f.pack.drivers[group]; fs.writeFileSync(d.module_path, code); d.module_sha256 = hash(code);
  const source = JSON.parse(fs.readFileSync(d.source_manifest_path));
  source.files.find(x => x.path === path.basename(d.module_path)).sha256 = d.module_sha256;
  const bytes = JSON.stringify(source); fs.writeFileSync(d.source_manifest_path, bytes); d.source_manifest_sha256 = hash(bytes);
  fs.writeFileSync(f.config.packPath, JSON.stringify(f.pack));
}

test('unresponsive open, create, idle and close cannot hang or silently start later rows', async t => {
  for (const phase of ['open', 'create', 'idle', 'close']) {
    const f = fixture(t, p => { p.shared.timeout_ms = phase === 'close' ? 1000 : 25; });
    f.config.cancellation_grace_ms = 10;
    if (phase === 'open') replaceDriver(f, 'U', 'export function open(){return new Promise(()=>{});}');
    if (phase === 'close') {
      const original = fs.readFileSync(f.pack.drivers.U.module_path, 'utf8');
      replaceDriver(f, 'U', original.replace('return {tools:', 'return {close(){return new Promise(()=>{});},tools:'));
    }
    const host = fakeHarness({ stuckCreateFirst: phase === 'create', stuckIdleFirst: phase === 'idle' });
    const result = await runStudy({ ctx: host.ctx, config: f.config, createUserMessage: input });
    assert.equal(result.quarantined, true, phase);
    assert.equal(result.outcomes.length, 1, phase);
    assert.equal(result.outcomes[0].status, phase === 'close' ? 'failed' : 'timeout', phase);
    assert.equal(host.global.has('llm/stream'), true, phase);
    assert.ok(fs.existsSync(f.config.ledgerPath + '.study-lock'), phase);
    if (host.creations[0]) {
      const denied = host.global.get('llm/stream')({ sessionId: host.creations[0].options.sessionId }, async function* () { throw new Error('must never reach provider'); });
      await assert.rejects(denied.next(), /no active run/);
    }
  }
});
