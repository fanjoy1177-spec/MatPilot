# DeepSeek Harness 桌面接入

**dev11 首次安装入口已改为插件内环境部署**，无 Python/MatterGen 时也能检查和安装。请优先阅读[环境部署指南](deployment.md)。下文保留 dev10 的已有环境手动接入方式和历史验收范围；其未自动部署的描述仅适用于旧版。原文快照另存于 `benchmarks/deployment/baseline/`。

适用：Harness Desktop 0.2.1-rc.2，MatPilot 原生连接包 0.2.0-dev.10，Python 服务 0.2.0.dev8。账户和 API 密钥由 Harness 管理，连接表单不填写密钥。

## 1. 安装 Python 服务

源码用户按 README 建立独立环境并安装。发行文件用户下载 [dev8 Python 发行文件](../releases/0.2.0.dev8/)，按 SHA256SUMS.txt 核对：

- matpilot_core-0.2.0.dev8-py3-none-any.whl：Python 服务。
- matpilot_core-0.2.0.dev8.tar.gz：解压后提供 examples、CIF 模板与文档；wheel 不含这些文件。

解压到固定目录，例如 D:/MatPilot，保持 examples 与模板相对位置。用该目录的虚拟环境 Python 安装 wheel。先运行 CLI demo，确认 backend=demo、real_compute_enabled=false。固定样本演示不需要 GPU 或 API。

## 2. 在“插件”页面安装

下载 [dsh-matpilot-0.2.0-dev.10.tgz](../releases/harness/0.2.0-dev.10/dsh-matpilot-0.2.0-dev.10.tgz)，按同目录 SHA256SUMS.txt 核对。

1. 打开 Harness 左侧“插件”，点击右上角“添加插件”。
2. 输入本地 tgz 文件的绝对路径，点击“安装”，完成后“立即启用”。也可输入源码中 packages/dsh-matpilot 的绝对目录。
3. 向下滚动到“已安装插件”，找到“材策 MatPilot”。右侧开关控制插件启停，点击名称打开详情。
4. 填写连接配置并保存：

| 字段 | 示例/说明 |
|---|---|
| Python 可执行文件 | D:/MatPilot/.venv/Scripts/python.exe；该环境应已安装 matpilot-core |
| MatPilot 配置文件 | D:/MatPilot/examples/config.multimodel.demo.json；必须是绝对路径 |
| 独立安装目录 | 普通 pip 安装留空；pip --target 安装时填写对应目录 |
| 工具超时（毫秒） | 默认 60000，支持 1000–300000 的整数 |

配置保存到当前 Harness profile，保存后自动重连工具服务。未配置时仍能安装并打开设置；完成连接后才能调用九个工具。界面显示“配置已保存”只表示设置持久化，实际连接须用下一步能力查询确认。

桌面 profile 由 Electron 管理，不能使用 dsh plugin --profile desktop add。插件包包含界面与连接器，Python 服务和材料模型须分别安装；不能仅安装 tgz 就运行材料模型。

## 3. 核验并开始对话

新建会话，输入：“查询 MatPilot 能力，告诉我当前后端，先不要执行任务”。应确认 backend=demo、Python 版本 0.2.0.dev8、真实计算关闭，以及九个工具。Harness 的大模型对话按账户产生 API 费用；插件安装和保存连接设置本身不发送模型请求。

固定演示：

1. “使用固定 NaCl 示例，2 个候选，每批 1 个。只用 demo 后端，禁止真实计算。先展示方案，等待确认。”
2. 检查方案后：“确认执行这个 demo 方案，请完成查询、结构检查和报告。”
3. 报告应标明 demonstration_fixture；两个文件经检查为一个独立结构，不能当作两个材料发现。

关闭主开关会卸载 MatPilot 的九个工具；重新开启会重新连接。关闭工具入口不会取消已提交的独立任务，需先用 control_job 查询或停止。更换 Python 后端代码后可停用再启用插件；单纯刷新聊天页不保证后台进程更新。

## 旧版桌面配置迁移

旧教程直接在 ~/.dsh/profiles/desktop/cordis.patch.yml 插入 id=matpilot-mcp 的内置 MCP 客户端，工具可用但没有 MatPilot 原生插件卡片。

先备份该文件，将旧条目 command、env.MATPILOT_CONFIG、可选 env.PYTHONPATH 分别记录为表单三项路径。仅移除对应 matpilot-mcp 插入项，保留其他账户/界面设置，再安装并配置原生包。同名 MCP 入口不能同时保留，否则可能重复注册工具。原生包的配置条目为 id=matpilot。

更新本地 tgz 时使用新版本文件；Harness 可能把相同路径的安装视为没有依赖变化。非 registry 来源若更新失败，按页面提示备份配置后卸载再安装新包，不覆盖旧 tgz 后反复重试。

## 验收与边界

原生界面验收见 [连接包验收记录](native_plugin_acceptance.md)。Python 核心固定样本与九工具验证见 [dev8 验收记录](release_acceptance_v08.md)。旧发行文件保留，原生连接包独立版本不等于新材料模型版本。

真实计算仍需另外部署模型、权重及算力，明确授权并切换配置；本次未运行。演示通过不证明材料性能、真实双模型可用或整体流程提速。历史插件作用和正反例见 [插件效果记录](study_plugin_effects.md) 与 [消融记录](study_ablation_results.md)。

正常对话使用 Harness 账户预算；研究模块 benchmarks/harness_acceptance.mjs 的 19 元内部限额仅覆盖其创建的测试会话，不是整台电脑的账户限额。重跑付费对照前应核对当前官方价格。
