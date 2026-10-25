import { spawn } from 'node:child_process';
import { homedir, platform } from 'node:os';
import { resolve } from 'node:path';
import { statfs } from 'node:fs/promises';

export function sanitize(text) {
  return String(text).replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, '$1<redacted>@')
    .replace(/((?:token|password|api[_-]?key|authorization)[=: ]+)[^\s,]+/gi, '$1<redacted>');
}
export function run(command, args, { timeout = 30000, onOutput, env = {}, input, captureLimit = 1048576 } = {}) {
  return new Promise((resolveResult, reject) => {
    const child = spawn(command, args, { windowsHide: true, shell: false, env: { ...process.env, ...env }, stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '', err = '', expired = false;
    const append = (chunk, error) => {
      let text = chunk.includes(0) ? chunk.toString('utf16le') : chunk.toString('utf8');
      text = sanitize(text); if (error) err = (err + text).slice(-65536); else out = (out + text).slice(-captureLimit);
      onOutput?.(text);
    };
    child.stdout.on('data', c => append(c, false)); child.stderr.on('data', c => append(c, true));
    const timer = setTimeout(() => { expired = true; child.kill(); }, timeout);
    child.once('error', e => { clearTimeout(timer); reject(e); });
    child.once('close', code => { clearTimeout(timer); resolveResult({ code: expired ? 124 : code, stdout: out, stderr: err }); });
    child.stdin.end(input);
  });
}
export function validateTarget(target) {
  if (!target || !['linux', 'wsl'].includes(target.kind)) throw new Error('Unsupported deployment target');
  if (target.kind === 'wsl' && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(target.distribution)) throw new Error('Invalid WSL distribution name');
  return target;
}
export function targetCommand(target, args) {
  validateTarget(target);
  return target.kind === 'wsl' ? ['wsl.exe', ['-d', target.distribution, '--exec', ...args]] : [args[0], args.slice(1)];
}
export async function targetRun(target, script, args = [], options = {}) {
  // Linux timeout owns the entire command group, including WSL descendants.
  const seconds = Math.ceil((options.timeout ?? 30000) / 1000);
  const vars=Object.entries(options.linuxEnv??{}).map(([key,value])=>{if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)||String(value).includes('\0'))throw Error('Invalid process environment');return `${key}=${value}`;});
  const [command, argv] = targetCommand(target, ['timeout', '--signal=TERM', '--kill-after=10s', `${seconds}s`, 'env', ...vars, '/bin/sh', '-c', script, 'matpilot', ...args]);
  return run(command, argv, { ...options, timeout: (seconds + 20) * 1000 });
}
export async function hostToTargetPath(target, path) {
  const absolute = resolve(path);
  if (target.kind === 'linux') return absolute;
  const [cmd, args] = targetCommand(target, ['wslpath', '-a', '-u', absolute]);
  const result = await run(cmd, args);
  if (result.code || !result.stdout.trim().startsWith('/')) throw new Error('Cannot map package path into WSL; use a local drive.');
  return result.stdout.trim();
}
export async function probe({ distribution = 'Ubuntu', mode = 'mattergen', hostPlatform = platform(), runner = targetRun, requiredGiB, recipeId } = {}) {
  if (!['demo', 'mattergen'].includes(mode)) throw new Error('Unknown installation mode');
  const report = { schema: 1, checked_at: new Date().toISOString(), mode, checks: [], blockers: [], actions: [], installed: false };
  if (!['win32', 'linux'].includes(hostPlatform)) {
    report.blockers.push('This release supports Linux x86_64 and Windows/WSL2.'); return report;
  }
  report.target = validateTarget(hostPlatform === 'win32' ? { kind: 'wsl', distribution } : { kind: 'linux' });
  if (hostPlatform === 'win32' && runner === targetRun) {
    try {
      const listed = await run('wsl.exe', ['--list', '--verbose']);
      const row = listed.stdout.replace(/\u0000/g, '').split(/\r?\n/).find(x => x.trim().replace(/^\*\s*/, '').split(/\s+/)[0] === distribution);
      if (!row || !/\s2\s*$/.test(row)) {
        report.blockers.push('Selected WSL2 distribution is not installed or is WSL1.');
        report.actions.push(`Install WSL2 as administrator, restart if requested, then retry: wsl --install -d ${distribution}`); return report;
      }
    } catch { report.blockers.push('WSL is unavailable.'); report.actions.push('Install WSL2 and Ubuntu, then retry the same check.'); return report; }
  }
  if(recipeId && !/^[A-Za-z0-9._-]+$/.test(recipeId)) throw Error('Invalid recipe identity');
  const script = `printf 'arch=%s\\n' "$(uname -m)"
printf 'kernel=%s\\n' "$(uname -s)"
printf 'home=%s\\n' "$HOME"
printf 'glibc=%s\\n' "$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
printf 'free_kb=%s\\n' "$(df -Pk "$HOME" | awk 'END {print $4}')"
for tool in curl tar sha256sum timeout; do printf 'tool_%s=%s\\n' "$tool" "$(command -v "$tool" || true)"; done
if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader; elif test -x /usr/lib/wsl/lib/nvidia-smi; then /usr/lib/wsl/lib/nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader; fi
if test -n "$1" && test -f "$HOME/.local/share/matpilot/$1-$2/config.json" && test -x "$HOME/.local/share/matpilot/$1-$2/core/bin/python"; then printf 'managed_present=1\\n'; fi
`;
  try {
    const r = await runner(report.target, script, [recipeId??'', mode]);
    if (r.code) { report.blockers.push('Linux preflight could not run. Ensure WSL initialization and coreutils are complete.'); report.detail = r.stderr; return report; }
    const data = Object.fromEntries(r.stdout.split(/\r?\n/).filter(x => /^[a-z_][a-z0-9_]*=/.test(x)).map(x => [x.slice(0, x.indexOf('=')), x.slice(x.indexOf('=') + 1)]));
    report.facts = data;
    report.gpus = r.stdout.split(/\r?\n/).filter(x => /,\s*\d+\s*MiB,/.test(x));
    if (data.arch !== 'x86_64' || data.kernel !== 'Linux') report.blockers.push('Linux x86_64 is required by the pinned wheels.');
    const libc = /^glibc (\d+)\.(\d+)/.exec(data.glibc ?? '');
    if (!libc || (+libc[1] < 2 || (+libc[1] === 2 && +libc[2] < 28))) report.blockers.push('glibc >= 2.28 is required.');
    if (!data.home?.startsWith('/') || /[\r\n\0]/.test(data.home)) report.blockers.push('Cannot determine a safe Linux home directory.');
    for (const tool of ['curl', 'tar', 'sha256sum', 'timeout']) if (!data[`tool_${tool}`]) report.blockers.push(`Missing system prerequisite: ${tool}`);
    report.managed_environment_present = data.managed_present === '1';
    const required = requiredGiB ?? (report.managed_environment_present ? 2 : mode === 'mattergen' ? 12 : 2);
    report.required_free_gib = required;
    if (!Number.isFinite(+data.free_kb) || +data.free_kb < required * 1024 ** 2) report.blockers.push(`At least ${required} GiB free space is required by this installer policy.`);
    if (hostPlatform === 'win32' && runner === targetRun) {
      const disk = await statfs(homedir()); report.host_free_gib = Number(disk.bavail) * Number(disk.bsize) / 1024 ** 3;
      if (report.host_free_gib < required) report.blockers.push(`Windows system drive has less than ${required} GiB free for WSL growth/cache. Move/free storage before installation.`);
    }
    if (mode === 'mattergen' && !report.gpus.length) { report.blockers.push('No NVIDIA GPU visible inside the selected Linux environment.'); report.actions.push('Check the Windows/Linux NVIDIA driver or use a separately provisioned GPU host. Demo is a separate explicit choice.'); }
    report.checks = Object.entries(data).map(([name, value]) => ({ name, value }));
  } catch (e) { report.blockers.push(sanitize(e.message)); }
  report.status = report.blockers.length ? 'needs_action' : 'eligible_for_installation';
  report.note = 'Preflight is not a CUDA allocation or real model inference test. No packages were installed.';
  return report;
}
