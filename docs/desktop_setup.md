# DeepSeek Harness 桌面接入

当前连接包为 **Harness 0.2.0-dev.12**，配套 **Python core 0.2.0.dev14**。两者独立编号；连接成功后应查询实际核心版本，不能只看插件卡片版本。插件声明支持 Harness `>=0.2.1-rc.2 <0.3.0`；首次安装与系统要求见[环境部署指南](deployment.md)。

## 首次使用

1. 下载 [Harness dev12 部署包](../releases/harness/0.2.0-dev.12/)，按同目录 `SHA256SUMS.txt` 校验并解压。
2. 在 Harness 的“插件 → 添加插件”中填写 `dsh-matpilot-0.2.0-dev.12.tgz` 的绝对路径，安装并启用“材策 MatPilot”。
3. 打开插件详情，复制环境检查指令并发送：“检查材策运行 MatterGen 的环境，并准备安装计划。保留现有环境，先不生成材料。”
4. 检查计划后授权安装；按[部署指南](deployment.md)查询进度、恢复失败并完成分层验证。真实单候选验证、启用真实计算和正式任务提交分别需要对应授权。

无需预装 Python 或 MatterGen，七个部署工具可先加载。联网安装会下载依赖和权重；WSL、系统驱动等缺项需先按检查结果处理。启用插件本身不会开始下载或材料生成。只体验固定样本时明确选择 `demo`。

## 已有环境手动连接

已有独立环境可以安装 [core dev14 wheel](../releases/0.2.0.dev14/)，按同目录校验值核对。wheel 不包含示例配置，应从同版本仓库获取 `examples/` 并保持模板相对位置。先按[项目首页](../README.md)运行 CLI demo，再连接桌面。

打开“已安装插件 → 材策 MatPilot”，**取消勾选“优先使用已验证并激活的托管环境”**，填写并保存：

| 字段 | 示例或要求 |
|---|---|
| Python 可执行文件 | `E:/MatPilot/.venv/Scripts/python.exe`；该环境应已安装 core dev14 |
| MatPilot 配置文件 | `E:/MatPilot/examples/config.multimodel.demo.json`；必须为绝对路径 |
| 独立安装目录（可选） | 普通 pip 安装留空；`pip --target` 安装填写对应目录 |
| 工具超时（毫秒） | 默认 `60000`；接受 `1000`—`300000` 的整数 |

保留托管环境勾选时，已激活的受管环境优先于手动路径。保存配置会重连工具服务；“配置已保存”仅表示设置已持久化。账户和 API 密钥由 Harness 管理，不填入上述表单。

桌面 profile 由应用管理，不使用 `dsh plugin --profile desktop add`。真实材料计算还需要模型环境、权重、配置和算力；Windows 使用 WSL2 后端，不能仅安装连接包就视为模型可用。

## 检查连接与固定样本体验

新建会话，输入：“查询 MatPilot 能力，告诉我核心版本、当前后端与真实计算状态，先不要执行任务。”

- `get_capabilities` 的 `version` 应为 `0.2.0.dev14`；固定样本体验应为 `backend=demo`、真实计算关闭。
- Python 核心应注册 **12 个 MCP 工具**，包括 `recommend_candidates`、`get_candidate_evidence` 和 `prepare_property_prediction`。七个部署工具另计，完整核心工具表见[项目首页](../README.md)。
- 如果仍返回旧核心版本，检查手动路径和托管环境选择；停用后重新启用插件，再次查询。刷新聊天页不保证后台进程更新。

连接检查通过后，可先发送：“使用固定 NaCl 示例，2 个候选，每批 1 个，只用 demo 后端。先展示方案，等待确认。”检查方案后再授权执行并要求结构检查和报告。报告应明确标注 `demonstration_fixture`；重复文件不计作多个独立材料发现。

已有候选的只读推荐无需启用真实计算。参数、默认策略及证据边界见[候选推荐说明](candidate_recommendation.md)和[跨材料使用指南](recommendation_v2.md)。

## 旧 MCP 配置迁移与更新

若曾在 `~/.dsh/profiles/desktop/cordis.patch.yml` 手动加入 `id=matpilot-mcp`，先备份配置，并将旧条目的 `command`、`env.MATPILOT_CONFIG` 和可选 `env.PYTHONPATH` 分别记录为上述三项路径。仅移除对应旧条目，保留其他账户及界面设置，再安装原生包并选择手动连接。原生条目为 `id=matpilot`，两套入口不能同时占用同名 MCP 工具。

升级使用新版本 tgz 文件及新的受管环境，完成连接验证后再切换。当前安装包不会自动替换正在使用的旧环境。若 Harness 提示本地包更新失败，按页面提示备份连接配置后卸载、重新安装新包，不反复覆盖同一路径的旧 tgz。

停用插件会移除其工具入口，但不会取消已经提交的独立任务；停用前用 `get_job` 或 `control_job` 查询、停止任务。

## 验证范围

当前安装验证见[部署验收说明](deployment-acceptance.md)，核心集成与科学证据边界见[验证摘要](validation_summary_20261101.md)。固定样本、MCP 连通和软件回归通过不证明材料性质、真实双模型可用或整体提速。Harness 对话按所用账户产生 API 费用。

旧版步骤及对应验收记录可从[清理前的桌面接入文档](https://github.com/fanjoy1177-spec/MatPilot/blob/9052a358f3f0b7135abd66db546fccb7ad2a8004/docs/desktop_setup.md)追溯，当前安装以本文和部署指南为准。
