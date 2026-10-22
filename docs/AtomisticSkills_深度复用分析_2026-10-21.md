# AtomisticSkills 深度复用分析

检索与核验日期：2026 年 10 月 21 日。

建议保留 MatPilot 已有的任务服务和 MatterGen 适配器，按需调用 AtomisticSkills 的计算工具、技能脚本与独立环境。自主开发收敛到材料生成任务的需求完整性、候选身份和结果判定。插件包装、多模型接入、单位说明、研究计划和一般溯源已有直接先例，不能单独作为创新主张。

本次下载并检查了源码，在不调用模型、GPU 或外部 API 的条件下完成四项隔离核验。它们证明特定函数的行为，不代表完整智能体的失败率、生成质量或实际部署成功率。

## 版本与证据

| 对象 | 固定版本 | 用途 |
|---|---|---|
| AtomisticSkills | `6afdd465a35088136e4875d7d2fbaefc633f4adf`，插件清单版本 2.0.0 | 代码审查和隔离核验 |
| AtomisticSkillsBenchmark | `db6116c1833f30cccb65ff78cda175510c3107b6` | 基线和评分设计参考 |
| 本地 MatPilot | 读取当前任务书、架构、验证协议及源代码；参与核验文件另存 SHA256 | 分清已有基础与待实现能力 |

原始调研时的源码副本位于调研环境的 `upstream/`；公开仓库只保留 [四个补丁涉及文件的原始快照](../benchmarks/baseline/upstream_snapshot/)；逐文件来源与哈希见 [source_manifest.json](atomisticskills_review/source_manifest.json)，核验记录见 [probe_results.json](atomisticskills_review/probe_results.json)，复核程序见 [review_probes.py](atomisticskills_review/review_probes.py)。上游代码保持原样；尚未安装其运行环境。

## AtomisticSkills 已经解决了什么

它按工作流、技能和计算工具组织科研过程，由通用智能体读取技能并调用工具。插件清单预配置十个 MCP 服务，包括生成模型、机器学习势、数据库和 Atomate2；因此 MatPilot 不必再证明“材料能力可以做成插件”。[插件清单](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/.claude-plugin/plugin.json)

科研规范已经要求先规划、保存任务目录、记录结果，并为复杂任务设置用户审阅。部分指令依赖特定宿主的 artifact、notify_user 等工具，移植到 DeepSeek Harness 时需要转换为该宿主实际支持的交互方式。[研究规范](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/.agents/rules/research-standards.md)

其 MatterGen 技能已经指出：元素体系不等于精确化学式，条件引导不保证输出元素完整，生成后需要解析与筛选。MatPilot 应复用这些科学边界，研究如何让软件持续执行这些要求。[MatterGen 技能](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/skills/ml-generative-mattergen/SKILL.md)

稳定性技能要求目标结构和参照相使用一致的模型与设置，并区分近似计算与进一步 DFT 验证。单位技能还区分原始模型输出和 ASE 计算器输出。不能把“知道这些规则”或“写了注意事项”算作新方法。[稳定性技能](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/skills/mat-stability/SKILL.md)、[单位规范](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/skills/general-property-units/SKILL.md)

## 哪些部分值得直接复用

“直接复用”指采用已有实现，不表示当前设备已具备运行条件。首版采用固定提交的独立工具进程，避免将整套科学依赖并入 MatPilot 的 Python 环境。

| 模块 | 复用方式 | MatPilot 补充内容 | 优先级 |
|---|---|---|---|
| 生成模型技能与能力说明 | 采用 MatterGen、DiffCSP++ 的科学说明、输入示例和依赖资料 | 转成有来源、版本和可用性状态的能力卡；逐项核验输入语义 | 立即 |
| DiffCSP++ 封装 | 调用已有 wrapper 或独立脚本，复用模型加载与采样 | 明确对称性前提、组成核验、预算、文件索引和部分完成状态 | 第二模型首选试验 |
| MatterGen 封装 | 保留现有 MatPilot 官方命令调用；参考上游环境与设置脚本 | 避免通过会覆盖条件的快捷入口提交复合需求 | 参考与交叉验证 |
| 结构读取和结构匹配 | 优先直接采用 pymatgen；参考上游 match_structure.py 的批量组织 | 按候选 ID 返回错误；区分查询失败、未命中和已匹配 | 立即 |
| Materials Project 查询 | 接入现成 MP 工具或 mp-api | 保存数据库 ID、查询条件和时间；不把数据库性质移植给生成候选 | 按案例需要 |
| MACE／MatGL 等弛豫工具 | 首版至多选择一个已就绪计算器，沿用独立环境 | 保存结构版本、权重、方法、收敛信息和单位 | 第二阶段 |
| 凸包与稳定性计算 | 复用 pymatgen 算法；修改上游脚本的目标选择方式后再采用 | 按候选唯一 ID 选择目标；检查能量可比性和参照集 | 有适用数据后 |
| venv/run 与环境锁定 | Linux worker 可采用上游 launcher 和固定依赖 | 验证硬件、权重与路径；每任务显式指定输出目录 | 部署阶段 |
| Atomate2 远程工作流 | 留作未来 DFT／集群接口 | 将其 job_id 映射到 MatPilot；不重复发明远程调度器 | 首版不扩展 |
| Benchmark 的任务组织 | 参考 instruction、environment、solution、tests 分离方式 | 自建需求完整性与候选身份任务；隐藏标准答案 | 立即 |

对应代码入口：[DiffCSP++ wrapper](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/src/utils/generative_models/diffcsp/diffcsp_wrapper.py)、[MatterGen wrapper](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/src/utils/generative_models/mattergen/mattergen_wrapper.py)、[结构匹配脚本](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/skills/mat-structure-novelty/scripts/match_structure.py)、[Atomate2 服务](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/src/mcp_server/atomate2_server.py)。

### 部署需要保留的边界

上游 MatterGen 环境指定 Python 3.10，DiffCSP++ 指定 Python 3.11，且两者依赖集合不同。当前 MatPilot 的轻量服务不应合并这些环境。采用 Windows 上的交互端配合 Linux／WSL worker，或者统一 Linux worker；实际选项由设备小任务验证决定。[MatterGen 环境](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/venv/mattergen/pyproject.toml)、[DiffCSP++ 环境](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/venv/diffcsp/pyproject.toml)

DiffCSP++ 的现成 MCP 入口要求空间群、Wyckoff 位置和对应元素，不能把“输入一个化学式”直接描述为已支持的完整过程。首版要求提供经过核对的结构模板或明确的对称性规格；缺失时澄清，不让 LLM 猜位点。如果权重或环境未在选型窗口内就绪，再试 CrystalFormer；Chemeleon 暂不列首选，以减少另写生成封装的工作。[DiffCSP++ 技能](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/skills/ml-generative-diffcsp/SKILL.md)

上游研究目录可由工作区和 CURRENT_RESEARCH_DIR 推导。MatPilot 应始终传入自己的 job_id 专属路径，不用这个会话级默认目录承担多任务隔离。[目录管理](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/src/utils/research_utils.py)

AtomisticSkills 根目录 LICENSE 标注 MIT；Benchmark 标注 Apache-2.0。复用代码保留其声明与来源，模型权重、数据库和计算软件分别登记，不把根目录代码许可延伸为所有外部资源的许可。[代码许可](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/LICENSE)、[评测许可](https://github.com/learningmatter-mit/AtomisticSkillsBenchmark/blob/db6116c1833f30cccb65ff78cda175510c3107b6/LICENSE)

## 隔离核验得到的具体问题

| 编号 | 输入与观测 | 影响和解释 |
|---|---|---|
| P1 条件优先级 | 同时给出 Fe-Ni-O、磁密度条件及对应模型，MatterGen 服务传给 wrapper 的条件只剩元素体系，模型改成 chemical_system | 接口文档明确说明了覆盖行为；这不是未披露行为。MatPilot 需要检测研究要求与实际参数的差异，不能只检查调用成功 |
| P2 数量语义 | 请求 3 个、每批 2 个；wrapper 向替代生成器传入 2 批并保存其返回的 4 个结果 | 证明包装层向上取整且不裁剪；没有实测真实模型产出或费用。计划应区分计算预算与需要的有效候选数 |
| P3 候选身份 | 给凸包函数两条同为 LiCl 的合成能量记录；交换顺序后，输出从 0 变为 250 meV/atom | 函数按化学式取第一个匹配条目。用真实 pymatgen 运算验证了身份歧义，但输入能量是测试数据，没有计算任何真实材料性能 |
| P4 本地校验边界 | MatPilot 收到完整的冲突条件时 blocked；把原文中的带隙要求从结构化参数漏掉后变成 ready | 当前规则只能检查收到的参数，不能保证原始自然语言完整。新方案必须覆盖这一缺口，且不能宣称机器能绝对理解所有原文 |

P1 源码位置：[mattergen_server.py](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/src/mcp_server/mattergen_server.py#L83)。P2：[mattergen_wrapper.py](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/src/utils/generative_models/mattergen/mattergen_wrapper.py#L153)。P3：[compute_ehull.py](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/skills/mat-stability/scripts/compute_ehull.py#L170)。P4 对应本地 `src/matpilot/adapters/mattergen.py`；该文件哈希已随核验保存。

另有两个静态审查发现需要在接入时处理：稳定性加载函数读取结构和数值能量，但没有在该入口强制核对每条记录的模型、单位与收敛元数据；结构查重脚本会在 MP 查询异常后使用空候选列表，并在无匹配时打印 NOVEL。调用方应分别处理不完整计算依据与数据库不可用，不能把它们视为已获得科学结论。这些是所审查路径的行为，不代表整个系统没有其他防护。

上游当前 MatterGen 和 DiffCSP++ 的两个服务测试主要检查返回结果含数量且没有 error。它们不能代替条件完整性、精确数量和结论来源的专门测试；也不能据此断言上游所有测试都很弱。[MatterGen 测试](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/tests/mattergen/test_mattergen_server.py)、[DiffCSP++ 测试](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/tests/diffcsp/test_diffcsp_server.py)

## 创新性需要进一步收窄

确定性校验、执行前检查和可追溯报告也有相近研究。LatticeMind 使用工作流约定、验证和有限修复管理 DFT；materials-semantic-mcp 为合成材料实验室数据建立指标语义与来源标签。它们说明“增加规则和来源字段”不能自动形成新颖贡献。[LatticeMind](https://arxiv.org/abs/2610.26869)、[materials-semantic-mcp](https://github.com/Tehscientist/materials-semantic-mcp)

更值得验证的问题是：面对控制能力不同的生成模型，如何让每条材料要求从原文、模型参数一直对应到具体候选的检查结果；在无法同时满足时，如何清楚说明可选修改，并保持用户未授权修改的要求不变。

研究输出可以集中为三项：一个带来源片段和判定方式的需求表示；一个跨模型生成与筛选计划的确定性检查器；一个专门检验需求丢失、候选串用和错误结论的任务集。它们目前是研究假设，尚未被证明具有独立方法创新。

只修复 P1 或 P3 属于有价值的软件修复。要提出系统性贡献，还需要在修复这些缺陷后的基线上表现出稳定收益，并在两个真实模型、不同需求表达和未见任务上保留效果。

## 如何复用评测而不误读结果

AtomisticSkillsBenchmark 有 31 个任务，分开保存任务环境、指令、参考解和验证器。其 no-skill 组仍安装上游 src 库，比较的是有无技能文档，不是裸 LLM 与完整工具系统。镜像中的 AtomisticSkills 主要固定在 `10048cca…`，不能直接配当前 2.0.0 技能后宣称复现原结果。[评测说明](https://github.com/learningmatter-mit/AtomisticSkillsBenchmark/blob/db6116c1833f30cccb65ff78cda175510c3107b6/README.md)

抽查的凸包任务验证器检查一个数值字段与参考值的接近程度。它不单独证明生成需求完整、候选身份没有串用或所有科学表述都有依据。MatPilot 应补这些评价维度，不能把这个发现推广到全部 31 个任务。[凸包验证器](https://github.com/learningmatter-mit/AtomisticSkillsBenchmark/blob/db6116c1833f30cccb65ff78cda175510c3107b6/tasks/materials-science/convex-hull-stability/tests/test_outputs.py)

主实验设置为原始上游、修复已知局部缺陷后的上游、增加 MatPilot 需求与结果检查层三组。后两组共享计算工具、模型、任务、提示预算和故障记录。修复后的基线也获得准确的科学说明，避免靠隐藏说明或限制基线工具制造优势。

新增 12 个开发题和 36 个封存测试题，按题目而非重复运行统计独立样本。历史回放、模拟计算和真实计算分别记录。保留可执行题的完成率和误拒绝率，防止“全部拒绝”获得高分。完整修订安排见 [MatPilot 研发方案 v1.1](MatPilot_研发方案_v1.1_2026-10-21.md)。
