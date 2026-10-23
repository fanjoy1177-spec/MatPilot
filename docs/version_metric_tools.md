# 正式插件中的结构版本与指标比较

dev7 保留九个工具，把底层已有的指标证据接口接到实际插件。这里只读取操作者或评价器已经登记的记录，不启动性质计算，也不允许 LLM 自行编造指标。

## 使用流程

1. 用 get_results(job_id) 读取原始候选、structure_versions 及 metrics_by_structure_version。候选 ID、版本 ID 和指标 ID 都从实际返回中取得。
2. 明确要比较哪些候选、哪个结构版本和哪个指标。不能用化学式替代身份，也不按数值最有利的结果自动选择。
3. 调用 compare_runs(job_ids, metric_selections)。每项选择必须同时包含 job_id、candidate_id、structure_version_id、metric_id；明确选择至少两个指标时，允许只给一个任务。
4. 读取 metric_comparison.comparable 和 reasons。同单位不表示可比，方法、权重、参照集、收敛等不满足时，返回不可比或信息不足，不输出优劣排名。

默认 screen_candidates 和 build_report 仍检查原始生成版本；额外结构版本不会增加生成数量或采样扣额。筛选返回 screening_version_scope 明示范围。未知指标保持未知，不继承父版本的性质值。

## 实际前后对照

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
