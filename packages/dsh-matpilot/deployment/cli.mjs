import { DeploymentManager } from './engine.mjs';
const [action='check',...rest]=process.argv.slice(2);
const manager=new DeploymentManager({stateDir:process.env.MATPILOT_DEPLOYMENT_STATE});
try {
  let result;
  if(action==='check') result=await manager.check({mode:rest[0]??'mattergen',distribution:rest[1]??'Ubuntu'});
  else if(action==='plan') result=await manager.plan({mode:rest[0]??'mattergen',distribution:rest[1]??'Ubuntu'});
  else if(action==='install') result=await manager.start(rest[0],rest[1]);
  else if(action==='status') result=await manager.status(rest[0]);
  else if(action==='resume') result=await manager.resume(rest[0]);
  else if(action==='verify') result=await manager.verify(rest[0],{real_generation:rest.includes('--one-real-sample'),confirmed:rest.includes('--confirm')});
  else if(action==='activate') result=await manager.activate(rest[0],{enable_real_compute:rest.includes('--enable-real-compute')});
  else throw Error('Commands: check|plan [mattergen|demo] [distribution]; install PLAN_ID CONFIRMED_PLAN_ID; status [JOB_ID]; resume JOB_ID; verify JOB_ID [--one-real-sample --confirm]; activate JOB_ID [--enable-real-compute]');
  console.log(JSON.stringify(result,null,2));
} catch(error) { console.error(JSON.stringify({error:error.message}));process.exitCode=1; }
