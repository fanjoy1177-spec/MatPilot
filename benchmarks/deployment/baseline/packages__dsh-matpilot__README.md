# 材策 MatPilot

通过现有 Harness 对话完成需求核对、任务跟踪、候选筛选与报告整理。

本包 0.2.0-dev.10 增加桌面原生插件卡片、启用开关和连接配置表单，连接已发行的 matpilot-core 0.2.0.dev8。Python 材料计算后端未在本次修改。

## 在桌面插件页安装

1. 安装 matpilot-core，并解压同版源码包保留 examples 与模板。
2. 在 Harness 的“插件 → 添加插件”输入本插件目录的绝对路径，或下载的 tgz 文件路径；安装后启用。
3. 打开“材策 MatPilot”，在连接表单填写 Python 可执行文件与 MatPilot 配置文件的绝对路径。常规 pip 安装的“独立安装目录”留空；target 安装才填写 PYTHONPATH 目录。
4. 保存连接配置，然后新建会话，输入“查询 MatPilot 能力，先不要执行任务”。首次使用选择 examples/config.multimodel.demo.json，确认 demo 后端及真实计算关闭。

主开关停用此插件和它注册的 MCP 工具，重新启用会重新连接。停用工具入口并不等同取消已经提交的独立任务；停用前应查询或停止任务。

## 旧配置迁移

若以前手动配置过 id=matpilot-mcp 的 MCP 插件，应备份并移除该旧条目，把它的 Python 路径、MATPILOT_CONFIG、可选 PYTHONPATH 转入本插件的连接配置。两套入口不能同时占用 mcp__matpilot__ 工具名。

不会自动运行材料模型、下载权重或发送 API 请求。Python 服务及真实模型环境仍须单独安装。当前演示不能证明材料性能或整体提速；本包尚未发布到 npm。
