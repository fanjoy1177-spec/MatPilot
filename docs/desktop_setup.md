# DeepSeek Harness 桌面接入

已验证版本：Harness Desktop 0.2.1-rc.2。Python 服务与桌面应用分开安装，账户和 API 密钥由 Harness 管理，MatPilot 配置中不需要填写密钥。

## 安装 Python 服务

下载源码时，可按 README 建立独立 Python 环境并安装。使用发行文件时，下载同版本的三个文件并按 SHA256SUMS.txt 核对：

- matpilot_core-0.2.0.dev8-py3-none-any.whl：Python 服务。
- matpilot_core-0.2.0.dev8.tar.gz：解压后提供 examples 配置、登记 CIF 模板、文档和研究工具；wheel 不含这些文件。
- dsh-matpilot-0.2.0-dev.8.tgz：Harness 命令行/Web 插件包；桌面版按下方内置 MCP 方法配置。

将源码包解压到固定目录，例如 D:/MatPilot，然后在其中建立虚拟环境，并用该环境的 Python 安装下载的 wheel。保留 examples 下的目录结构，不单独移动配置文件。将下方 Python 与 MATPILOT_CONFIG 改为实际绝对路径，并核对配置中的数据和模板路径。

先运行 CLI 演示，确认 backend=demo、real_compute_enabled=false。演示只用固定 NaCl 示例，不加载生成模型、权重或 DFT，也不需要 API 密钥。

## 配置内置 MCP 客户端

先备份用户目录下 `.dsh/profiles/desktop/cordis.patch.yml`。桌面版由应用管理，不使用 `dsh plugin --profile desktop add`。

在现有 YAML 数组中增加下面这一项，并将两处路径改为实际绝对路径。保留已有的账户、界面和模型设置；如果原文件只有注释和 `[]`，用下面的数组内容替换 `[]`。

```yaml
- insert:
    - id: matpilot-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: matpilot
        transport: stdio
        command: 'D:/MatPilot/.venv/Scripts/python.exe'
        args: ['-m', 'matpilot', 'serve']
        env:
          MATPILOT_CONFIG: 'D:/MatPilot/examples/config.demo.json'
          PYTHONIOENCODING: utf-8
        toolCallTimeoutMs: 60000
        failOnStartupError: true
```

当前桌面版会重新加载这份用户配置。先新建对话并输入“查询 MatPilot 能力，告诉我当前后端，先不要执行任务”。确认返回 `backend: demo`、固定 NaCl 示例说明及九个工具后，再测试计划和报告流程。

若修改了 Python 代码，需要重新加载对应 MCP 服务才能使用新代码；仅刷新聊天页面不一定重启后台 Python 进程。不要为加载插件强制关闭有未保存会话的应用。

## 演示步骤

1. 输入：“使用固定 NaCl 示例，2 个候选，每批 1 个。只用 demo 后端，禁止真实计算。先展示方案，等待确认。”
2. 检查计划后，回复：“确认执行这个 demo 方案，请完成查询、结构检查和报告。”
3. 确认报告来源是 `demonstration_fixture`。两个示例不一定是两个独立结构，应查看重复检查结果。

正常使用时由用户决定 API 预算。仓库中的 `benchmarks/harness_acceptance.mjs` 是单独启用的验收模块，其 19 元内部限额只覆盖该模块创建的测试会话，不是整台电脑或所有 Harness 对话的账户级限额。费率固定在验收时核实的价格；以后重跑前应重新核对官方价格。

## 真实计算

真实计算需要另行部署 MatterGen 和权重，并明确切换配置。本轮验收未执行这一步；演示成功不代表服务器、GPU、模型权重或其他生成模型已验收。

## 当前开发版注意事项

当前发行目标为 0.2.0.dev8；具体安装、九工具和固定样本验收以 [dev8验收记录](release_acceptance_v08.md) 为准，真实生成保持禁用。使用 examples/config.multimodel.demo.json 可加载演示模板。热加载首次连接可能超时；等待插件就绪后重试能力查询并确认版本，再提交任务。真实 API 开发对照见 [插件效果记录](study_plugin_effects.md) 和 [消融记录](study_ablation_results.md)，不把固定样本演示等同提速。
