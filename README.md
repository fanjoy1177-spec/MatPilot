# 材策 MatPilot

**让材料研究需求成为可检查、可执行、可追溯的任务。**

MatPilot 是 DeepSeek Harness 的材料研究工具插件，直接使用 Harness 界面。第一版接入 MatterGen，提供能力查询、完整条件检查、持久化任务、候选检查和证据报告。开发版已增加 DiffCSP++ 模板适配器及统一核验；第二模型尚未通过真实计算验收，当前不宣称已获得双模型科学成果。

MatPilot is a capability-aware materials workflow plugin for DeepSeek Harness. V1 integrates MatterGen. A DiffCSP++ template bridge is implemented but not yet accepted on real inference; no new LLM is trained and MatterGen's neural network is unchanged.

## 当前开发范围

当前源码正在按 [研发方案 v1.1](docs/specification_v1.1.md) 实现，版本为 `0.2.0.dev5`，尚未完成整份方案验收。已增加逐项原文需求记录、确定性映射、候选身份与结构版本、指标来源核验、三值结果检查和结构等价匹配。进度与未完成事项见 [实施状态](docs/implementation_status.md)。

自然语言请求使用带精确原文引文的 `requirements`，程序自动定位唯一匹配；重复引文需要更完整的上下文或明确位置。最大采样预算包含已启动但失败的尝试，合格候选目标单独记录，详见 `get_capabilities` 和 `examples/request.json`。本地旧 0.1.1 安装包保留作回退。0.2.0.dev5固定发行包已通过176项Python回归、26项研究控制器检查与独立安装；桌面和真实 API 验证过程见 [新版验收记录](docs/release_acceptance_v05.md)；不把 demo 作为真实双模型验收。

## 0.1.1 历史已验证状态

- 21 项流程测试与 1 项官方 MCP 客户端协议测试通过。
- Harness `0.2.1-rc.2` 中实际注册九个工具，能力查询成功。
- 本机 WSL 与 RTX 4050 完成 1 个 Fe-Ni-O 候选生成、解析、检查和报告，执行约 248 秒。这是单任务联调，不是性能基准。
- 已在用户的 Harness 桌面版中接入真实 DeepSeek API，完成中文需求、条件冲突、固定化学式、单位转换和演示流程联调。过程与局限见 [桌面验收记录](docs/desktop_acceptance.md)。
- **用户提速对照尚未进行。** 固定示例数据不计作材料模型生成成果；本轮 AI 验收没有运行 MatterGen、DFT 或其他真实材料计算。

## 安装后端

需要 Python 3.10 或以上。下载源码，在项目目录建立独立环境：

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

也可从 [发行文件目录](releases/0.2.0.dev5/) 下载 Python wheel 和 Harness 插件包，按其中 SHA256SUMS.txt 核对。示例命令使用固定 NaCl 样本验证流程，不需要 GPU，也不调用 AI。

## 安装到 Harness

先安装 Node.js，再执行：

```text
npm install -g @deepseek-ai/dsh@0.2.1-rc.2
dsh --profile matpilot --from-default-profile web --dump-config
dsh plugin --profile matpilot add ./packages/dsh-matpilot
```

发行包用户把最后一个参数改成下载的 `dsh-matpilot-0.2.0-dev.5.tgz`。这是本地发行名称，不代表已发布到 npm。

以上命令用于 Harness 命令行/Web 配置。**桌面版的 `desktop` 配置由 Electron 应用管理，不能使用 `dsh plugin --profile desktop add` 安装。** 已验证的桌面接入方法见 [桌面配置说明](docs/desktop_setup.md)。

在同一终端设置实际路径。Windows PowerShell 示例：

```powershell
$env:MATPILOT_PYTHON = 'D:\MatPilot\.venv\Scripts\python.exe'
$env:MATPILOT_CONFIG = 'D:\MatPilot\examples\config.demo.json'
dsh --profile matpilot
```

Linux 示例：

```sh
export MATPILOT_PYTHON=/opt/MatPilot/.venv/bin/python
export MATPILOT_CONFIG=/opt/MatPilot/examples/config.demo.json
dsh --profile matpilot
```

在 Harness 中配置大模型账户或 API，然后可输入：

> 先查询 MatPilot 的能力。准备一个 Fe-Ni-O 体系、4 个候选结构的任务，把析氧活性和耐氯腐蚀性列为后续验证目标。先展示方案，不开始生成。

检查条件、数量和后端后再授权执行。无 API 时仍可用 CLI 和 MCP 客户端验证工具。真实 API 验收案例及失败记录见 `artifacts/desktop-acceptance.json`。

## 连接实际 MatterGen

按 [MatterGen 官方说明](https://github.com/microsoft/mattergen) 在独立 Linux/CUDA 环境安装模型和权重。复制 `examples/config.native.json`，将 `command` 改为实际可执行文件的路径，将 `MATPILOT_CONFIG` 指向该配置。真实计算默认禁用；只有取得相应授权、完成环境核对后才可由操作者设置 real_compute_enabled=true。当前任务禁止真实计算，保持 false。

可用 `checkpoint_paths` 将检查点名称映射到已下载的模型目录，避免运行时下载。较旧 MatterGen 不支持 `--seed` 时设置 `seed_supported: false`；记录会明确标注种子未应用，不能保证逐结构复现。

Windows 可用 `backend: "wsl"`，设置 `wsl_distribution`、`wsl_user` 和 Linux `command`。输出目录须位于可映射到 `/mnt/盘符/` 的本地磁盘。Linux 监督进程负责中断和超时后的进程组清理。

服务器可运行独立的 MatPilot Python 环境，并由 Harness MCP 客户端通过 SSH 启动 `python -m matpilot serve`。远程部署尚待服务器联调，未宣称验收。

## 不经 AI 的结构化入口

```text
python -m matpilot --config examples/config.demo.json capabilities
python -m matpilot --config examples/config.demo.json plan examples/request.json
python -m matpilot --config examples/config.demo.json submit PLAN_ID --approve-plan PLAN_ID --key my-request-0001
python -m matpilot --config examples/config.demo.json job JOB_ID
python -m matpilot --config examples/config.demo.json screen JOB_ID
python -m matpilot --config examples/config.demo.json report JOB_ID
```

用实际返回的编号替换大写占位符。`--approve-plan` 表示已检查并授权该计划。同一次提交重试应保持幂等键一致。关闭 MCP 连接不会取消独立进程中的计算；停止任务用 `control_job` 或 CLI 的 `stop`。

## 工具

| 工具 | 作用 |
|---|---|
| get_capabilities | 模型、检查点、条件组合和单位 |
| prepare_plan | 完整校验并固定方案，不自动丢弃要求 |
| submit_job | 按已授权计划提交并拦截重复提交 |
| get_job | 查询实际进度、批次和错误 |
| control_job | 停止及恢复明确终止的任务 |
| get_results | 结构文件与完整性校验值 |
| screen_candidates | CIF、组成、基础几何与文件重复检查 |
| compare_runs | 描述可比性、完成数量与耗时 |
| build_report | Markdown 任务报告与 JSON 证据 |

## 验证任务提升与流程提速

减少查参数、改配置、报错返工及整理结果的人工时间，是主要研究目标。[作用与证据对照](docs/contribution_and_evidence.md) 分开说明实现、已验证行为和仍需测量的收益。工程封装与 AI 额外增益分别评估。主比较口径见 [评估执行说明](docs/study_protocol_v1.1.md)，旧入口比较保留在 docs/validation_protocol.md。第二模型配置见 [部署说明](docs/multimodel_setup.md)。不填入推测的提速百分比。[同题插件作用对照](docs/study_plugin_effects.md)保留全部12题及正反例：已观察到具体纠错作用，当前没有整体耗时或API费用下降的证据。新增研究驱动和回放/盲评工具的边界及用法见 [研究工具说明](docs/study_tooling.md)。dev5增加研究源码与待审报告工具；真实Harness开发联调及失败记录见 [开发对照记录](docs/study_development_results.md)。正式独立评分与用户时间测量仍未完成。

```text
python -m unittest discover -s tests -v
python -m pip install build
python -m build
npm pack ./packages/dsh-matpilot --pack-destination dist
```

Windows MCP 异步运行时需要本机回环通信；受限沙箱可能阻断初始化。真实 GPU 联调需显式配置实际后端，默认测试只使用固定样本和故障注入。

## 结果边界

条件目标不等于测量值。可解析、组成符合或距离检查通过，不证明稳定性、可合成性、催化或耐腐蚀性能。工作进程意外崩溃后的自动接管尚未实现；心跳迟到只提示检查，不自动重复启动。

代码采用 MIT 许可证；第三方组件与权重遵守各自授权，见 `THIRD_PARTY_NOTICES.md`。Python wheel 与 Harness bundle 以本地发行包提供，尚未发布到 npm 或 PyPI。

## 本科项目定位与复用

本项目面向缺少计算经验的材料专业学生，重点是减少参数配置错误、避免悄悄放宽需求，以及整理可核验的任务结果。插件形式、中文交互和接入大模型本身不宣称首创。

[AtomisticSkills](https://github.com/learningmatter-mit/AtomisticSkills) 是重要的同类工作与复用来源；第二模型接口已实现，真实验收及正式对照实验尚未完成。本仓库当前使用独立的 MatterGen 适配器和 pymatgen，仅按需桥接固定版本的 DiffCSP++ 包装器，没有把上游全部模型能力算作本项目成果。材策 MatPilot 是独立本科项目，与其他同名研究系统无隶属关系。

规则校验目前检查模型提交的结构化参数，不能保证识别自然语言提取阶段的所有遗漏。完整性仍需结合原文、方案审阅和独立测试评价。

## 开发消融补充（2026-10-22）

新增显式选择题目与模块开关的研究导出工具，源码回归189项Python、27项JavaScript通过。四道公开开发题、三种真实配置各运行一次，共12条真实Harness记录；全部材料数据仍为CPU回放。完整插件本轮API核算较低、墙钟较高，尚不支持整体提速或任务正确率提高。[完整消融报告与证据](docs/study_ablation_results.md)保留全部结果、费用与归因限制。已发布的dev5安装包及桌面固定部署保持不变，研究工具补充从当前仓库源码使用。
