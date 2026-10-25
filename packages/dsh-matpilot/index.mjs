import z from '@deepseek-ai/schemastery';
import * as mcpClient from '@deepseek-ai/dsh-mcp-client';
import { isAbsolute } from 'node:path';
import { join } from 'node:path';
import { DeploymentManager, DEFAULT_STATE_DIR, readJson } from './deployment/engine.mjs';
import { registerDeploymentTools } from './deployment/tools.mjs';

export const name = 'matpilot';
export const inject = ['tools'];
export const Config = z.object({
  pythonExecutable: z.string().default('python').description('已安装 matpilot-core 的 Python 可执行文件').volatile(),
  configPath: z.string().default('').description('MatPilot 配置 JSON 的绝对路径').volatile(),
  pythonPath: z.string().default('').description('可选：独立安装目录 PYTHONPATH').volatile(),
  toolCallTimeoutMs: z.number().step(1).min(1000).max(300000).default(60000).description('工具调用超时，毫秒').volatile(),
  useManagedEnvironment: z.boolean().default(true).description('优先连接经过部署工具验证并激活的环境').volatile(),
});

// Settings exposes volatile fields; saving them reconnects the owned MCP child.
export function apply(ctx, config) {
  let child;
  const manager = new DeploymentManager();
  const connect = async () => {
    if (config.useManagedEnvironment.get()) {
      let active;
      try { active = await readJson(join(DEFAULT_STATE_DIR,'active.json')); }
      catch(error) { if(error.code !== 'ENOENT') ctx.logger.warn('无法读取托管环境：'+error.message); }
      if(active) {
        const next = {serverName:'matpilot',transport:'stdio',command:active.command,args:active.args,env:active.env,toolCallTimeoutMs:config.toolCallTimeoutMs.get(),failOnStartupError:false};
        if(child) child.update(next,true); else child=ctx.plugin(mcpClient,next);
        return;
      }
    }
    const configPath = config.configPath.get().trim();
    if (!configPath) {
      if (child) { await child.dispose(); child = undefined; }
      ctx.logger.info('材策部署工具已就绪。首次使用请检查环境并准备安装计划；已有环境也可手动连接。');
      return;
    }
    if (!isAbsolute(configPath)) { ctx.logger.warn('MatPilot 配置文件必须使用绝对路径；部署检查工具仍可用。'); return; }
    const command = config.pythonExecutable.get().trim();
    if (!command) { ctx.logger.warn('Python 尚未配置；可使用部署检查工具。'); return; }
    const env = { MATPILOT_CONFIG: configPath, PYTHONIOENCODING: 'utf-8' };
    if (config.pythonPath.get().trim()) env.PYTHONPATH = config.pythonPath.get().trim();
    const next = {
      serverName: 'matpilot', transport: 'stdio', command,
      args: ['-m', 'matpilot', 'serve'], env,
      toolCallTimeoutMs: config.toolCallTimeoutMs.get(), failOnStartupError: false,
    };
    if (child) child.update(next, true);
    else child = ctx.plugin(mcpClient, next);
  };
  let pending = connect();
  registerDeploymentTools(ctx, manager, connect);
  ctx.on('loader/volatile-update', () => {
    pending = pending.catch(() => {}).then(connect);
    return pending;
  });
  ctx.inject(['settings'], (scope) => {
    scope.effect(() => scope.settings.configure({ auto: false }, ctx.fiber));
  });
  return pending;
}
