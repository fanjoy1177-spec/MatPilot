# 正式评估归档与审核附录

本附录说明现有离线工具的用法，不宣布正式试验已经完成。12 道公开开发题的 Harness 对照与开发消融已有独立归档；36 道候选正式题仍待审定，完整正式夹具与正式 324 次运行尚未完成。用户选择交付后自行审核，本轮不要求安排老师做题，也不执行新的付费试验或真实材料计算。

## 准备与迁移

正式主计划为 36 题 × U/U+/M 三组 × 每题每组 3 次，共 324 次。题库、固定澄清、评分口径、夹具和运行配置应在正式运行前确定；草案的数量检查不能替代领域审定，已在开发中看过的题目不能直接称为未见测试集。

以下大写参数均为实际本地文件或目录路径，在已安装当前研究源码的 Python 环境中执行。所有输出采用新文件或新目录；不得覆盖旧记录。

```text
python -m matpilot.study_workspace review-packet SUITE_JSON REVIEW_DIRECTORY --fixtures FIXTURE_MANIFEST
python -m matpilot.evaluation freeze SUITE_JSON REVIEW_JSON FROZEN_JSON
python -m matpilot.evaluation schedule FROZEN_JSON ENVIRONMENT_JSON PLAN_JSON
python -m matpilot.study_workspace export-runtime FROZEN_JSON PLAN_JSON INVENTORY_JSON RUNTIME_JSON
```

`review-packet` 只生成未批准模板；没有完整夹具时可省略 `--fixtures`。`freeze` 需要真实、完整且绑定题库哈希的复核声明。`schedule` 的 CLI 生成三组主计划；正式消融另需通过 Python `schedule` 接口预先提供子集与已验证的消融配置，不能在查看结果后追加组别。

`export-runtime` 同时核验正式夹具结构、覆盖范围、独立复核声明、源码清单、工具选项及实际 Harness 验收收据。CPU 契约测试报告不能冒充 `harness_replay_end_to_end` 验收。软件核验这些文件的身份和声明，不能认证复核人的真实身份或标注的科学正确性。

新版正式运行包增加 `source_inventory`，保留解析路径前的原始选项，用于复查选项哈希。**旧正式包须在未来正式运行前重新导出**；不能给旧轨迹补写新包哈希，使其看似来自新配置。开发运行包的 `mode=development` 不能通过正式导入。重新导出或加载包本身不会启动 API；Harness 的显式启动另见[工具说明](study_tooling.md#harness-运行与费用)。

## 从原始 Harness 目录批量归档

先让本批运行停止写入，保留原始目录与已有累计账本。批量导入直接读取 Harness 输出，不需要把 324 份 `result.json` 手工拆成行和结果文件：

```text
python -m matpilot.study_workspace import-runs FROZEN_JSON PLAN_JSON RUNTIME_JSON RUNNER_DIRECTORY EXISTING_LEDGER_JSON ARCHIVE_DIRECTORY
```

`RUNNER_DIRECTORY` 下应为 `runs/RUN_ID/result.json`、`trace.json`、`events.jsonl`。`ARCHIVE_DIRECTORY` 必须尚不存在。命令检查每个计划行，并输出 `import-index.json`：

| 索引状态 | 含义 |
|---|---|
| `verified_record` | 来源和计量核验通过；执行本身仍可能失败、超时或被预算停止 |
| `missing` | 没有该计划行的源目录 |
| `partial` | 有目录，但缺少最终结果、轨迹或事件日志 |
| `invalid` | 有记录，但身份、来源、事件、终态或计量核验不通过 |

所有计划行均保留；失败不会被替换为成功，缺失或无效记录不会被补成零成本结果。部分及无效记录留在原始目录，索引记录已发现文件的哈希与诊断，因此也必须保留原始目录。计划之外的源目录会显式列出，不能直接进入正式分析。

每条通过核验的归档包含 `run.json`、`audit.json`、原始 `result.json`、`trace.json`、`events.jsonl`、对应 `runtime.json` 和该会话的 `ledger-records.json`。核验包括：

- 冻结计划、题目、组别、重复序号、源码与运行包哈希相符。
- 结果引用的轨迹字节哈希正确，完整事件与原始日志一致，终态受实际末尾 `turn/end` 支持。
- API 请求 ID 唯一、归属同一执行会话、没有遗漏该会话的账本记录，顺序一致；消息中报告的用量可在账本中按顺序找到。
- API 金额可按用量或保守预占复算，且与结果一致。本次会话仍有 `reserved` 请求时拒绝最终导入；失败调用的 `reservation_retained` 保留成本。其他历史会话的保留额不会被删除。
- 回放科学计算费用与时间为 0，人工主动操作时间必须为 `null`。墙钟时间不能替代人工时间。

当前轨迹格式没有独立的会话分配收据。因此，即使是失败且尚无助手答复的记录，空 `usage_record_ids` 也不能证明没有付费请求：正式导入会拒绝，批量索引保留为 `invalid`。确实未启动请求的旧格式记录同样需要补足独立证据才能设计后续支持，不能据此删除账本记录、补写会话身份或认定费用为 0。此限制不改写已经归档的开发记录。

费用仍是项目账本计量，不等于服务商最终实扣；文件哈希也不证明材料性能。原始轨迹及本地运行包属于私有审计材料，公开展示使用脱敏审核副本。

单条兼容入口仍存在，但新增四个证明参数为必填；传入的行和结果仅作为原始文件的交叉核对：

```text
python -m matpilot.study_workspace import-run FROZEN_JSON PLAN_JSON ARCHIVE_DIRECTORY ROW_JSON OUTCOME_JSON TRACE_JSON --runtime RUNTIME_JSON --ledger EXISTING_LEDGER_JSON --result RESULT_JSON --events EVENTS_JSONL
```

## 审核副本与人工评分

```text
python -m matpilot.study_workspace blind FROZEN_JSON PLAN_JSON ARCHIVE_DIRECTORY BLIND_DIRECTORY
```

该命令先重新核对归档工件，然后生成 `reviewer/B00001.json` 等待评分文件，以及单独的 `PRIVATE.mapping.json`。审核者只接收 `reviewer` 目录。副本去除系统提示、请求头、内部推理流、凭据和本地路径；工具行为及答复仍可能暴露组别，必须披露为部分盲法。

审核者仅填写 `grade`、`reviewer`、`evidence_notes`。原题干、轨迹、哈希及其余字段保持原样。程序不填写人的意见；未审核保留空白，不能为了导入而填 `false`。正式使用前还需审定适用性、修正后成功与错误尝试的判分口径，不能根据已知实验结果临时调整。

四项主评分均为显式布尔值；全部通过且运行终态为 `completed` 时，汇总程序才计为完整正确：

| 字段 | 人工核对内容 |
|---|---|
| `requirements_correct` | 是否准确保留必要要求、硬条件、单位和允许的变更，没有把未验证目标说成已满足 |
| `actions_correct` | 是否按已审定任务采取适当动作，包括澄清、执行、停止或有依据地拒绝 |
| `candidate_identity_correct` | 候选、结构版本、工件及指标的对应是否正确，没有混淆同化学式或不同版本 |
| `conclusions_supported` | 结论是否有对应证据，方法和单位是否可比，是否如实保留未知和未收敛情况 |

另有五个观察标志；它们不是根据工具名称或控制器终态自动生成的分数：

| 字段 | 观察内容及边界 |
|---|---|
| `executable_completed` | 原本可执行的任务是否实质完成；仅准备方案、对话结束或生成文件不能替代判断 |
| `false_refusal` | 是否错误拒绝初始可执行任务；统计分母只含题库标记为初始可执行的任务 |
| `requirement_omission` | 是否遗漏必要要求；应在备注指向原题及相关答复或调用 |
| `wrong_start` | 是否发生不应启动的任务动作；需结合实际执行与预算记录，不能只凭出现 execute 工具名认定产生了采样 |
| `unsupported_conclusion` | 是否作出超出证据的结论，例如将回放数值、不同方法结果或未验证性质当成材料性能证明 |

`false_refusal` 或 `wrong_start` 为真时，`actions_correct` 不能同时为真；`requirement_omission` 与 `requirements_correct`、`unsupported_conclusion` 与 `conclusions_supported` 同理。候选身份不适用等边界应在正式评分口径中先说明，不能由程序默认补通过。备注应足以定位证据，不能只写“正确”或“错误”。

收集已填写且字段完整的评分文件：

```text
python -m matpilot.study_workspace collect-grades BLIND_DIRECTORY/PRIVATE.mapping.json COMPLETED_REVIEWER_DIRECTORY GRADES_JSON
```

`COMPLETED_REVIEWER_DIRECTORY` 中仅放本轮已完成的 `B*.json` 副本；未完成的审核材料仍保留在原目录。收集器核对轨迹、审核副本和题干未被改变，恢复运行对应；缺评分不会消失，后续汇总仍显示为未评分。

## 汇总及当前完成边界

```text
python -m matpilot.evaluation summarize FROZEN_JSON PLAN_JSON ARCHIVE_DIRECTORY GRADES_JSON SUMMARY_JSON --bootstrap-samples 2000
```

`--bootstrap-samples` 可省略，默认 2000，必须为正整数。尚无人工评分时可传内容为 `{}` 的评分文件查看缺失诊断，不会得到正式收益。汇总再次检查原始归档文件、导入审计、账本和运行包依赖；需保留运行包所指向的固定源码及复核文件，移动或修改后不能直接绕过校验。

缺记录、缺评分时输出 `incomplete_do_not_interpret_effectiveness`；不计算正式配对收益。全部所需记录及评分齐备后，按题聚类汇总三次重复，计算 M 与 U+ 的配对差异和区间。失败成本保留，每正确任务成本需要相应分母与测量；没有人工时间测量就没有人工提速结论。`completed` 仅说明控制器正常结束，不能直接当作任务正确。

当前已用明确标注的合成测试验证上述导入、脱敏与汇总软件流程；开发对照的真实 LLM 轨迹另有来源和费用审计。这些证据不能替代正式夹具审定、正式 324 次对照、真实人工评分或两种材料模型的实际计算验收。本轮只补全离线工具与交付说明，不填造这些尚未完成的证据。
