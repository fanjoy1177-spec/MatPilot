# 材策 MatPilot

**让材料研究需求成为可检查、可执行、可追溯的任务。**

MatPilot 是 DeepSeek Harness 的材料研究工具插件，提供需求检查、任务管理、候选结构核验、代表结构推荐和证据报告。接入 MatterGen，保留 DiffCSP++ 模板适配器；第二模型尚未通过真实计算验收。

MatPilot is a capability-aware materials workflow plugin for DeepSeek Harness. It connects research requirements, model execution and traceable candidate evidence without training a new LLM or changing MatterGen's neural network.

| 组件 | 当前开发版 | 下载与说明 |
|---|---|---|
| Python 核心 | `0.2.0.dev14` | [wheel 与 SHA256](releases/0.2.0.dev14/) |
| Harness 插件与环境部署包 | `0.2.0-dev.12` | [安装文件](releases/harness/0.2.0-dev.12/) · [部署指南](docs/deployment.md) |

两个版本号属于不同组件；当前安装配方与包内 wheel 均固定为 core dev14。开发版尚未发布到 npm 或 PyPI，安装包验证不等于已在每台用户机器完成部署。

## 快速开始

首次使用下载 [MatPilot 环境部署 ZIP](releases/harness/0.2.0-dev.12/MatPilot-setup-0.2.0-dev.12.zip)，按同目录 `SHA256SUMS.txt` 核对并解压。

1. 在 Harness 的“插件 → 添加插件”中填写解压后 `dsh-matpilot-0.2.0-dev.12.tgz` 的绝对路径，安装并启用。
2. 打开“已安装插件 → 材策 MatPilot”，复制环境检查指令，在对话中发送。
3. 检查安装计划后确认，安装隔离环境；失败时使用进度查询和恢复入口。
4. 完成导入、CUDA、MCP 检查，再按需授权单候选验证和真实计算。固定样本体验可选择 demo。

无需预装 Python 或 MatterGen；真实 MatterGen 计算仍需符合要求的 Linux/WSL2、NVIDIA 驱动、模型权重和磁盘空间。适用条件、原/新流程比较与失败恢复见[部署指南](docs/deployment.md)，已有环境的连接与旧配置迁移见[桌面接入](docs/desktop_setup.md)。账户与 API 配置由 Harness 管理。

连接后可输入：

> 先查询 MatPilot 的能力。准备一个 Fe-Ni-O 体系、4 个候选结构的任务，把析氧活性和耐氯腐蚀性列为后续验证目标。先展示方案，不开始生成。

确认条件、数量和后端后再授权执行。条件目标会保留为待验证目标，不会被当作已测得的材料性质。

## 能做什么

- 保留原文需求及对应关系，检查模型能力、条件组合、单位和采样预算。
- 固定已审阅方案，持久化执行任务，记录失败尝试并保留有效的部分结果。
- 核验 CIF、组成、结构身份和等价重复，给出候选推荐理由与排除原因。
- 绑定结构版本、指标来源和方法；缺少证据时返回未知，方法不可比时不混排。
- 输出可追溯报告，准备后续性质预测计划；计划接口本身不执行预测模型。

当前默认 `auto` 对支持的离子材料采用 `ionic_mixed_valence_v1`，不支持的体系明确回退。跨材料 `triage_v2`、显式性质证据和预测准备接口保留；`ionic_ranked_v2`、`ionic_complexity_v3` 是可选策略，未替换默认策略。详见[候选推荐](docs/candidate_recommendation.md)、[跨材料使用指南](docs/recommendation_v2.md)和[性质预测证据契约](docs/property_predictor_contract.md)。

Python 核心提供 12 个 MCP 工具；Harness 环境部署工具另行提供。

| 工具 | 作用 |
|---|---|
| `get_capabilities` | 查询模型、检查点、条件和单位 |
| `prepare_plan` | 检查并固定任务方案 |
| `submit_job` | 提交已授权方案，拦截重复提交 |
| `get_job` / `control_job` | 查询、停止及恢复任务 |
| `get_results` | 返回结构文件与校验值 |
| `screen_candidates` | 核验结构与所选指标 |
| `recommend_candidates` | 推荐已有候选并提供审计记录 |
| `get_candidate_evidence` | 查询候选证据与可比性 |
| `prepare_property_prediction` | 准备预测计划及证据契约 |
| `compare_runs` | 比较可比任务的完成数量与耗时 |
| `build_report` | 保存任务报告与证据 |

## 源码安装与固定样本演示

需要 Python 3.10 或以上。在仓库根目录建立独立环境：

```text
python -m venv .venv
```

Windows：

```text
.venv\Scripts\python -m pip install .
.venv\Scripts\python -m matpilot --config examples/config.demo.json demo
```

Linux：

```sh
.venv/bin/python -m pip install .
.venv/bin/python -m matpilot --config examples/config.demo.json demo
```

演示使用固定 NaCl 样本，无需 GPU 或 AI。wheel 不包含示例配置，单独安装 wheel 时需从同版本仓库取得 `examples/`。真实 MatterGen 默认禁用；已有模型环境通过配置中的命令、工作目录和 WSL 参数连接。第二模型配置见[多模型部署说明](docs/multimodel_setup.md)。

## 验证与边界

最新[验证摘要](docs/validation_summary_20261101.md)区分软件回归和科学证据。固定 100 结构研究的主方法命中真实前 50 名中的 **11/20**，随机期望为 **10/20**；MACE 归档标量对照为 **20/20**，但收敛状态未知，未注册为可用 Service 指标，不能替代主方法结果。

[公开复现数据与命令](benchmarks/ranking100/)可离线核对六种方法的排名指标。本轮没有新增模型推断、几何弛豫或 DFT。既有开发对照尚未证明整体耗时或 API 费用下降，详见[作用与证据](docs/contribution_and_evidence.md)及[历史对照记录](docs/history.md)。

结构可解析、组成匹配、有限价态规则可行或进入推荐清单，都不证明稳定性、可合成性、催化或耐腐蚀性能。原文映射仍需审阅；工作进程意外崩溃后的自动接管尚未实现，心跳迟到不会触发自动重跑。

开发检查与 CI 使用固定样本和故障注入：

```sh
python -m pip install . -r benchmarks/study_drivers/requirements.txt
python -m unittest discover -s tests -v
node --test benchmarks/harness_study.test.mjs benchmarks/study_drivers/harness_bridge.test.mjs
node --test benchmarks/deployment/*.test.mjs
```

## 仓库导航

| 目录 | 内容 |
|---|---|
| `src/` | Python 核心与模型适配器 |
| `packages/` | Harness 插件与环境部署实现 |
| `examples/` | 示例请求、配置和模板 |
| `docs/` | 使用说明、接口契约与验证边界 |
| `tests/` / `benchmarks/` | 回归测试、固定夹具、研究工具与复现数据 |
| `releases/` | 当前开发版安装文件及校验清单 |

旧版安装包、过程验收记录、调研草稿和生成报告通过[历史资料入口](docs/history.md)访问。新生成的报告与日志由 `.gitignore` 排除，避免再次混入源码目录。

代码采用 MIT 许可证，第三方组件和权重遵守各自授权，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。MatPilot 是独立项目，与其他同名研究系统无隶属关系；[AtomisticSkills](https://github.com/learningmatter-mit/AtomisticSkills) 是同类工作与部分桥接设计的复用来源。
