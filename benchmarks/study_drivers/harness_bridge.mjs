// A real local JSONL subprocess bridge. No provider, model weights or network API
// are loaded here. The pinned broker owns each group's actual tool implementation.
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';

const GROUPS = new Set(['U', 'U+', 'M', 'M_without_mapping', 'M_without_evidence']);
const ENV_KEYS = new Set(['PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'TMPDIR', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'LANG', 'LC_ALL', 'SYSTEMDRIVE', 'HOMEDRIVE', 'HOMEPATH']);
const stamp = () => new Date().toISOString();
function error(code, message) { const e = new Error(`${code}: ${message}`); e.code = code; return e; }
function boundedPositive(value, fallback, max, label) {
  const n = value ?? fallback;
  if (!Number.isFinite(n) || n <= 0 || n > max) throw error('INVALID_CONFIG', label);
  return n;
}
function environment() {
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => ENV_KEYS.has(key.toUpperCase())));
  // Deliberately omit API keys, tokens, arbitrary PYTHONPATH and user site hooks.
  return { ...env, PYTHONUNBUFFERED: '1', PYTHONNOUSERSITE: '1', PYTHONIOENCODING: 'utf-8' };
}

export class JsonlBroker {
  static async start({ pythonPath, scriptPath, args = [], signal, journal = () => {}, requestTimeoutMs = 30000, closeTimeoutMs = 1000, maxResponseBytes = 4 * 1024 * 1024 }) {
    if (signal?.aborted) throw error('CANCELLED', 'broker creation was cancelled');
    if (typeof pythonPath !== 'string' || !pythonPath || typeof scriptPath !== 'string' || !fs.statSync(scriptPath).isFile()) throw error('INVALID_CONFIG', 'Python executable and broker script are required');
    boundedPositive(requestTimeoutMs, 30000, 300000, 'request timeout');
    boundedPositive(closeTimeoutMs, 1000, 5000, 'close timeout');
    boundedPositive(maxResponseBytes, 4194304, 16777216, 'response size');
    const broker = new JsonlBroker({ pythonPath, scriptPath: path.resolve(scriptPath), args, signal, journal, requestTimeoutMs, closeTimeoutMs, maxResponseBytes });
    try { await broker.spawned; return broker; }
    catch (e) { await broker.close(); throw e; }
  }

  constructor({ pythonPath, scriptPath, args, signal, journal, requestTimeoutMs, closeTimeoutMs, maxResponseBytes }) {
    this.journal = journal; this.requestTimeoutMs = requestTimeoutMs; this.closeTimeoutMs = closeTimeoutMs;
    this.maxResponseBytes = maxResponseBytes; this.pending = new Map(); this.buffer = ''; this.stderr = '';
    this.closed = false; this.stopping = false; this.sequence = 0; this.signal = signal;
    this.child = spawn(pythonPath, ['-u', scriptPath, ...args], { cwd: path.dirname(scriptPath), stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, shell: false, env: environment() });
    this.spawned = new Promise((resolve, reject) => {
      this.child.once('spawn', () => { this.pid = this.child.pid; this.note('spawn', { pid: this.pid, script_path: scriptPath }); resolve(); });
      this.child.once('error', e => { const problem = error('SPAWN_FAILED', e.message); this.failAll(problem); reject(problem); });
    });
    this.exited = new Promise(resolve => this.child.once('close', (code, signalName) => {
      this.closed = true; this.exit = { code, signal: signalName }; this.signal?.removeEventListener('abort', this.onAbort);
      const problem = this.buffer.trim() ? error('TRUNCATED_RESPONSE', 'broker exited during a JSONL response') : error('BROKER_EXIT', `code=${code}, signal=${signalName}`);
      this.failAll(problem); this.note('exit', { ...this.exit, stderr_tail: this.stderr }); resolve(this.exit);
    }));
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', chunk => this.onData(chunk));
    this.child.stderr.setEncoding('utf8');
    this.child.stderr.on('data', chunk => { this.stderr = (this.stderr + chunk).slice(-8192); this.note('stderr', { text: chunk.slice(0, 8192), truncated: chunk.length > 8192 }); });
    this.child.stdin.on('error', e => this.terminate(error('STDIN_ERROR', e.message)));
    this.onAbort = () => this.terminate(error('CANCELLED', 'owning study run was cancelled'));
    signal?.addEventListener('abort', this.onAbort, { once: true });
    if (signal?.aborted) this.onAbort();
  }

  note(event, data = {}) {
    try { this.journal({ component: 'python-jsonl-bridge', time: stamp(), event, ...data }); }
    catch (e) { this.recordingError = e; this.terminate(error('JOURNAL_FAILED', 'cannot preserve broker evidence'), false); }
  }
  failAll(reason) {
    for (const request of this.pending.values()) { clearTimeout(request.timer); request.signal?.removeEventListener('abort', request.abort); request.reject(reason); }
    this.pending.clear();
  }
  terminate(reason, record = true) {
    this.stopping = true; this.failure ??= reason; this.failAll(reason);
    if (record) this.note('terminated', { reason: String(reason), pid: this.pid ?? null });
    // Windows venv python.exe can be a redirector with a separate worker PID.
    // Kill only the tree rooted at this instance's still-live ChildProcess.
    // Never select processes by image name or accept an external target PID.
    if (this.child.pid && this.child.exitCode === null && this.child.signalCode === null && !this.killPromise) {
      if (process.platform === 'win32') {
        const taskkill = path.join(process.env.SystemRoot ?? process.env.WINDIR ?? 'C:\\Windows', 'System32', 'taskkill.exe');
        this.killPromise = new Promise(resolve => {
          const killer = spawn(taskkill, ['/PID', String(this.child.pid), '/T', '/F'], { shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: environment() });
          let detail = '';
          killer.stdout.on('data', chunk => { detail = (detail + chunk).slice(-2048); });
          killer.stderr.on('data', chunk => { detail = (detail + chunk).slice(-2048); });
          killer.once('error', e => { this.note('tree-kill-error', { owned_root_pid: this.child.pid, error: String(e) }); resolve(); });
          killer.once('close', code => { this.note('tree-kill', { owned_root_pid: this.child.pid, code, detail }); resolve(); });
        });
      } else { this.child.kill('SIGKILL'); this.killPromise = Promise.resolve(); }
    }
  }
  onData(chunk) {
    if (this.closed || this.stopping) return;
    this.buffer += chunk;
    if (Buffer.byteLength(this.buffer, 'utf8') > this.maxResponseBytes) { this.terminate(error('RESPONSE_TOO_LARGE', 'JSONL response exceeded its limit')); return; }
    let newline;
    while ((newline = this.buffer.indexOf('\n')) !== -1) {
      const line = this.buffer.slice(0, newline).replace(/\r$/, ''); this.buffer = this.buffer.slice(newline + 1);
      if (!line.trim()) continue;
      let response;
      try { response = JSON.parse(line); } catch { this.terminate(error('INVALID_JSONL', 'broker stdout contained non-JSON output')); return; }
      const request = response && this.pending.get(response.id);
      if (!request || Object.hasOwn(response, 'result') === Object.hasOwn(response, 'error') || (Object.hasOwn(response, 'error') && (!response.error || typeof response.error !== 'object'))) { this.terminate(error('INVALID_RESPONSE', 'unknown request id or ambiguous response envelope')); return; }
      clearTimeout(request.timer); request.signal?.removeEventListener('abort', request.abort); this.pending.delete(response.id);
      this.note('response', { id: response.id, method: request.method, response });
      if (this.recordingError) { request.reject(error('JOURNAL_FAILED', 'cannot preserve broker response')); return; }
      if (response.error) request.reject(error(String(response.error.code ?? 'BROKER_TOOL_ERROR'), String(response.error.message ?? 'broker tool failed')));
      else request.resolve(response.result);
    }
  }
  request(method, arguments_ = {}, signal) {
    if (this.closed || this.stopping || this.recordingError) return Promise.reject(this.failure ?? error('BROKER_CLOSED', 'broker no longer accepts requests'));
    if (signal?.aborted) { this.terminate(error('CANCELLED', 'tool request was cancelled')); return Promise.reject(this.failure); }
    if (typeof method !== 'string' || !method) return Promise.reject(error('INVALID_METHOD', 'method must be a nonempty string'));
    const id = `${++this.sequence}-${randomUUID()}`;
    return new Promise((resolve, reject) => {
      const abort = () => this.terminate(error('CANCELLED', `tool request ${method} was cancelled`));
      const timer = setTimeout(() => this.terminate(error('BROKER_TIMEOUT', `${method} exceeded ${this.requestTimeoutMs} ms`)), this.requestTimeoutMs);
      this.pending.set(id, { method, resolve, reject, timer, signal, abort });
      signal?.addEventListener('abort', abort, { once: true });
      this.note('request', { id, method, arguments: arguments_ });
      if (this.stopping) return;
      let line;
      try { line = JSON.stringify({ id, method, arguments: arguments_ }) + '\n'; }
      catch (e) { this.terminate(error('INVALID_ARGUMENTS', e.message)); return; }
      try { this.child.stdin.write(line, e => { if (e) this.terminate(error('STDIN_WRITE_FAILED', e.message)); }); }
      catch (e) { this.terminate(error('STDIN_WRITE_FAILED', e.message)); }
    });
  }
  async close() {
    if (this.closed) return this.exit;
    this.stopping = true;
    this.failAll(error('BROKER_CLOSED', 'owning driver is closing'));
    this.child.stdin.end();
    let timer;
    const deadline = new Promise(resolve => { timer = setTimeout(() => resolve(null), this.closeTimeoutMs); });
    const graceful = await Promise.race([this.exited, deadline]); clearTimeout(timer);
    if (graceful) return graceful;
    this.terminate(error('BROKER_FORCE_CLOSE', 'broker did not exit after stdin closed'));
    let killTimer;
    const killed = await Promise.race([this.exited, new Promise((_, reject) => { killTimer = setTimeout(() => reject(error('BROKER_UNRESPONSIVE', 'spawned broker did not terminate after kill')), 2000); })]).finally(() => clearTimeout(killTimer));
    return killed;
  }
}

export async function open({ run, task, shared, replay, options = {}, artifactDir, signal, journal = () => {} }) {
  if (!GROUPS.has(run?.group) || !task?.fixture_id || !run.run_id) throw error('INVALID_CONFIG', 'group, fixture and run identity are required');
  const implementationGroup = options.implementation_group;
  if (!GROUPS.has(implementationGroup) || implementationGroup !== run.group) throw error('IMPLEMENTATION_GROUP_MISMATCH', 'pinned implementation_group must exactly match the planned group');
  const manifest = replay?.manifest_path ?? options.fixture_manifest_path;
  if (!manifest || !fs.statSync(manifest).isFile()) throw error('INVALID_CONFIG', 'pinned fixture manifest is unavailable');
  const cap = boundedPositive(shared?.sampling_cap, undefined, 100000, 'sampling cap');
  if (!Number.isInteger(cap)) throw error('INVALID_CONFIG', 'sampling cap must be an integer');
  const guidance = boundedPositive(shared?.scientific_defaults?.guidance_scale ?? shared?.guidance_scale, 2, 1000, 'guidance scale');
  const step = boundedPositive(shared?.scientific_defaults?.step_lr, 1e-5, 1, 'step learning rate');
  const args = ['--group', implementationGroup, '--manifest', path.resolve(manifest), '--fixture-id', task.fixture_id, '--run-id', run.run_id, '--output-dir', path.resolve(artifactDir), '--sampling-cap', String(cap), '--guidance-scale', String(guidance), '--step-lr', String(step)];
  if (run.mode === 'reviewed_replay') args.push('--formal');
  if (options.template_config) args.push('--template-config', path.resolve(options.template_config));
  if (replay?.review_path) args.push('--review', path.resolve(replay.review_path));
  if (replay?.manifest_sha256) args.push('--expected-manifest-sha256', replay.manifest_sha256);
  if (replay?.review_sha256) args.push('--expected-review-sha256', replay.review_sha256);
  const broker = await JsonlBroker.start({ pythonPath: options.python_path, scriptPath: options.broker_path, args, signal, journal, requestTimeoutMs: options.request_timeout_ms ?? 30000, closeTimeoutMs: options.close_timeout_ms ?? 1000 });
  try {
    const description = await broker.request('describe_tools', {}, signal);
    if (description?.identity?.group !== run.group || description?.identity?.real_compute_allowed !== false) throw error('IDENTITY_MISMATCH', 'broker group or real-compute boundary is incorrect');
    if (!Array.isArray(description.tools) || !description.tools.length) throw error('INVALID_TOOLS', 'broker did not describe its tools');
    const names = new Set();
    const tools = description.tools.map(schema => {
      if (typeof schema.name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_-]{0,127}$/.test(schema.name) || ['run_code', 'ask_clarification', 'describe_tools', 'sample'].includes(schema.name) || names.has(schema.name) || typeof schema.description !== 'string' || !schema.parameters || typeof schema.parameters !== 'object' || Array.isArray(schema.parameters)) throw error('INVALID_TOOLS', 'duplicate, reserved, or malformed tool schema');
      names.add(schema.name);
      return {
        name: schema.name, description: schema.description, parameters: structuredClone(schema.parameters),
        output: { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        timeoutMs: options.request_timeout_ms ?? 30000,
        async execute(args, exec) { return broker.request(schema.name, args, exec.signal); },
      };
    });
    journal({ component: 'python-jsonl-bridge', event: 'identity-verified', time: stamp(), identity: description.identity, implementation_group: implementationGroup, launched_root_pid: broker.pid, tool_names: [...names], scientific_defaults: { guidance_scale: guidance, step_lr: step } });
    return { tools, identity: description.identity, close: () => broker.close() };
  } catch (e) { await broker.close(); throw e; }
}
