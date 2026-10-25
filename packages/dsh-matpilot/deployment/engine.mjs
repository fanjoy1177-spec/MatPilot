import { mkdir, readFile, writeFile, rename, open, unlink, appendFile } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { probe, targetRun, targetCommand, hostToTargetPath, sanitize } from './platform.mjs';
import { digestFile, download } from './download.mjs';
import { withPackageNetwork } from './network.mjs';

export const DEPLOYMENT_DIR = dirname(fileURLToPath(import.meta.url));
export const DEFAULT_STATE_DIR = process.env.MATPILOT_DEPLOYMENT_STATE || join(homedir(), '.matpilot', 'deployment');
export const hash = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
export async function readJson(path) { return JSON.parse(await readFile(path, 'utf8')); }
export async function atomicJson(path, data) {
  await mkdir(dirname(path), { recursive: true });
  const temp = `${path}.${randomUUID()}.tmp`;
  await writeFile(temp, JSON.stringify(data, null, 2)+'\n', {mode:0o600});
  for (let n=0;;n++) {
    try { await rename(temp, path); return; }
    catch(e) { if(n >= 5 || !['EPERM','EACCES','EBUSY'].includes(e.code)) throw e; await new Promise(r=>setTimeout(r,50*(n+1))); }
  }
}
function identifier(id) { if(!/^[a-f0-9-]{20,80}$/.test(id ?? '')) throw Error('Invalid saved identifier'); return id; }
function alive(pid) { if(!Number.isInteger(pid) || pid < 1) return false; try { process.kill(pid,0); return true; } catch(e) { return e.code === 'EPERM'; } }
export async function loadRecipe() {
  const recipe = await readJson(join(DEPLOYMENT_DIR,'recipe.json'));
  for(const required of ['config.yaml','checkpoints/last.ckpt']) if(!recipe.weights?.some(entry=>entry.relative_path===required)) throw Error(`Incomplete official checkpoint layout: ${required}`);
  for (const type of ['core','mattergen']) if(await digestFile(join(DEPLOYMENT_DIR,`locks/${type}-linux-py310.txt`)) !== recipe[`${type}_lock_sha256`]) throw Error(`Packaged ${type} dependency lock checksum mismatch`);
  if(await digestFile(join(DEPLOYMENT_DIR,'assets',recipe.core_wheel)) !== recipe.core_sha256) throw Error('Packaged MatPilot wheel checksum mismatch');
  if(await digestFile(join(DEPLOYMENT_DIR,'assets',recipe.mattergen_wheel)) !== recipe.mattergen_wheel_sha256) throw Error('Packaged MatterGen wheel checksum mismatch');
  return recipe;
}
export class DeploymentManager {
  constructor({ stateDir = DEFAULT_STATE_DIR, probeFn = probe, launchWorker } = {}) {
    this.stateDir = resolve(stateDir); this.probeFn = probeFn; this.launchWorker = launchWorker;
  }
  async check(options = {}) { return this.probeFn({...options,recipeId:(await loadRecipe()).id}); }
  async plan({ mode = 'mattergen', distribution = 'Ubuntu' } = {}) {
    const recipe = await loadRecipe();
    const inspection = await this.check({mode,distribution});
    if(inspection.blockers.length) return {status:'needs_action', inspection, plan_id:null};
    const payload = {
      schema:1, recipe_hash:hash(recipe), recipe_id:recipe.id, mode, target:inspection.target,
      root:`${inspection.facts.home}/.local/share/matpilot/${recipe.id}-${mode}`,
      created_at:new Date().toISOString(), inspection,
      operations:['download_verified_uv','install_isolated_python','install_locked_core', ...(mode==='mattergen'?['install_locked_mattergen','download_verified_chemical_system_weights']:[]), 'verify_imports_and_mcp','write_disabled_configuration'],
      preserves_existing_environments:true, real_compute_enabled:false,
      downloads:{sources:recipe.sources, weight_bytes:mode==='mattergen'?recipe.weights.reduce((s,x)=>s+x.size,0):0, dependency_bytes:'resolver-specific; see lock files'},
      instructions:'Review the selected target, disk use and operations. Installation needs this exact plan ID. It does not run material generation or modify drivers/WSL. Real verification is a separate one-sample action.'
    };
    const plan_id=hash(payload); await atomicJson(join(this.stateDir,'plans',`${plan_id}.json`),{plan_id,...payload});
    return {status:'planned',plan_id,...payload};
  }
  async savedPlan(id) {
    const plan = await readJson(join(this.stateDir,'plans',`${identifier(id)}.json`));
    const {plan_id,...payload} = plan;
    if(plan_id !== id || hash(payload)!==id || plan.recipe_hash!==hash(await loadRecipe())) throw Error('Saved plan or recipe changed; prepare a new plan.');
    return plan;
  }
  async lock(job) {
    await mkdir(this.stateDir,{recursive:true});
    const path=join(this.stateDir,'installation.lock');
    try { const fd=await open(path,'wx',0o600); await fd.writeFile(JSON.stringify({job_id:job.job_id, launcher_pid:process.pid,created_at:Date.now()})); await fd.close(); }
    catch(e) {
      if(e.code!=='EEXIST') throw e;
      const lock=await readJson(path);
      let owner; try { owner=await this.status(lock.job_id); } catch {}
      if(alive(owner?.pid) || (Date.now()-lock.created_at<30000 && alive(lock.launcher_pid))) throw Error('Another deployment worker is still active. Query its status; do not start a second installer.');
      // Dead worker lock: retain original evidence before reclaiming it.
      await rename(path,`${path}.interrupted-${Date.now()}`); return this.lock(job);
    }
  }
  async release(jobId) {
    const path=join(this.stateDir,'installation.lock');
    try { const l=await readJson(path); if(l.job_id===jobId) await unlink(path); } catch(e) { if(e.code!=='ENOENT') throw e; }
  }
  async launch(job) {
    await this.lock(job);
    await atomicJson(join(this.stateDir,'jobs',`${job.job_id}.json`),job);
    await atomicJson(join(this.stateDir,'latest.json'),{job_id:job.job_id});
    try {
      if(this.launchWorker) await this.launchWorker(job);
      else {
        const child=spawn(process.execPath,[join(DEPLOYMENT_DIR,'worker.mjs'),this.stateDir,job.job_id],{detached:true,windowsHide:true,stdio:'ignore',env:{...process.env,ELECTRON_RUN_AS_NODE:'1'}});
        await new Promise((r,j)=>{child.once('spawn',r);child.once('error',j);}); child.unref();
      }
    } catch(e) { job.status='failed';job.error=sanitize(e.message);await atomicJson(join(this.stateDir,'jobs',`${job.job_id}.json`),job);await this.release(job.job_id);throw e; }
    return {job_id:job.job_id,status:'queued',message:'The independent worker retains progress. Query status; a completed download is not a completed model verification.'};
  }
  async start(plan_id, confirmed_plan_id) {
    if(plan_id!==confirmed_plan_id) throw Error('The reviewed plan ID must match exactly.');
    const plan=await this.savedPlan(plan_id);
    // Idempotence: a repeated start of the same plan returns the existing job.
    try { const latest=await this.status(); if(latest.plan_id===plan_id) return latest; } catch(e) { if(e.code!=='ENOENT') throw e; }
    return this.launch({schema:1,job_id:randomUUID(),plan_id,mode:plan.mode,operation:'install',status:'queued',attempt:1,steps:{},created_at:new Date().toISOString(),human_active_seconds:null,api_calls:0,real_generation_runs:0});
  }
  async status(id) {
    if(!id) id=(await readJson(join(this.stateDir,'latest.json'))).job_id;
    const job=await readJson(join(this.stateDir,'jobs',`${identifier(id)}.json`));
    if(['running','queued'].includes(job.status) && !alive(job.pid) && Date.now()-Date.parse(job.updated_at??job.created_at)>30000) {
      job.status='interrupted'; job.error='Deployment worker is no longer running; resume revalidates completed steps.';
      await atomicJson(join(this.stateDir,'jobs',`${id}.json`),job);
    }
    return job;
  }
  async resume(id) {
    const job=await this.status(id);
    if(!['failed','interrupted'].includes(job.status)) throw Error('Only a confirmed failed/interrupted deployment can resume.');
    await this.savedPlan(job.plan_id);
    job.attempt++;job.status='queued';job.error=null;job.pid=null;job.resumed_at=new Date().toISOString();job.updated_at=job.resumed_at;
    return this.launch(job);
  }
  async verify(id, { real_generation=false, confirmed=false }={}) {
    const original=await this.status(id);const plan=await this.savedPlan(original.plan_id);
    if(!['installed','verified','failed'].includes(original.status) || !original.steps?.configuration?.completed) throw Error('Finish installation before verification.');
    if(real_generation && (plan.mode!=='mattergen' || confirmed!==true)) throw Error('Real verification requires explicit consent for one GPU sample and a MatterGen installation.');
    return this.launch({schema:1,job_id:randomUUID(),plan_id:plan.plan_id,mode:plan.mode,operation:real_generation?'real_verification':'verification',status:'queued',attempt:1,steps:{configuration:original.steps.configuration},created_at:new Date().toISOString(),human_active_seconds:null,api_calls:0,real_generation_runs:0});
  }
  async activate(id, {enable_real_compute=false}={}) {
    const job=await this.status(id);const plan=await this.savedPlan(job.plan_id);
    if(!['installed','verified'].includes(job.status) || !job.verification?.mcp) throw Error('MCP verification must pass before activation.');
    if(enable_real_compute && (!job.verification?.real_generation || plan.mode!=='mattergen')) throw Error('A real one-sample verification is required before enabling real computation.');
    const result=await targetRun(plan.target,'"$1/core/bin/python" "$1/input/verify.py" configure "$1" "$2" "$3"',[plan.root,plan.mode,enable_real_compute?'true':'false']);
    if(result.code) throw Error(result.stderr || result.stdout);
    const args=['env',`MATPILOT_CONFIG=${plan.root}/config.json`,'PYTHONIOENCODING=utf-8',`${plan.root}/core/bin/python`,'-m','matpilot','serve'];
    const [command,argv]=targetCommand(plan.target,args);
    const active={schema:1,job_id:job.job_id,plan_id:plan.plan_id,mode:plan.mode,real_compute_enabled:enable_real_compute,command,args:argv,env:{},root:plan.root,activated_at:new Date().toISOString()};
    await atomicJson(join(this.stateDir,'active.json'),active);return active;
  }
}

export async function executeJob(manager,id,{runner=targetRun,downloader=download,linuxEnv,networkReady=false}={}) {
  if(!networkReady && runner===targetRun) {
    const pending=await readJson(join(manager.stateDir,'jobs',`${identifier(id)}.json`));
    const plan=await manager.savedPlan(pending.plan_id);
    if(pending.operation!=='install') return executeJob(manager,id,{runner,downloader,networkReady:true});
    return withPackageNetwork(plan.target,env=>executeJob(manager,id,{runner,downloader,linuxEnv:env,networkReady:true}));
  }
  const path=join(manager.stateDir,'jobs',`${identifier(id)}.json`);const job=await readJson(path);
  const plan=await manager.savedPlan(job.plan_id), recipe=await loadRecipe();
  const events=join(manager.stateDir,'events',`${id}.jsonl`), logs=join(manager.stateDir,'logs',`${id}.log`);
  await mkdir(dirname(events),{recursive:true});await mkdir(dirname(logs),{recursive:true});
  const event=async(type,data={})=>appendFile(events,JSON.stringify({at:new Date().toISOString(),attempt:job.attempt,type,...data})+'\n');
  const save=async()=>{job.updated_at=new Date().toISOString();await atomicJson(path,job);};
  job.pid=process.pid;job.status='running';job.error=null;await save();await event('worker_started',{operation:job.operation});
  // Serialize heartbeat writes with step state saves; never overwrite a newer state snapshot.
  let saving=Promise.resolve();const persist=()=>saving=saving.then(save);
  const heartbeat=setInterval(()=>{void persist().catch(()=>{});},10000);
  let logging=Promise.resolve();
  const output=s=>{logging=logging.then(()=>appendFile(logs,sanitize(s))).catch(()=>{});};
  const call=async(script,args=[],timeout=60000)=>{
    const r=await runner(plan.target,script,args,{timeout,onOutput:output,linuxEnv});
    if(r.code) throw Error(`Step command failed (${r.code}): ${r.stderr || r.stdout}`); return r.stdout;
  };
  const step=async(name,fn)=>{
    job.current_step=name;job.steps[name]={...job.steps[name],completed:false,started_at:new Date().toISOString()};await persist();await event('step_started',{name});
    const evidence=await fn();job.steps[name]={...job.steps[name],completed:true,finished_at:new Date().toISOString(),evidence:evidence??null};await persist();await event('step_completed',{name});
  };
  try {
    const inspection=await manager.check({mode:plan.mode,distribution:plan.target.distribution,requiredGiB:job.operation==='install' && job.attempt===1 ? undefined : 2});
    if(inspection.blockers.length || inspection.target.kind!==plan.target.kind || `${inspection.facts.home}/.local/share/matpilot/${recipe.id}-${plan.mode}`!==plan.root) throw Error(`Environment changed or is ineligible: ${inspection.blockers.join('; ')}`);
    if(job.operation==='install') {
      const assetDir=await hostToTargetPath(plan.target,DEPLOYMENT_DIR);
      const cache=join(manager.stateDir,'downloads');
      await step('uv_download',()=>downloader(recipe.uv_url,join(cache,`uv-${recipe.uv_version}.tar.gz`),recipe.uv_sha256,output));
      await step('python_download',()=>downloader(recipe.python_url,join(cache,`python-${recipe.python}.tar.gz`),recipe.python_sha256,output));
      const archive=await hostToTargetPath(plan.target,join(cache,`uv-${recipe.uv_version}.tar.gz`));
      const pythonArchive=await hostToTargetPath(plan.target,join(cache,`python-${recipe.python}.tar.gz`));
      await step('bootstrap',()=>call(`set -eu
mkdir -p "$1/input" "$1/tools" "$1/cache"
printf '%s  %s\\n' "$4" "$3" | sha256sum -c -
tar -xzf "$3" -C "$1/tools"
cp "$2/recipe.json" "$2/verify.py" "$1/input/"
cp "$2/locks/core-linux-py310.txt" "$2/locks/mattergen-linux-py310.txt" "$1/input/"
cp "$2/assets/$5" "$2/assets/$6" "$1/input/"
"$1/tools/uv-x86_64-unknown-linux-gnu/uv" --version
`,[plan.root,assetDir,archive,recipe.uv_sha256,recipe.core_wheel,recipe.mattergen_wheel]));
      const prefix=`set -eu
export UV_NO_CONFIG=1 PIP_CONFIG_FILE=/dev/null UV_CACHE_DIR="$1/cache/uv" UV_PYTHON_DOWNLOADS=never UV_HTTP_TIMEOUT=30 UV_HTTP_RETRIES=2
uv="$1/tools/uv-x86_64-unknown-linux-gnu/uv"
`;
      await step('python',()=>call(prefix+'printf \'%s  %s\\n\' "$3" "$2" | sha256sum -c -\ntar -xzf "$2" -C "$1"\n"$uv" venv --python "$1/python/bin/python3" --allow-existing "$1/core"\n',[plan.root,pythonArchive,recipe.python_sha256],600000));
      await step('core',()=>call(prefix+`printf '%s  %s\\n' "$3" "$1/input/core-linux-py310.txt" | sha256sum -c -
"$uv" pip sync --python "$1/core/bin/python" --require-hashes "$1/input/core-linux-py310.txt"
printf '%s  %s\\n' "$4" "$1/input/$2" | sha256sum -c -
"$uv" pip install --python "$1/core/bin/python" --no-deps "$1/input/$2"
"$uv" pip check --python "$1/core/bin/python"
`,[plan.root,recipe.core_wheel,recipe.core_lock_sha256,recipe.core_sha256],3600000));
      if(plan.mode==='mattergen') {
        await step('mattergen',()=>call(prefix+`"$uv" venv --python "$1/python/bin/python3" --allow-existing "$1/mattergen"
printf '%s  %s\\n' "$3" "$1/input/mattergen-linux-py310.txt" | sha256sum -c -
"$uv" pip sync --python "$1/mattergen/bin/python" --require-hashes --find-links https://data.pyg.org/whl/torch-2.2.0+cu121.html "$1/input/mattergen-linux-py310.txt"
printf '%s  %s\\n' "$5" "$1/input/$4" | sha256sum -c -
"$uv" pip install --python "$1/mattergen/bin/python" --no-deps "$1/input/$4"
"$uv" pip check --python "$1/mattergen/bin/python"
`,[plan.root,recipe.python,recipe.mattergen_lock_sha256,recipe.mattergen_wheel,recipe.mattergen_wheel_sha256],7200000));
        await step('weights',async()=>{
          const verified=[];
          for(const entry of recipe.weights) {
            if(!/^[A-Za-z0-9_./-]+$/.test(entry.path) || entry.path.includes('..')) throw Error('Unsafe packaged weight path');
            const destination=join(cache,'weights',recipe.weights_revision,entry.path);
            verified.push(await downloader(entry.url,destination,entry.sha256,output));
            const source=await hostToTargetPath(plan.target,destination);
            const relative=entry.relative_path;
            if(!relative || relative.startsWith('/') || relative.includes('..') || !/^[A-Za-z0-9_./-]+$/.test(relative)) throw Error('Unsafe checkpoint relative path');
            const destinationPath=`${plan.root}/weights/chemical_system/${relative}`;
            await call('set -eu; mkdir -p "$(dirname "$2")"; cp "$1" "$2"; printf \'%s  %s\\n\' "$3" "$2" | sha256sum -c -',[source,destinationPath,entry.sha256]);
          }
          return verified;
        });
      }
      await step('configuration',()=>call('"$1/core/bin/python" "$1/input/verify.py" configure "$1" "$2" false',[plan.root,plan.mode]));
    }
    await step('verification',async()=>{
      const result=await call('"$1/core/bin/python" "$1/input/verify.py" check "$1" "$2" false',[plan.root,plan.mode],180000);
      job.verification=JSON.parse(result.trim().split(/\r?\n/).at(-1));return job.verification;
    });
    if(job.operation==='real_verification') {
      // A retried verification uses the persisted core idempotency key; no extra sampling.
      await step('real_generation',async()=>{
        job.real_generation_runs=1;await persist();
        const result=await call('"$1/core/bin/python" "$1/input/verify.py" smoke "$1" "$2" "$3"',[plan.root,plan.mode,job.job_id],2100000);
        const evidence=JSON.parse(result.trim().split(/\r?\n/).at(-1));job.verification.real_generation=evidence.passed;job.verification.smoke=evidence;return evidence;
      });
    }
    job.status=job.operation==='install'?'installed':'verified';job.finished_at=new Date().toISOString();job.current_step=null;await event('completed',{status:job.status});
  } catch(e) {
    job.status='failed';job.error=sanitize(e.message);job.failed_step=job.current_step;await event('failed',{step:job.current_step,error:job.error});
  } finally {
    clearInterval(heartbeat);await saving;await logging;await save();await manager.release(id);
  }
  return job;
}
