// Explicitly started study plugin. Installing it does not make an API request.
// Group modules execute distinct, hash-pinned tools; this is not a prompt comparison.
import fs from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';

export const name = 'matpilot-study';
export const inject = ['agents', 'tools', 'llm', 'systemPrompt'];
const FORMAT = 'matpilot-study-runtime/v1';
const FORBIDDEN = new Set(['gold', 'expected', 'acceptable_actions', 'forbidden_claims', 'required_facts', 'rubric_answers']);
const SECRET_KEY = /^(authorization|proxy-authorization|cookie|set-cookie|api[-_]?key|access[-_]?token|refresh[-_]?token|password|secret|client[-_]?secret)$/i;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.+-]{0,159}$/;
const SHARED_HASHES = ['scientific_capability_hash', 'replay_fixture_hash', 'clarification_policy_hash'];
export const STUDY_PROMPT_SHADOWS = Object.freeze(['mcp:matpilot', 'mcp-resource-servers']);
const now = () => new Date().toISOString();
const sha = data => createHash('sha256').update(data).digest('hex');
const fail = message => { throw new Error(message); };
const positive = (x, label) => Number.isFinite(x) && x > 0 || fail(`${label} must be positive`);
const nonnegative = x => Number.isFinite(x) && x >= 0;
const json = filename => JSON.parse(fs.readFileSync(filename, 'utf8'));

export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}

function assertNoAnswers(value, at = '$') {
  if (!value || typeof value !== 'object') return;
  for (const [key, child] of Object.entries(value)) {
    if (FORBIDDEN.has(key.toLowerCase())) fail(`Answer-bearing field is forbidden in runtime pack: ${at}.${key}`);
    assertNoAnswers(child, `${at}.${key}`);
  }
}

export function redact(value, redactions = [], at = '$') {
  if (Array.isArray(value)) return value.map((v, i) => redact(v, redactions, `${at}[${i}]`));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => {
    if (SECRET_KEY.test(k)) {
      redactions.push({ path: `${at}.${k}`, reason: 'credential field' });
      return [k, '[REDACTED]'];
    }
    return [k, redact(v, redactions, `${at}.${k}`)];
  }));
  if (typeof value === 'string') {
    const cleaned = value.replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [REDACTED]').replace(/\bsk-[A-Za-z0-9_-]{16,}/g, '[REDACTED]');
    if (cleaned !== value) redactions.push({ path: at, reason: 'credential-shaped string' });
    return cleaned;
  }
  return value;
}

function atomicJson(filename, value) {
  const tmp = filename + '.' + randomUUID() + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), { flag: 'wx' });
  fs.renameSync(tmp, filename);
}

function pinnedFile(base, filename, expected, label) {
  if (typeof filename !== 'string' || !/^[a-f0-9]{64}$/.test(expected ?? '')) fail(`Missing pinned ${label}`);
  const absolute = path.resolve(base, filename);
  if (sha(fs.readFileSync(absolute)) !== expected) fail(`Hash mismatch: ${label}`);
  return absolute;
}

/** Validate exporter output and every file before importing executable driver code. */
export function loadRuntimePack(packPath) {
  const absolute = path.resolve(packPath), base = path.dirname(absolute);
  const raw = fs.readFileSync(absolute), pack = JSON.parse(raw);
  assertNoAnswers(pack);
  if (pack.format !== FORMAT || !['development', 'reviewed_replay'].includes(pack.mode) || pack.real_compute_allowed !== false) fail('Invalid runtime pack or real computation enabled');
  if (!/^[a-f0-9]{64}$/.test(pack.provenance?.plan_sha256 ?? '') || !/^[a-f0-9]{64}$/.test(pack.provenance?.suite_sha256 ?? '')) fail('Missing validated plan/suite provenance');
  const shared = pack.shared;
  if (!shared?.provider || !shared.model || typeof shared.reasoning_effort !== 'string') fail('Missing model identity');
  for (const k of ['max_turns', 'max_output_tokens', 'max_requests', 'timeout_ms', 'sampling_cap']) positive(shared[k], k);
  for (const k of SHARED_HASHES) if (!/^[a-f0-9]{64}$/.test(shared[k] ?? '')) fail(`Invalid shared ${k}`);
  const tasks = new Map(), rows = new Set(), drivers = new Map();
  for (const t of pack.tasks ?? []) {
    if (!SAFE_ID.test(t.id) || tasks.has(t.id) || typeof t.prompt !== 'string' || !t.prompt.trim() || !t.fixture_id) fail('Invalid or duplicate task');
    if (!Array.isArray(t.fixed_clarifications)) fail('Fixed clarifications must be explicit slot/answer records');
    const slots = new Set();
    for (const c of t.fixed_clarifications) {
      if (!c || typeof c.slot !== 'string' || !c.slot.trim() || c.slot.length > 500 || slots.has(c.slot) || typeof c.answer !== 'string') fail('Invalid or duplicate clarification slot');
      if (c.max_uses !== undefined && (!Number.isInteger(c.max_uses) || c.max_uses < 1)) fail('Invalid clarification allowance');
      slots.add(c.slot);
    }
    tasks.set(t.id, t);
  }
  for (const [group, d] of Object.entries(pack.drivers ?? {})) {
    if (!SAFE_ID.test(group) || !d.source_revision || !d.configuration_hash) fail('Driver identity is incomplete');
    if (pack.mode === 'reviewed_replay' && d.driver_validated !== true) fail(`Formal driver is unvalidated: ${group}`);
    for (const k of SHARED_HASHES) if (d[k] !== shared[k]) fail(`Unequal science/clarification capability: ${group}.${k}`);
    const modulePath = pinnedFile(base, d.module_path, d.module_sha256, group + ' module');
    const instructionPath = pinnedFile(base, d.instruction_path, d.instruction_sha256, group + ' instructions');
    if (d.capability_sha256 !== d.scientific_capability_hash) fail('Capability document is not the shared capability hash');
    pinnedFile(base, d.capability_path, d.capability_sha256, group + ' capability');
    const sourcePath = pinnedFile(base, d.source_manifest_path, d.source_manifest_sha256, group + ' source manifest');
    const source = json(sourcePath);
    if (source.source_revision !== d.source_revision || !Array.isArray(source.files) || !source.files.length) fail(`No matching pinned implementation files/revision: ${group}`);
    const declared = new Set(), sourceBase = path.dirname(sourcePath);
    for (const f of source.files) {
      const resolved = path.resolve(sourceBase, f.path), relative = path.relative(sourceBase, resolved);
      if (path.isAbsolute(f.path) || relative.startsWith('..' + path.sep) || relative === '..' || path.isAbsolute(relative) || declared.has(resolved)) fail('Source dependency escapes its manifest or is duplicated');
      declared.add(pinnedFile(sourceBase, f.path, f.sha256, group + ' source file'));
    }
    if (!declared.has(modulePath) || !declared.has(instructionPath)) fail('Source manifest must bind both module and instruction files');
    if (pack.mode === 'reviewed_replay') {
      const validationPath = pinnedFile(base, d.validation_report_path, d.validation_report_sha256, group + ' validation report');
      const report = json(validationPath);
      if (report.passed !== true || report.configuration_sha256 !== d.validation_configuration_sha256 || report.evidence_scope !== 'harness_replay_end_to_end' || !Array.isArray(report.checks) || !report.checks.length) fail('Formal driver validation report is incomplete, not a real Harness run, or belongs to another configuration');
    }
    drivers.set(group, { ...d, modulePath, instructions: fs.readFileSync(instructionPath, 'utf8') });
  }
  const implementations = new Set();
  for (const group of ['U', 'U+', 'M']) {
    const d = drivers.get(group);
    if (!d) fail(`Missing comparator group: ${group}`);
    const fingerprint = sha(canonical({ module: d.module_sha256, source: d.source_manifest_sha256, options: d.options ?? {} }));
    if (implementations.has(fingerprint)) fail('Comparator groups differ only in prompt; distinct tool implementations are required');
    implementations.add(fingerprint);
  }
  if (!Array.isArray(pack.rows) || !pack.rows.length) fail('No planned runs');
  for (const row of pack.rows) {
    if (!SAFE_ID.test(row.run_id) || rows.has(row.run_id) || !tasks.has(row.task_id) || !drivers.has(row.group)) fail('Invalid, duplicate, or unresolved run');
    if (row.configuration_hash !== drivers.get(row.group).configuration_hash || row.mode !== pack.mode) fail('Run configuration/mode does not match the export');
    rows.add(row.run_id);
  }
  if (pack.mode === 'reviewed_replay') {
    const replay = pack.replay;
    const manifestPath = pinnedFile(base, replay?.manifest_path, replay?.manifest_sha256, 'scientific replay manifest');
    const reviewPath = pinnedFile(base, replay?.review_path, replay?.review_sha256, 'scientific replay review');
    if (replay.manifest_sha256 !== shared.replay_fixture_hash) fail('Replay manifest is not the shared fixture hash');
    const manifest = json(manifestPath), review = json(reviewPath);
    const ids = Array.isArray(manifest.fixtures) ? manifest.fixtures.map(f => f.id) : Object.keys(manifest.fixtures ?? {});
    if (manifest.purpose !== 'formal_workflow_study' || review.approved !== true || review.independent_of_implementation !== true || review.manifest_sha256 !== replay.manifest_sha256 || ids.some(id => !review.fixture_ids?.includes(id)) || [...tasks.values()].some(t => !ids.includes(t.fixture_id))) fail('Formal replay review or coverage is incomplete');
    for (const key of ['reviewer', 'materials_qualification', 'reviewed_at', 'notes']) if (typeof review[key] !== 'string' || !review[key].trim()) fail('Formal replay review identity is incomplete');
  }
  return { pack, tasks, drivers, packSha256: sha(raw), packPath: absolute };
}

/** Fixed answers are available only through an explicit shared tool, never from gold. */
export function clarificationTool(task, journal) {
  const used = new Map();
  return {
    name: 'ask_clarification',
    description: '向用户请求任务所需的补充信息。仅使用明确的 slot；没有预先提供的答案时，返回信息不可用，不能自行补造。',
    parameters: { type: 'object', properties: { slot: { type: 'string', ...(task.fixed_clarifications.length ? { enum: task.fixed_clarifications.map(c => c.slot) } : {}) }, question: { type: 'string' } }, required: ['slot', 'question'], additionalProperties: false },
    output: { schema: { type: 'object' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute(args) {
      const item = task.fixed_clarifications.find(c => c.slot === args.slot), n = used.get(args.slot) ?? 0;
      const result = item && n < (item.max_uses ?? 1)
        ? { slot: args.slot, available: true, answer: item.answer }
        : { slot: args.slot, available: false, reason: item ? 'fixed reply allowance exhausted' : 'no fixed answer for this slot' };
      if (result.available) used.set(args.slot, n + 1);
      journal({ slot: args.slot, question: args.question, result });
      return result;
    },
  };
}

/** Remove only inherited production MCP prompt contributions in this agent.
 * SystemPrompt.section officially shadows same-name inherited sections. Do not
 * use complete:true, broad text replacement, or mutate the desktop registry:
 * host guidance and pinned group instructions (including intentional M source
 * instructions) must survive unchanged. Tools are independently scope-restricted.
 */
export function isolateStudyPrompt(agentCtx, journal = () => {}) {
  const order = agentCtx.systemPrompt.getSectionOrder('MCP_SERVERS');
  if (!Number.isFinite(order)) fail('Harness MCP_SERVERS section order is unavailable');
  for (const sectionName of STUDY_PROMPT_SHADOWS) agentCtx.systemPrompt.section({ name: sectionName, order, text: '', interpolate: false });
  agentCtx.on('system-prompt/assemble', async (_assembly, _context, next) => {
    const assembly = await next();
    if (!Array.isArray(assembly?.sections)) fail('Harness returned no prompt sections for isolation verification');
    const contaminated = assembly.sections.filter(s => STUDY_PROMPT_SHADOWS.includes(s.name) && s.text !== '');
    if (contaminated.length) fail('Inherited production MCP prompt survived study isolation: ' + contaminated.map(s => s.name).join(', '));
    journal({ event: 'prompt-isolation-checked', policy: 'scoped-shadow/v1', shadowed_sections: [...STUDY_PROMPT_SHADOWS], retained_sections: assembly.sections.filter(s => !STUDY_PROMPT_SHADOWS.includes(s.name)).map(s => ({ name: s.name, text_sha256: sha(s.text) })) });
    return assembly;
  });
}

/** Lock is intentionally not stolen after a crash: an operator must verify quiescence. */
export class BudgetLedger {
  constructor(filename, prices, batchGuardCny, notify = () => {}) {
    for (const k of ['input_cny_per_million', 'output_cny_per_million', 'max_input_tokens', 'max_output_tokens']) positive(prices?.[k], k);
    positive(batchGuardCny, 'batch_guard_cny');
    this.filename = path.resolve(filename); this.prices = prices; this.notify = notify;
    fs.mkdirSync(path.dirname(this.filename), { recursive: true });
    this.lockPath = this.filename + '.study-lock';
    this.lock = fs.openSync(this.lockPath, 'wx');
    fs.writeFileSync(this.lock, JSON.stringify({ pid: process.pid, created_at: now() }));
    try {
      this.ledger = fs.existsSync(this.filename) ? json(this.filename) : { records: [] };
      if (!Array.isArray(this.ledger.records) || this.ledger.records.some(r => !nonnegative(r.accounted_cny))) fail('Invalid existing cost ledger');
      this.startTotal = this.total(); this.batchGuardCny = batchGuardCny;
      this.ledger.notification_threshold_cny = 20;
      this.persist();
    } catch (e) { this.close(); throw e; }
  }
  total() { return this.ledger.records.reduce((s, r) => s + r.accounted_cny, 0); }
  persist() { this.ledger.accounted_upper_bound_cny = this.total(); atomicJson(this.filename, redact(this.ledger)); }
  threshold(record) {
    if (this.total() >= 20 && !this.ledger.study_twenty_cny_notified_at) {
      this.ledger.study_twenty_cny_notified_at = now(); this.persist();
      this.notify({ type: 'api_cost_threshold', threshold_cny: 20, accounted_upper_bound_cny: this.total(), reservation_included: record.status === 'reserved', time: now() });
    }
  }
  reserve(runId, sessionId) {
    const p = this.prices, reservation = (p.max_input_tokens * p.input_cny_per_million + p.max_output_tokens * p.output_cny_per_million) / 1e6;
    if (this.total() - this.startTotal + reservation > this.batchGuardCny + 1e-12) fail('BATCH_BUDGET_STOP: next request exceeds the configured batch guard');
    const record = { id: randomUUID(), run_id: runId, session_id: sessionId, started_at: now(), status: 'reserved', accounted_cny: reservation, pricing: { ...p } };
    this.ledger.records.push(record); this.persist(); this.threshold(record);
    return record;
  }
  settle(record, usage, finish, error) {
    const fields = usage && ['inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens'].map(k => usage[k] ?? 0);
    const kind = typeof finish === 'string' ? finish : finish?.kind;
    if (!error && fields?.every(nonnegative) && usage.inputTokens !== undefined && usage.outputTokens !== undefined && ['stop', 'tool-calls', 'max-tokens'].includes(kind)) {
      const [input, output, read, write] = fields, p = this.prices;
      record.usage = usage;
      record.accounted_cny = ((input + read + write) * p.input_cny_per_million + output * p.output_cny_per_million) / 1e6;
      record.status = 'usage_reported';
    } else record.status = 'reservation_retained';
    record.finish = finish ?? null; record.error = error ? String(error) : null; record.ended_at = now();
    this.persist(); this.threshold(record);
  }
  // A planned run_id can recur in a later pack. The host creates a fresh UUID-
  // bearing session for every execution, so historical retries must not enter
  // this execution's outcome even though they remain in the cumulative ledger.
  cost(sessionId) { return this.ledger.records.filter(r => r.session_id === sessionId).reduce((s, r) => s + r.accounted_cny, 0); }
  close() {
    if (this.lock !== undefined) { fs.closeSync(this.lock); this.lock = undefined; fs.unlinkSync(this.lockPath); }
  }
  quarantine(reason) {
    if (this.lock !== undefined) { fs.closeSync(this.lock); this.lock = undefined; }
    atomicJson(this.lockPath, { pid: process.pid, quarantined_at: now(), reason, instruction: 'Do not remove until this Harness process and its driver subprocesses are confirmed stopped.' });
  }
}

export function terminalStatus(events, flags = {}) {
  const reason = [...events].reverse().find(e => e.type === 'turn/end')?.data?.reason;
  if (flags.budgetStopped) return { status: 'budget_stopped', failure: flags.error ?? 'batch API budget guard reached', turn_end_reason: reason ?? null };
  if (flags.timedOut) return { status: 'timeout', failure: flags.quarantined ? 'run deadline reached and cancellation did not settle; batch quarantined' : 'run wall-clock deadline reached', turn_end_reason: reason ?? null };
  if (flags.error) return { status: 'failed', failure: String(flags.error), turn_end_reason: reason ?? null };
  if (reason?.kind !== 'completed') return { status: 'failed', failure: reason ? `turn ended: ${reason.kind}` : 'missing turn/end event', turn_end_reason: reason ?? null };
  return { status: 'completed', failure: null, turn_end_reason: reason };
}

/** Execute a manually selected batch. The normal plugin invokes this only on start.json. */
export async function runStudy({ ctx, config, createUserMessage, runIds, signal }) {
  const loaded = loadRuntimePack(config.packPath), { pack, tasks, drivers } = loaded;
  const out = path.resolve(config.outputDir); fs.mkdirSync(out, { recursive: true });
  const append = (filename, value) => fs.appendFileSync(filename, JSON.stringify(redact(value)) + '\n');
  const notify = event => { append(path.join(out, 'notifications.jsonl'), event); config.onNotification?.(event); };
  if (config.prices.max_output_tokens !== pack.shared.max_output_tokens) fail('Reservation output ceiling must match the model configuration');
  if (!fs.existsSync(config.ledgerPath)) fail('Existing cumulative API ledger is required; refusing to reset cost history to zero');
  if (runIds !== undefined && (!Array.isArray(runIds) || new Set(runIds).size !== runIds.length || runIds.some(id => !pack.rows.some(r => r.run_id === id)))) fail('Invalid requested run selection');
  const grace = config.cancellation_grace_ms ?? 5000;
  positive(grace, 'cancellation_grace_ms');
  if (grace > 30000) fail('Cancellation grace must not exceed 30 seconds');
  const ledger = new BudgetLedger(config.ledgerPath, config.prices, config.batch_guard_cny, notify);
  const selected = pack.rows.filter(r => !runIds || runIds.includes(r.run_id));
  const prefix = 'matpilot-study-' + randomUUID() + '-';
  let current, stopBatch = false, quarantined = false;
  const settleWithinGrace = (promise, label, state, abortSignal) => new Promise((resolve, reject) => {
    let timer;
    const abort = () => { timer ??= setTimeout(() => {
      quarantined = stopBatch = state.flags.quarantined = true;
      state.flags.error = `Cancellation did not settle: ${label}`;
      abortSignal.removeEventListener('abort', abort);
      reject(new Error(state.flags.error));
    }, grace); };
    const cleanup = () => { clearTimeout(timer); abortSignal.removeEventListener('abort', abort); };
    abortSignal.addEventListener('abort', abort, { once: true });
    if (abortSignal.aborted) abort();
    Promise.resolve(promise).then(value => { cleanup(); resolve(value); }, error => { cleanup(); reject(error); });
  });
  const cancelBatch = () => {
    stopBatch = true;
    if (current) { current.flags.error = 'study controller was disposed or cancelled'; current.cancel?.(); }
  };
  signal?.addEventListener('abort', cancelBatch, { once: true });
  if (signal?.aborted) cancelBatch();
  const removeStream = ctx.on('llm/stream', async function* (options, next) {
    if (!String(options.sessionId ?? '').startsWith(prefix)) { yield* next(); return; }
    const state = current;
    if (!state || state.closed || state.cancelled() || options.sessionId !== state.sessionId) fail('Study request has no active run');
    if (options.purpose || options.provider !== pack.shared.provider || options.model !== pack.shared.model || options.maxTokens !== pack.shared.max_output_tokens) fail('Unapproved auxiliary/model request');
    if (++state.requests > pack.shared.max_requests) { state.flags.error = 'maximum model requests reached'; fail(state.flags.error); }
    let record;
    try { record = ledger.reserve(state.row.run_id, state.sessionId); }
    catch (e) { state.flags.budgetStopped = true; state.flags.error = String(e); stopBatch = true; throw e; }
    state.trace.usage_record_ids.push(record.id); state.persist('request-reserved');
    let usage, finish, error;
    try {
      for await (const chunk of next()) {
        if (chunk.type === 'usage') usage = chunk.usage;
        if (chunk.type === 'finish') finish = chunk.reason;
        yield chunk;
      }
    } catch (e) { error = e; throw e; }
    finally { ledger.settle(record, usage, finish, error); state.persist('request-settled'); }
  });
  const outcomes = [];
  try {
    for (const row of selected) {
      if (stopBatch) break;
      const runDir = path.join(out, 'runs', row.run_id);
      fs.mkdirSync(path.dirname(runDir), { recursive: true });
      // Refuse both completed and crash-orphaned folders. Neither is silently retried.
      fs.mkdirSync(runDir);
      const task = tasks.get(row.task_id), definition = drivers.get(row.group), controller = new AbortController();
      const start = performance.now();
      const identity = Object.fromEntries(['run_id', 'task_id', 'group', 'repeat', 'configuration_hash'].map(k => [k, row[k]]));
      const trace = { format: 'matpilot-study-trace/v1', run_identity: identity, provenance: { ...pack.provenance, runtime_pack_sha256: loaded.packSha256, module_sha256: definition.module_sha256, source_manifest_sha256: definition.source_manifest_sha256, instruction_sha256: definition.instruction_sha256 }, events: [], tools: [], clarifications: [], driver_events: [], usage_record_ids: [], redactions: [], terminal: null };
      const state = current = { row, sessionId: prefix + row.run_id, requests: 0, flags: {}, trace, closed: false, cancelled: () => controller.signal.aborted, persist: stage => {
        const paths = []; const clean = redact(trace, paths); clean.redactions = paths;
        atomicJson(path.join(runDir, 'trace.partial.json'), clean);
        append(path.join(runDir, 'journal.jsonl'), { time: now(), stage });
      } };
      let handle, driver, timeout;
      state.cancel = () => { controller.abort(); handle?.agent.cancel({ kind: 'hook', reason: state.flags.error ?? 'study cancelled' }); };
      try {
        state.persist('allocated');
        timeout = setTimeout(() => { state.flags.timedOut = true; controller.abort(); handle?.agent.cancel({ kind: 'hook', reason: 'study timeout' }); }, pack.shared.timeout_ms);
        // Recheck all pinned files before each run, including resumed batches.
        if (loadRuntimePack(config.packPath).packSha256 !== loaded.packSha256) fail('Runtime pack changed during this batch');
        const mod = await settleWithinGrace(import(pathToFileURL(definition.modulePath).href + '?sha=' + definition.module_sha256), 'driver import', state, controller.signal);
        if (typeof mod.open !== 'function') fail('Group module must export open(context)');
        const openingDriver = mod.open({ run: structuredClone(row), task: structuredClone(task), shared: structuredClone(pack.shared), replay: structuredClone(pack.replay ?? null), options: structuredClone(definition.options ?? {}), artifactDir: path.join(runDir, 'artifacts'), signal: controller.signal, journal: event => { if (!state.closed) { trace.driver_events.push(event); state.persist('driver-event'); } } });
        Promise.resolve(openingDriver).then(late => { if (state.flags.quarantined) void Promise.resolve().then(() => late?.close?.()).catch(() => {}); }, () => {});
        driver = await settleWithinGrace(openingDriver, 'driver open', state, controller.signal);
        if (!Array.isArray(driver?.tools) || !driver.tools.length) fail('Group driver returned no scientific tools');
        const tools = [...driver.tools, clarificationTool(task, event => { trace.clarifications.push(event); state.persist('clarification'); })];
        const names = tools.map(t => t.name);
        if (new Set(names).size !== names.length || names.includes('run_code')) fail('Duplicate or reserved group tool');
        for (const tool of tools) if (!tool.output?.schema || typeof tool.output.render !== 'function' || typeof tool.execute !== 'function') fail('Incomplete Harness tool contract');
        const openingAgent = ctx.agents.create({
          sessionId: state.sessionId, meta: { cwd: config.projectRoot },
          agentOptions: { provider: pack.shared.provider, model: pack.shared.model, reasoningEffort: pack.shared.reasoning_effort, maxTokens: pack.shared.max_output_tokens },
          setup(agentCtx) {
            isolateStudyPrompt(agentCtx, event => { trace.driver_events.push(event); state.persist('prompt-isolation'); });
            agentCtx.tools.restrict({ allow: [] }); // Own registrations remain visible.
            for (const tool of tools) agentCtx.tools.register(tool);
            agentCtx.tools.presentAs('native');
            agentCtx.tools.guard(exec => !names.includes(exec.name) ? 'Tool outside this study group' : controller.signal.aborted ? 'Study run cancelled' : undefined);
            agentCtx.on('tools/pre-execute', async exec => names.includes(exec.name) && !controller.signal.aborted ? { kind: 'allow' } : { kind: 'deny', reason: 'Study scope is closed' });
            agentCtx.on('tools/result', (exec, result) => { if (!state.closed) { trace.tools.push({ time: now(), name: exec.name, arguments: exec.arguments, result }); state.persist('tool-result'); } });
            agentCtx.on('session/event', (_session, event) => {
              if (state.closed) return;
              append(path.join(runDir, 'events.jsonl'), event);
              if (event.type === 'turn/start' && (state.turns = (state.turns ?? 0) + 1) > pack.shared.max_turns) {
                state.flags.error = 'maximum turns reached'; controller.abort(); handle?.agent.cancel({ kind: 'hook', reason: state.flags.error });
              }
            });
            agentCtx.systemPrompt.section({ name: 'matpilot-study-policy', order: 9998, interpolate: false, text: '仅使用当前提供的工具，禁止真实材料计算。保留用户的原始要求；证据不足时说明不足，不得编造结果。需要补充信息时显式调用 ask_clarification；未提供的答案不能猜测。所有回放均只用于流程评估，不能作为材料性能证明。' });
            agentCtx.systemPrompt.section({ name: 'matpilot-group-instructions', order: 9999, interpolate: false, text: definition.instructions });
          },
        });
        Promise.resolve(openingAgent).then(late => { if (state.flags.quarantined) { late.agent.cancel({ kind: 'hook', reason: 'late study creation after cancellation' }); void late.dispose().catch(() => {}); } }, () => {});
        handle = await settleWithinGrace(openingAgent, 'agent create', state, controller.signal);
        if (controller.signal.aborted) fail('Study was cancelled during group setup');
        handle.agent.followup(createUserMessage({ content: [{ type: 'text', text: task.prompt }], source: { kind: 'user' } }));
        await settleWithinGrace(handle.agent.whenIdle(), 'agent idle', state, controller.signal);
      } catch (e) { state.flags.error ??= String(e); }
      finally {
        clearTimeout(timeout); controller.abort();
        if (handle) {
          try { trace.events = handle.agent.session.snapshotEvents(); } catch (e) { state.flags.error ??= 'session snapshot failed: ' + String(e); stopBatch = true; }
          try { await settleWithinGrace(handle.dispose(), 'agent dispose', state, controller.signal); } catch (e) { state.flags.error ??= 'agent disposal failed: ' + String(e); stopBatch = true; }
        }
        try { await settleWithinGrace(driver?.close?.(), 'driver close', state, controller.signal); } catch (e) { state.flags.error ??= 'driver close failed: ' + String(e); stopBatch = true; }
        state.closed = true;
        trace.terminal = terminalStatus(trace.events, state.flags);
        const paths = [], clean = redact(trace, paths); clean.redactions = paths;
        const traceBytes = JSON.stringify(clean, null, 2);
        fs.writeFileSync(path.join(runDir, 'trace.json'), traceBytes, { flag: 'wx' });
        const outcome = { ...trace.terminal, wall_seconds: (performance.now() - start) / 1000, human_active_seconds: null, api_cny: ledger.cost(state.sessionId), science_cny: 0, science_seconds: 0, configuration_hash: row.configuration_hash };
        fs.writeFileSync(path.join(runDir, 'result.json'), JSON.stringify({ row, outcome, trace_sha256: sha(traceBytes), correctness: 'ungraded', accounting: 'conservative usage or retained reservation; real science disabled' }, null, 2), { flag: 'wx' });
        outcomes.push({ run_id: row.run_id, status: outcome.status, api_cny: outcome.api_cny });
        append(path.join(out, 'batch.jsonl'), { time: now(), ...outcomes.at(-1) });
        current = undefined;
      }
    }
  } finally {
    signal?.removeEventListener('abort', cancelBatch);
    if (quarantined) {
      // Retain the stream-denial hook and lock marker until the host is stopped.
      // In-process uncooperative code cannot honestly be claimed terminated.
      ledger.quarantine('Study cancellation did not settle; late model requests remain denied');
      notify({ type: 'study_quarantined', time: now(), reason: 'Unresponsive driver/agent; stop the Harness process before clearing its study lock.' });
    } else { if (typeof removeStream === 'function') removeStream(); ledger.close(); }
  }
  return { outcomes, stopped_early: stopBatch, quarantined, budget_stopped: outcomes.some(o => o.status === 'budget_stopped'), accounted_upper_bound_cny: ledger.total() };
}

export async function apply(ctx, config) {
  const out = path.resolve(config.outputDir); fs.mkdirSync(out, { recursive: true });
  const loaded = loadRuntimePack(config.packPath);
  const { createUserMessage } = await import(pathToFileURL(path.join(config.runtimeRoot, 'dsh-llm/lib/index.js')).href);
  let running = false, disposed = false;
  const controller = new AbortController();
  const tick = async () => {
    const start = path.join(out, 'start.json');
    if (running || disposed || !fs.existsSync(start)) return;
    running = true;
    try {
      const request = json(start);
      if (request.runtime_pack_sha256 !== loaded.packSha256) fail('Start request must name the exact reviewed runtime pack hash');
      fs.renameSync(start, path.join(out, 'started-' + randomUUID() + '.json'));
      await runStudy({ ctx, config, createUserMessage, runIds: request.run_ids, signal: controller.signal });
    } catch (e) { atomicJson(path.join(out, 'controller-error.json'), redact({ time: now(), error: String(e) })); }
    finally { running = false; }
  };
  const timer = setInterval(() => { void tick(); }, 1000);
  ctx.on('dispose', () => { disposed = true; clearInterval(timer); controller.abort(); });
  atomicJson(path.join(out, 'ready.json'), { loaded: true, runtime_pack_sha256: loaded.packSha256, mode: loaded.pack.mode, real_compute_allowed: false, automatic_start: false, cost_notification_cny: 20 });
}
