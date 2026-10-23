# 正式插件中的结构版本与指标比较

dev8 保留九个工具：在 dev7 的结构版本读取与指标比较基础上，补齐筛选、逐项核验和报告中的显式指标选择。这里只读取操作者或评价器已经登记的记录，不启动性质计算，也不允许 LLM 自行编造指标。

## 使用流程

1. 用 get_results(job_id) 读取原始候选、structure_versions 及 metrics_by_structure_version。候选 ID、版本 ID 和指标 ID 都从实际返回中取得。
2. 明确要比较哪些候选、哪个结构版本和哪个指标。不能用化学式替代身份，也不按数值最有利的结果自动选择。
3. 调用 compare_runs(job_ids, metric_selections)。每项选择必须同时包含 job_id、candidate_id、structure_version_id、metric_id；明确选择至少两个指标时，允许只给一个任务。
4. 读取 metric_comparison.comparable 和 reasons。同单位不表示可比，方法、权重、参照集、收敛等不满足时，返回不可比或信息不足，不输出优劣排名。

screen_candidates 和 build_report 只检查原始生成版本；额外结构版本不会增加生成数量或采样扣额。筛选返回 screening_version_scope="original_generated_versions_only" 明示范围。未知指标保持未知，不继承父版本的性质值。其他结构版本的指标可通过 get_results 读取，并在 compare_runs 中明确选择比较。

## 筛选与报告中的显式选择

没有提供 metric_selections 时，行为保持不变：同一候选、结构版本和性质存在多个有效测量，逐项核验返回 unknown，不自动采用最有利值。需要选择时，调用 screen_candidates(job_id, metric_selections) 或 build_report(job_id, metric_selections)，每项仍须同时给出实际返回的 job_id、candidate_id、structure_version_id、metric_id。

选择必须属于这个任务的原始生成版本，并匹配数值要求的性质和单位；每个版本的同一性质只能选一条指标。未知身份、交叉绑定、重复选择、额外版本以及不相关的性质或单位会被拒绝。所选指标缺少必要方法信息、未收敛或证据不可用时，结论仍为 unknown。未选择的性质和候选保持默认核验逻辑。显式选择不会增加候选数量或采样扣额，也不会修改原始计划。

返回的 metrics_by_structure_version 表示本次核验实际采用的指标；all_metrics_by_structure_version 保留全部已登记指标，包括未选的不利测量。metric_selection_audit 保存规范化选择、所选及未选指标 ID、selection_sha256 与解释边界。报告 JSON 保留这些记录，Markdown 明示所选身份和未选指标；判定仅适用于所选方法和指标，不构成材料普遍性质的保证。示例数据不构成真实性质结论。

默认报告仍写入数据目录下的 `reports/<job_id>/`。显式选择的报告写入 `reports/<job_id>/metric-selections/<selection_sha256>/`，不同选择分别保存，避免后一次选择覆盖前一次报告；仅选择顺序不同会归一到同一路径。这是按选择区分报告，不是每次调用自动创建新历史版本。

## dev7 → dev8 的实际对照

对冻结 dev7 wheel 和 dev8 源码分别使用独立 SQLite 数据目录，输入相同 NaCl ZIP、CIF 和两条合成指标原始字节，目标值为 1.1 eV，测量值分别为 1.1、1.6 eV：

| 项目 | dev7 | dev8 |
|---|---|---|
| 不指定指标，存在两条有效测量 | unknown | unknown |
| 显式选择 1.1 进入逐项核验及报告 | 不支持该入口 | pass，仅适用于所选指标 |
| 显式选择 1.6 进入逐项核验及报告 | 不支持该入口 | fail，仅适用于所选指标 |
| 显式选择绑定到其他候选 | 不支持该入口 | 拒绝错绑 |
| 默认、选择 1.1、选择 1.6 的报告 | 仅默认报告 | 三份同时存在 |
| 显式选择后可审计的测量 | 不适用 | 1.1、1.6 均保留 |
| 正式工具数 | 9 | 9 |
| 操作前后原始候选数 / 采样扣额 / 剩余预算 | 均为 1 / 1 / 0 | 均为 1 / 1 / 0 |

结果来自 [实际 CPU 对照记录](../artifacts/metric-screening-cpu-comparison.json) 和 [复算脚本](../benchmarks/compare_metric_screening.py)。脚本调用实际 Service 与已注册 MCP 函数，记录冻结 wheel、两组源码和输入工件哈希；不把这一检查当作外部 Harness 或真实模型验收。dev8 的 [最终 CPU 回归记录](../artifacts/v08-unit-validation.json) 共 275 项：266 项通过、9 项跳过、无失败或错误，运行期间所登记源码未变化；跳过项目不计为通过。

## dev6 → dev7 的既有对照

同一份合成 CIF 与指标字节，在独立数据目录对比冻结 dev6 wheel 和 dev7 源码：

| 项目 | dev6 | dev7 |
|---|---|---|
| 正式工具数 | 9 | 9 |
| 原始候选数 / 采样扣额 | 1 / 1 | 1 / 1 |
| get_results 可读取指标的结构版本 | 0 | 2 |
| get_results 返回的指标条数 | 0 | 3 |
| 单任务显式选择两版本同方法指标 | 接口不支持 | 返回可比 |
| 单任务显式选择不同方法指标 | 接口不支持 | 返回不可比，无排名 |

dev6 的底层数据库已支持指标记录；本轮改善是让宿主内的插件可以调用这项能力，而不是宣称新增材料计算算法。比较来自 [实际记录](../artifacts/version-metric-tools-cpu-comparison.json) 和 [复算脚本](../benchmarks/compare_version_metric_tools.py)，其中保留两版源码及夹具哈希。

此结果只证明指定输入下的工具行为。合成指标不是材料性能；接口可用不等于 AI 已正确选择指标，也不等于节省了多少人工时间。历史 U/U+/M 对照和消融另见 [贡献与证据](contribution_and_evidence.md)。
