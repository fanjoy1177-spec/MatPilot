import { readFile, mkdir, rename, stat } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname } from 'node:path';
import { run } from './platform.mjs';

export async function digestFile(path) {
  const hash = createHash('sha256');
  for await (const part of createReadStream(path)) hash.update(part);
  return hash.digest('hex');
}
export async function systemProxy() {
  const supplied = process.env.HTTPS_PROXY || process.env.https_proxy;
  if (supplied) return supplied;
  if (process.platform !== 'win32') return '';
  try {
    const key = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings';
    const enabled = await run('reg.exe', ['query', key, '/v', 'ProxyEnable']);
    if (!/0x1\b/.test(enabled.stdout)) return '';
    const response = await run('reg.exe', ['query', key, '/v', 'ProxyServer']);
    const value = /REG_SZ\s+([^\r\n]+)/.exec(response.stdout)?.[1]?.trim();
    if (!value) return '';
    const chosen = /(?:^|;)https=([^;]+)/.exec(value)?.[1] ?? (!value.includes('=') ? value : '');
    return chosen ? (chosen.includes('://') ? chosen : `http://${chosen}`) : '';
  } catch { return ''; }
}
export async function curlArgs(url) {
  if (!url.startsWith('https://')) throw new Error('Only HTTPS artifact sources are allowed');
  const proxy = await systemProxy();
  return ['--fail', '--location', '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '20', '--retry', '2', ...(proxy ? ['--proxy', proxy] : []), url];
}
export async function fetchText(url) {
  const r = await run(process.platform === 'win32' ? 'curl.exe' : 'curl', [...await curlArgs(url), '--silent', '--show-error', '--max-time', '120'], { timeout: 130000, captureLimit: 32*1024*1024 });
  if (r.code) throw new Error(`Official source is unreachable: ${new URL(url).hostname}; ${r.stderr}`);
  return r.stdout;
}
export async function download(url, destination, expectedHash, onOutput, {request=run}={}) {
  if (!/^[a-f0-9]{64}$/.test(expectedHash)) throw new Error('An immutable SHA256 is required');
  await mkdir(dirname(destination), {recursive:true});
  try { if (await digestFile(destination) === expectedHash) return { reused: true, sha256: expectedHash }; }
  catch (e) { if (e.code !== 'ENOENT') throw e; }
  const partial = `${destination}.partial`;
  // A TLS close failure can occur after all bytes arrived. Only the pinned hash
  // can establish completion; a completed partial needs no further network call.
  try { if(await digestFile(partial)===expectedHash) {await rename(partial,destination);return {reused:true,sha256:expectedHash,recovered_partial:true};} }
  catch(e) {if(e.code!=='ENOENT') throw e;}
  const cmd = process.platform === 'win32' ? 'curl.exe' : 'curl';
  const args = [...await curlArgs(url), '--silent', '--show-error', '--max-time', '3600', '--output', partial];
  let r = await request(cmd, [...args, '--continue-at', '-'], {timeout:3610000,onOutput});
  // A server rejecting ranges must restart its own partial file, never append a 200 response.
  if ([33, 36].includes(r.code)) r = await request(cmd, args, {timeout:3610000,onOutput});
  if (r.code && await digestFile(partial).catch(()=>null)!==expectedHash) throw new Error(`Download failed (${r.code}) for ${new URL(url).hostname}; partial file kept for resume. ${r.stderr}`);
  const hash = await digestFile(partial);
  if (hash !== expectedHash) {
    await rename(partial, `${partial}.rejected-${Date.now()}`);
    throw new Error('Artifact SHA256 mismatch. Rejected bytes were preserved; they will not be executed. Retry to download again.');
  }
  await rename(partial, destination);
  return { reused: false, sha256: hash, bytes: (await stat(destination)).size, transport_warning:r.code?`curl exit ${r.code}; exact pinned SHA256 matched`:null };
}
