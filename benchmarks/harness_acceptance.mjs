// Operator-invoked Harness acceptance plugin; never enabled by the product bundle.
// The caller supplies private paths in a local Harness patch, not in this file.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';

export const name = 'matpilot-acceptance';
export const inject = ['agents', 'tools', 'llm', 'systemPrompt'];

export async function apply(ctx, config) {
  const out = path.resolve(config.outputDir);
  fs.mkdirSync(out, { recursive: true });
  const save = (name, value) => fs.writeFileSync(path.join(out, name), JSON.stringify(value, null, 2));
  const configText = fs.readFileSync(config.backendConfig, 'utf8');
  const settings = JSON.parse(configText);
  if (settings.backend !== 'demo' || settings.command?.[0] !== '__MATPILOT_NO_REAL_COMPUTE__') throw new Error('Acceptance requires the locked demonstration backend');
  const names = ['get_capabilities','prepare_plan','submit_job','get_job','control_job','get_results','screen_candidates','compare_runs','build_report'].map(n => 'mcp__matpilot__' + n);
  const prefix = 'matpilot-acceptance-';
  const ledgerPath = path.join(out, 'budget.json');
  const ledger = fs.existsSync(ledgerPath) ? JSON.parse(fs.readFileSync(ledgerPath, 'utf8')) : {limit_cny:19, user_limit_cny:20, model:'deepseek-flash', price_basis:'peak, all input at cache-miss price', input_cny_per_million:2, output_cny_per_million:8, records:[]};
  // Reserve a full 1,048,576-token context plus the entire capped output before
  // EVERY attempt. Failed or incomplete streams keep that full reservation.
  const reservation = (1048576 * 2 + 2048 * 8) / 1e6;
  let currentCase;
  const persistBudget = () => {
    ledger.accounted_upper_bound_cny = ledger.records.reduce((sum, r) => sum + r.accounted_cny, 0);
    save('budget.json', ledger);
  };
  persistBudget();
  ctx.on('llm/stream', async function* (options, next) {
    if (!String(options.sessionId ?? '').startsWith(prefix)) { yield* next(); return; }
    if (options.purpose) throw new Error('Auxiliary paid calls disabled during acceptance');
    if (options.provider !== 'deepseek-official' || options.model !== 'deepseek-flash' || !(options.maxTokens > 0 && options.maxTokens <= 2048)) throw new Error('Unapproved model or output budget');
    if (currentCase && ++currentCase.attempts > 12) throw new Error('Acceptance case call limit reached');
    if (ledger.records.reduce((sum, r) => sum + r.accounted_cny, 0) + reservation > ledger.limit_cny) throw new Error('MatPilot API spending cap reached');
    const record = {case_id:currentCase?.id, session_id:options.sessionId, started_at:new Date().toISOString(), status:'reserved', accounted_cny:reservation};
    ledger.records.push(record); persistBudget();
    let usage, finish;
    try {
      for await (const chunk of next()) {
        if (chunk.type === 'usage') usage = chunk.usage;
        if (chunk.type === 'finish') finish = chunk.reason;
        yield chunk;
      }
      const fields = usage && ['inputTokens','outputTokens','cacheReadTokens','cacheWriteTokens'].map(k => usage[k] ?? 0);
      const finishKind = typeof finish === 'string' ? finish : finish?.kind;
      if (fields?.every(v => Number.isFinite(v) && v >= 0) && usage.inputTokens !== undefined && usage.outputTokens !== undefined && ['stop','tool-calls','max-tokens'].includes(finishKind)) {
        const [input, output, cacheRead, cacheWrite] = fields;
        record.usage = usage;
        record.accounted_cny = (2 * (input + cacheRead + cacheWrite) + 8 * output) / 1e6;
        record.status = 'usage_reported';
      } else record.status = 'reservation_retained';
      record.finish = finish;
    } finally { record.ended_at = new Date().toISOString(); persistBudget(); }
  });
  const {createUserMessage} = await import(pathToFileURL(path.join(config.runtimeRoot,'dsh-llm/lib/index.js')).href);
  const input = text => createUserMessage({content:[{type:'text',text}],source:{kind:'user'}});
  let running = false;
  let disposed = false;
  let active;
  const cases = JSON.parse(fs.readFileSync(config.casesPath, 'utf8'));
  const resultPath = path.join(out, 'results.json');
  const result = fs.existsSync(resultPath) ? JSON.parse(fs.readFileSync(resultPath, 'utf8')) : {started_at:new Date().toISOString(), backend:'demo', real_compute_allowed:false, host:'DeepSeek Harness', cases:[]};
  const run = async () => {
    if (running || disposed || !fs.existsSync(path.join(out,'start.json'))) return;
    if (!names.every(n => ctx.tools.get(n))) return;
    const requested = JSON.parse(fs.readFileSync(path.join(out,'start.json'),'utf8'));
    fs.renameSync(path.join(out,'start.json'),path.join(out,'started-'+Date.now()+'.json'));
    running = true;
    try {
      for (const c of cases.filter(c => !requested.case_ids || requested.case_ids.includes(c.id))) {
        if (disposed) break;
        currentCase = {id:c.id,expected:c.expected,prompt:c.prompt,attempts:0,tool_calls:[],started_at:new Date().toISOString()};
        result.cases.push(currentCase); save('results.json', result);
        let approvedPlan;
        active = await ctx.agents.create({sessionId:prefix+c.id+'-'+randomUUID(),meta:{cwd:config.projectRoot},agentOptions:{provider:'deepseek-official',model:'deepseek-flash',reasoningEffort:'off',maxTokens:2048},setup(agentCtx){
          agentCtx.tools.restrict({allow:names});
          agentCtx.tools.presentAs('native');
          agentCtx.tools.guard(exec => {
            if (!names.includes(exec.name)) return 'Only MatPilot tools are permitted in this test';
            if (fs.readFileSync(config.backendConfig,'utf8') !== configText) return 'Demonstration configuration changed';
            if (exec.name.endsWith('__submit_job') && (!approvedPlan || exec.arguments?.plan_id !== approvedPlan || exec.arguments?.approved_plan_id !== approvedPlan)) return 'This exact demonstration plan has not been authorized';
          });
          agentCtx.on('tools/pre-execute',async exec => names.includes(exec.name) ? {kind:'allow'} : {kind:'deny',reason:'Acceptance tools only'});
          agentCtx.on('tools/result',(exec,res) => {currentCase.tool_calls.push({name:exec.name,args:exec.arguments,result:res});save('results.json',result);});
          agentCtx.systemPrompt.section({name:'matpilot-acceptance-policy',order:9999,text:'本次为软件验收，仅使用 MatPilot 工具和固定 demo 数据。严格保留用户要求；禁止通过其他工具运行命令或真实计算。先查询能力。不要伪造结果、执行授权或性能证据。用中文简洁回答。',interpolate:false});
        }});
        const agent = active.agent;
        const turn = async text => {
          agent.followup(input(text));
          const timer = setTimeout(()=>agent.cancel({kind:'user'}),150000);
          try { await agent.whenIdle(); } finally { clearTimeout(timer); }
        };
        await turn(c.prompt);
        if (c.followup) {
          const plans = currentCase.tool_calls.filter(t=>t.name.endsWith('__prepare_plan')).map(t=>{
            try { return JSON.parse(t.result.content.find(c=>c.type==='text').text); } catch { return null; }
          }).filter(p=>p?.state==='ready');
          approvedPlan = plans.at(-1)?.plan_id;
          if (approvedPlan) {
            currentCase.approved_demo_plan = approvedPlan;
            await turn('确认仅执行这个固定 NaCl 的 demo 方案，plan_id='+approvedPlan+'。这是演示测试授权。请提交、查询、检查候选并生成报告；严禁真实计算，结果必须标明固定示例来源。');
          } else currentCase.followup_error = 'No ready plan id found';
        }
        currentCase.events = agent.session.snapshotEvents().filter(e=>/message|tool|error/.test(e.type));
        currentCase.ended_at = new Date().toISOString();
        save('results.json',result);
        await active.dispose(); active=undefined;
      }
      result.finished_at=new Date().toISOString(); save('results.json',result);
    } catch(e) { result.error=String(e); save('results.json',result); }
    finally { if(active){await active.dispose();active=undefined;} currentCase=undefined;running=false; }
  };
  const timer = setInterval(()=>run().catch(e=>save('controller-error.json',{error:String(e)})),1000);
  ctx.on('dispose',()=>{disposed=true;clearInterval(timer);active?.agent.cancel({kind:'user'});});
  save('ready.json',{loaded:true,pid:process.pid,registered_tools:names.filter(n=>ctx.tools.get(n)),backend:'demo',budget_limit_cny:ledger.limit_cny});
}
