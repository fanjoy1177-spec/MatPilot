// Optional real-host test after a completed deployment; never starts generation.
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import assert from 'node:assert/strict';
const requireHost=createRequire(resolve('packages/dsh-matpilot/package.json'));
const {Context}=await import(pathToFileURL(requireHost.resolve('@deepseek-ai/cordis')).href);
const {default:ToolRuntime}=await import(pathToFileURL(requireHost.resolve('@deepseek-ai/dsh-tools')).href);
const {default:SystemPrompt}=await import(pathToFileURL(requireHost.resolve('@deepseek-ai/dsh-system-prompt')).href);
const {apply}=await import('../../packages/dsh-matpilot/index.mjs');
const ctx=new Context();await ctx.plugin(SystemPrompt);await ctx.plugin(ToolRuntime);
const config=Object.fromEntries(Object.entries({pythonExecutable:'not-installed',configPath:'',pythonPath:'',toolCallTimeoutMs:60000,useManagedEnvironment:true}).map(([k,v])=>[k,{get:()=>v}]));
try {
 await ctx.plugin({inject:['tools'],apply:child=>apply(child,config)});
 const activated=await ctx.tools.get('matpilot_activate_environment').execute({job_id:process.argv[2],enable_real_compute:false},{});
 assert.equal(activated.real_compute_enabled,false);
 const names=['get_capabilities','prepare_plan','submit_job','get_job','control_job','get_results','screen_candidates','compare_runs','build_report'];
 const until=Date.now()+30000;
 while(Date.now()<until && names.some(name=>!ctx.tools.get('mcp__matpilot__'+name)))await new Promise(r=>setTimeout(r,200));
 for(const name of names)assert.ok(ctx.tools.get('mcp__matpilot__'+name),'Missing '+name);
 const result=await ctx.tools.get('mcp__matpilot__get_capabilities').execute({},{signal:AbortSignal.timeout(30000)});
 if(result.isError)throw Error(JSON.stringify(result));
 const caps=JSON.parse((result.content??result.value?.content).find(c=>c.type==='text').text);
 assert.equal(caps.backend,'native');assert.equal(caps.version,'0.2.0.dev8');
 console.log(JSON.stringify({deployment_tools:7,mcp_tools:names.length,managed_connection:true,manual_python_path:false,backend:caps.backend,version:caps.version,real_compute_enabled:activated.real_compute_enabled}));
} finally {await ctx.fiber.dispose();}
