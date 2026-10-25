window.__ModuleLoader__.load({
  id: 'dsh-matpilot',
  factory: (require) => {
    const React = require('react');
    const h = React.createElement;
    const fields = [
      ['pythonExecutable', 'Python 可执行文件', '例如 D:/MatPilot/.venv/Scripts/python.exe'],
      ['configPath', 'MatPilot 配置文件', '例如 D:/MatPilot/examples/config.multimodel.demo.json'],
      ['pythonPath', '独立安装目录（可选）', '普通 pip 安装留空；独立 target 安装填写对应目录'],
      ['toolCallTimeoutMs', '工具超时（毫秒）', '默认 60000，范围 1000–300000'],
    ];
    const box = { padding: 20, border: '1px solid var(--dsw-alias-border-l4, #ddd)', borderRadius: 12, display: 'grid', gap: 14, margin: '16px 0', maxWidth: 760 };
    const inputStyle = { width: '100%', boxSizing: 'border-box', font: 'inherit', padding: '10px 12px', borderRadius: 8, border: '1px solid var(--dsw-alias-border-l3, #bbb)', color: 'var(--dsw-alias-label-primary, #222)', background: 'var(--dsw-alias-bg-layer-1, white)' };
    function values(state) {
      return { pythonExecutable: state.value?.pythonExecutable ?? 'python', configPath: state.value?.configPath ?? '', pythonPath: state.value?.pythonPath ?? '', toolCallTimeoutMs: String(state.value?.toolCallTimeoutMs ?? 60000), useManagedEnvironment: state.value?.useManagedEnvironment ?? true };
    }
    function apply(ctx) {
      const scope = ctx.configForms.get('matpilot');
      function ConnectionForm() {
        const state = React.useSyncExternalStore((listener) => scope.subscribe(listener), () => scope.getSnapshot());
        const [draft, setDraft] = React.useState(null);
        const [revision, setRevision] = React.useState();
        const [busy, setBusy] = React.useState(false);
        const [notice, setNotice] = React.useState('');
        const current = draft ?? values(state);
        const unavailable = state.status !== 'ready';
        const edit = (key, value) => {
          if (draft === null) setRevision(state.revision);
          setDraft({ ...current, [key]: value });
          setNotice('');
        };
        const save = async (event) => {
          event.preventDefault();
          const data = { ...current, pythonExecutable: current.pythonExecutable.trim(), configPath: current.configPath.trim(), pythonPath: current.pythonPath.trim(), toolCallTimeoutMs: Number(current.toolCallTimeoutMs) };
          if (!data.useManagedEnvironment && (!data.pythonExecutable || !data.configPath)) { setNotice('手动连接需要填写 Python 和配置文件路径。'); return; }
          if (data.configPath && !/^(?:[A-Za-z]:[\\/]|\/|\\\\)/.test(data.configPath)) { setNotice('配置文件必须填写绝对路径。'); return; }
          if (!Number.isInteger(data.toolCallTimeoutMs) || data.toolCallTimeoutMs < 1000 || data.toolCallTimeoutMs > 300000) { setNotice('超时必须是 1000–300000 范围内的整数。'); return; }
          setBusy(true); setNotice('');
          try {
            const ok = await scope.mutate(Object.entries(data).map(([key, value]) => ({ op: 'set', path: [key], value })), revision ?? state.revision);
            if (ok) { setDraft(null); setNotice('配置已保存。请在新会话中查询 MatPilot 能力，确认工具已就绪。'); }
            else setNotice('保存未被接受，可能配置已被其他窗口修改。请重新载入后再试。');
          } catch (error) { setNotice('保存失败：' + String(error.message ?? error)); }
          finally { setBusy(false); }
        };
        return h('form', { onSubmit: save, style: box, 'aria-label': 'MatPilot 连接配置' },
          h('h3', { style: { margin: 0 } }, '准备材策运行环境'),
          h('p', { style: { margin: 0, lineHeight: 1.6 } }, '还没有 Python 或 MatterGen 也可以先检查环境。材策会生成安装计划，在独立目录安装，记录失败步骤并支持恢复。账户与 API 密钥继续由 Harness 管理。'),
          h('p', { style: { margin: 0, lineHeight: 1.6 } }, '在对话中输入：“检查材策运行 MatterGen 的环境，并准备安装计划。保留现有环境，先不生成材料。”确认计划后可要求安装、查询进度或恢复失败任务，无需记住工具名。'),
          h('button', { type: 'button', onClick: async () => { try { await navigator.clipboard.writeText('检查材策运行 MatterGen 的环境，并准备安装计划。保留现有环境，先不生成材料。'); setNotice('已复制。请在 Harness 对话中发送，开始只读检查。'); } catch { setNotice('复制失败，请复制上方示例发送到对话。'); } }, style: { ...inputStyle, width: 'auto', cursor: 'pointer' } }, '复制环境检查指令'),
          h('p', { style: { margin: 0, lineHeight: 1.6 } }, '安装完成、工具连接、真实小任务通过会分别报告。真实验证需另行确认一个候选的计算；缺少 GPU 或 WSL 时先说明所需系统步骤。安装进度保留在本机，重新打开对话后可查询。'),
          h('label', null, h('input', {type:'checkbox',checked:current.useManagedEnvironment,disabled:unavailable || !state.writable || busy,onChange:event=>edit('useManagedEnvironment',event.target.checked)}), ' 优先使用已验证并激活的托管环境'),
          h('h3', { style: { margin: 0 } }, '已有环境的手动连接（可选）'),
          unavailable ? h('p', { role: 'status' }, '正在读取连接配置，请稍候或刷新插件页面。') : null,
          ...fields.map(([key, label, hint]) => h('label', { key, style: { display: 'grid', gap: 6 } },
            h('span', null, label),
            h('input', { name: key, 'aria-label': label, type: key === 'toolCallTimeoutMs' ? 'number' : 'text', value: current[key], disabled: unavailable || !state.writable || busy, onChange: (event) => edit(key, event.target.value), style: inputStyle, spellCheck: false, autoComplete: 'off' }),
            h('small', { style: { opacity: 0.7 } }, hint))),
          h('p', { style: { margin: 0, lineHeight: 1.6 } }, '如只需体验固定样本，可明确选择 demo 模式。保存连接参数不会启动安装或材料生成；真实 MatterGen 安装请使用上方对话指令。'),
          h('div', { style: { display: 'flex', gap: 10 } },
            h('button', { type: 'submit', disabled: unavailable || !state.writable || busy || draft === null, style: { ...inputStyle, width: 'auto', background: '#0d705a', color: 'white', cursor: 'pointer' } }, busy ? '保存中…' : '保存连接配置'),
            h('button', { type: 'button', disabled: busy, onClick: () => { setDraft(null); setRevision(undefined); setNotice('已重新读取当前配置。'); }, style: { ...inputStyle, width: 'auto', cursor: 'pointer' } }, '重新载入')),
          notice ? h('p', { role: 'status', style: { margin: 0, lineHeight: 1.6 } }, notice) : null,
          !state.writable && !unavailable ? h('p', null, '当前宿主配置只读。') : null,
          h('p', { style: { margin: 0, lineHeight: 1.6 } }, '开始使用：新建会话，输入“查询 MatPilot 能力，先不要执行任务”。确认后再让 AI 准备计划。'));
      }
      ctx.effect(() => ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({ name: 'plugins.bundle.config', key: 'dsh-matpilot' }, ConnectionForm)), 'matpilot: bundle configuration');
      ctx.effect(() => ctx.slots.inject('plugins.row.config', () => ctx.slots.register({ name: 'plugins.row.config', key: 'dsh-matpilot#matpilot' }, ConnectionForm)), 'matpilot: row configuration');
    }
    return { inject: ['slots', 'configForms'], apply };
  },
});
