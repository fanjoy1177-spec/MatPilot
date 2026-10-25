import z from '@deepseek-ai/schemastery';
import * as mcpClient from '@deepseek-ai/dsh-mcp-client';
import { isAbsolute } from 'node:path';

export const name = 'matpilot';
export const Config = z.object({
  pythonExecutable: z.string().default('python').description('已安装 matpilot-core 的 Python 可执行文件').volatile(),
  configPath: z.string().default('').description('MatPilot 配置 JSON 的绝对路径').volatile(),
  pythonPath: z.string().default('').description('可选：独立安装目录 PYTHONPATH').volatile(),
  toolCallTimeoutMs: z.number().step(1).min(1000).max(300000).default(60000).description('工具调用超时，毫秒').volatile(),
});

// Settings exposes volatile fields; saving them reconnects the owned MCP child.
export function apply(ctx, config) {
  let child;
  const connect = async () => {
    const configPath = config.configPath.get().trim();
    if (!configPath) {
      if (child) { await child.dispose(); child = undefined; }
      ctx.logger.info('MatPilot 尚未连接：请在插件详情中配置 Python 与配置文件路径。');
      return;
    }
    if (!isAbsolute(configPath)) throw new Error('MatPilot 配置文件必须使用绝对路径。');
    const command = config.pythonExecutable.get().trim();
    if (!command) throw new Error('Python 可执行文件不能为空。');
    const env = { MATPILOT_CONFIG: configPath, PYTHONIOENCODING: 'utf-8' };
    if (config.pythonPath.get().trim()) env.PYTHONPATH = config.pythonPath.get().trim();
    const next = {
      serverName: 'matpilot', transport: 'stdio', command,
      args: ['-m', 'matpilot', 'serve'], env,
      toolCallTimeoutMs: config.toolCallTimeoutMs.get(), failOnStartupError: true,
    };
    if (child) child.update(next, true);
    else child = ctx.plugin(mcpClient, next);
  };
  let pending = connect();
  ctx.on('loader/volatile-update', () => {
    pending = pending.catch(() => {}).then(connect);
    return pending;
  });
  ctx.inject(['settings'], (scope) => {
    scope.effect(() => scope.settings.configure({ auto: false }, ctx.fiber));
  });
  return pending;
}
