import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { probe, validateTarget, targetCommand } from '../../packages/dsh-matpilot/deployment/platform.mjs';
import { DeploymentManager, executeJob, atomicJson } from '../../packages/dsh-matpilot/deployment/engine.mjs';
const facts='arch=x86_64\nkernel=Linux\nhome=/tmp/matpilot-test-user\nglibc=glibc 2.35\nfree_kb=40000000\ntool_curl=/usr/bin/curl\ntool_tar=/usr/bin/tar\ntool_sha256sum=/usr/bin/sha256sum\ntool_timeout=/usr/bin/timeout\nGPU fixture, 6144 MiB, 550.0\n';
const goodProbe=()=>probe({hostPlatform:'linux',runner:async()=>({code:0,stdout:facts,stderr:''})});
async function fixture(t,overrides={}) {
  const stateDir=await mkdtemp(join(tmpdir(),'matpilot-deployment-'));
  t.after(()=>rm(stateDir,{recursive:true,force:true}));
  const manager=new DeploymentManager({stateDir,probeFn:goodProbe,launchWorker:async()=>{},...overrides});
  return manager;
}
test('preflight works without any Python and does not run an install',async()=>{
  const calls=[];const result=await probe({hostPlatform:'linux',runner:async(_target,script)=>{calls.push(script);return{code:0,stdout:facts,stderr:''};}});
  assert.equal(result.status,'eligible_for_installation');assert.equal(result.installed,false);
  assert.equal(calls.length,1);assert.ok(!calls[0].includes('pip install'));
});
test('GPU absence is blocked, never silently converted to demo',async()=>{
  const result=await probe({hostPlatform:'linux',runner:async()=>({code:0,stdout:facts.replace(/GPU fixture.*\n/,''),stderr:''})});
  assert.ok(result.blockers.some(x=>x.includes('NVIDIA')));assert.equal(result.mode,'mattergen');
  const demo=await probe({mode:'demo',hostPlatform:'linux',runner:async()=>({code:0,stdout:facts.replace(/GPU fixture.*\n/,''),stderr:''})});
  assert.equal(demo.blockers.length,0);
});
test('unsupported architecture and missing system dependencies are explicit',async()=>{
  const r=await probe({hostPlatform:'linux',runner:async()=>({code:0,stdout:facts.replace('x86_64','aarch64').replace('tool_curl=/usr/bin/curl','tool_curl='),stderr:''})});
  assert.ok(r.blockers.some(x=>x.includes('x86_64')));assert.ok(r.blockers.some(x=>x.includes('curl')));
});
test('distribution names cannot inject shell syntax',()=>{
  assert.throws(()=>validateTarget({kind:'wsl',distribution:'Ubuntu; touch /tmp/pwn'}));
  assert.deepEqual(targetCommand({kind:'wsl',distribution:'Ubuntu-24.04'},['/bin/echo','a b']),['wsl.exe',['-d','Ubuntu-24.04','--exec','/bin/echo','a b']]);
});
test('installation requires matching reviewed plan and rejects altered saved plans',async t=>{
  const manager=await fixture(t);const plan=await manager.plan();
  await assert.rejects(manager.start(plan.plan_id,'wrong'),/match/);
  const path=join(manager.stateDir,'plans',plan.plan_id+'.json');const saved=JSON.parse(await readFile(path));saved.root='/tmp/another-root';await writeFile(path,JSON.stringify(saved));
  await assert.rejects(manager.start(plan.plan_id,plan.plan_id),/changed/);
});
test('plan identifier traversal is rejected',async t=>{
  const manager=await fixture(t);await assert.rejects(manager.savedPlan('../credentials'),/Invalid/);
});
test('active installation lock rejects parallel installation',async t=>{
  const manager=await fixture(t);const plan=await manager.plan();const first=await manager.start(plan.plan_id,plan.plan_id);
  const again=await manager.start(plan.plan_id,plan.plan_id);assert.equal(first.job_id,again.job_id);
  const another=await manager.plan();await assert.rejects(manager.start(another.plan_id,another.plan_id),/still active/);
});
test('failed dependency install keeps evidence and resumes into verification',async t=>{
  const manager=await fixture(t);const plan=await manager.plan();const started=await manager.start(plan.plan_id,plan.plan_id);
  let fail=true;
  const runner=async(_target,script)=>{
    if(script.includes('pip sync')&&fail) return{code:7,stdout:'',stderr:'simulated network interruption'};
    if(script.includes('verify.py" check')) return{code:0,stdout:JSON.stringify({mcp:true,tool_count:9,real_generation:false})+'\n',stderr:''};
    return{code:0,stdout:'fixture step OK',stderr:''};
  };
  const downloader=async()=>({reused:true,fixture:true});
  const failed=await executeJob(manager,started.job_id,{runner,downloader});
  assert.equal(failed.status,'failed');assert.equal(failed.failed_step,'core');assert.equal(failed.steps.bootstrap.completed,true);
  fail=false;await manager.resume(started.job_id);
  const resumed=await executeJob(manager,started.job_id,{runner,downloader});assert.equal(resumed.status,'installed');assert.equal(resumed.attempt,2);
  assert.equal(resumed.real_generation_runs,0);assert.equal(resumed.verification.real_generation,false);
  const events=await readFile(join(manager.stateDir,'events',started.job_id+'.jsonl'),'utf8');assert.ok(events.includes('simulated network interruption'));assert.ok(events.includes('completed'));
});
test('real verification cannot run without separate consent',async t=>{
  const manager=await fixture(t);const plan=await manager.plan();const jobId='12345678-1234-1234-1234-123456789abc';
  await atomicJson(join(manager.stateDir,'jobs',jobId+'.json'),{job_id:jobId,plan_id:plan.plan_id,status:'installed',steps:{configuration:{completed:true}},verification:{mcp:true}});
  await assert.rejects(manager.verify(jobId,{real_generation:true}),/explicit consent/);
  await assert.rejects(manager.activate(jobId,{enable_real_compute:true}),/one-sample/);
});
test('a vanished worker becomes interrupted, never auto-restarted',async t=>{
  const manager=await fixture(t);const jobId='87654321-1234-1234-1234-123456789abc';
  await atomicJson(join(manager.stateDir,'jobs',jobId+'.json'),{job_id:jobId,status:'running',pid:null,created_at:'2020-01-01T00:00:00Z'});
  assert.equal((await manager.status(jobId)).status,'interrupted');
});
