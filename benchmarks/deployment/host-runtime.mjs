// Optional host integration: run in a checkout linked to an installed Harness runtime.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';
const requireHost=createRequire(resolve('packages/dsh-matpilot/package.json'));
const {Context}=await import(pathToFileURL(requireHost.resolve('@deepseek-ai/cordis')).href);
const {default:ToolRuntime}=await import(pathToFileURL(requireHost.resolve('@deepseek-ai/dsh-tools')).href);
const {default:SystemPrompt}=await import(pathToFileURL(requireHost.resolve('@deepseek-ai/dsh-system-prompt')).href);
const {apply}=await import('../../packages/dsh-matpilot/index.mjs');
const ctx=new Context();await ctx.plugin(SystemPrompt);await ctx.plugin(ToolRuntime);
const config=Object.fromEntries(Object.entries({pythonExecutable:'definitely-not-a-python',configPath:'',pythonPath:'',toolCallTimeoutMs:60000,useManagedEnvironment:false}).map(([k,v])=>[k,{get:()=>v}]));
await ctx.plugin({inject:['tools'],apply:child=>apply(child,config)});
const names=['matpilot_check_environment','matpilot_plan_installation','matpilot_start_installation','matpilot_deployment_status','matpilot_resume_installation','matpilot_verify_environment','matpilot_activate_environment'];
for(const name of names) assert.ok(ctx.tools.get(name),`missing ${name}`);
const check=await ctx.tools.get('matpilot_check_environment').execute({mode:'mattergen'},{});
console.log(JSON.stringify({host_tools_registered:names.length,python_preinstalled:false,check},null,2));
await ctx.fiber.dispose();
