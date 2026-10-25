import { defineTool } from '@deepseek-ai/dsh-tools';
const text = {type:'string'};
const requiredText = {type:'string',required:true};
export function registerDeploymentTools(ctx,manager,onActivate) {
  const add = (name,description,parameters,execute) => ctx.effect(()=>ctx.tools.register(defineTool({
    name,description,parameters,
    output:{schema:{type:'json'},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
    async execute(args) {return execute(args);}
  })),`matpilot deployment ${name}`);
  add('matpilot_check_environment','检查材策/MatterGen所需环境，无需预装Python。首次使用、后端缺失或连接失败时先调用。只读检查，不安装、不调用生成模型。',{mode:{type:'string',enum:['mattergen','demo']},distribution:text},args=>manager.check(args));
  add('matpilot_plan_installation','准备固定版本的环境安装计划。默认真实MatterGen环境；demo须用户明确选择，不得默默降级。把目录、下载和系统前置条件展示给用户。此工具不安装。',{mode:{type:'string',enum:['mattergen','demo']},distribution:text},args=>manager.plan(args));
  add('matpilot_start_installation','用户已授权所展示的安装计划后启动独立安装任务。传入相同plan_id和confirmed_plan_id。固定工具安装隔离Python、依赖及权重，保留既有环境；不生成材料。返回任务号后用status查询，不自由编写其他安装命令。',{plan_id:requiredText,confirmed_plan_id:requiredText},args=>manager.start(args.plan_id,args.confirmed_plan_id));
  add('matpilot_deployment_status','查询持久化安装/验证任务及具体失败步骤。关闭聊天后仍可查询；省略job_id读取最近任务。安装通过不代表真实生成已通过。',{job_id:text},args=>manager.status(args.job_id));
  add('matpilot_resume_installation','恢复已经确认失败或中断的安装/验证任务。复用固定计划、下载缓存和校验，不扩大采样预算，不强杀仍运行的任务。',{job_id:requiredText},args=>manager.resume(args.job_id));
  add('matpilot_verify_environment','复核导入、CUDA、权重及MCP九工具。默认不生成材料；用户另行授权一个GPU候选时才传real_generation=true,confirmed=true，验证独立记录，不能当作材料性质证明。',{job_id:requiredText,real_generation:{type:'boolean'},confirmed:{type:'boolean'}},args=>manager.verify(args.job_id,args));
  add('matpilot_activate_environment','连接已通过MCP验证的托管环境。默认真实计算关闭；用户授权启用真实计算且一候选验证已通过时才能enable_real_compute=true。自动连接，无需复制Python路径。',{job_id:requiredText,enable_real_compute:{type:'boolean'}},async args=>{const result=await manager.activate(args.job_id,args);await onActivate();return result;});
}
