import { DeploymentManager, executeJob, readJson, atomicJson } from './engine.mjs';
import {join} from 'node:path';
import {sanitize} from './platform.mjs';
const [stateDir,jobId]=process.argv.slice(2);
try { const result=await executeJob(new DeploymentManager({stateDir}),jobId);process.exitCode=result.status==='failed'?1:0; }
catch(error) {
  try { const path=join(stateDir,'jobs',jobId+'.json');const job=await readJson(path);job.status='failed';job.error=sanitize(error.message);job.updated_at=new Date().toISOString();await atomicJson(path,job);await new DeploymentManager({stateDir}).release(jobId); } catch {}
  console.error(sanitize(error.message));process.exitCode=1;
}
