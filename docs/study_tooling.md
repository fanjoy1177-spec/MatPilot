# 对照研究工具使用说明

本目录提供 U、U+、M 的研究工具，不把开发调试结果当成正式效果。普通用户仍使用已部署的 MatPilot 九工具插件。研究驱动必须显式启动；导出计划、安装或加载工具不会自行发起付费调用。

## 实际执行与替代边界

| 部分 | 实际执行的代码 | 当前证据范围 |
|---|---|---|
| U | 固定 AtomisticSkills 的原始 MatterGen server、构造器与生成方法；正常技能全文保留 | 原始软件行为；生成引擎由条件绑定的回放替代 |
| U+ | 相同快照应用已公开四项补丁，逐文件核对身份 | 局部修复行为；不是新科学模型 |
| DiffCSP | 实际 server 与 generate_with_symmetry；空间群、元素与 Wyckoff 来自共同登记模板 | 构造器、PyXtal/tensor 输入与扩散由明确标记的 CPU 替代实现；未验真实推理 |
| M | 同一 U+ 科学入口，加真实 Service.prepare_plan、requirements 和 evidence 模块 | 需求与结果检查的软件行为，仍需真实 Harness 与 LLM 联调 |
| 去需求映射 | 相同 Service 函数，只在私有函数环境替换需求映射依赖 | 关闭原文锚定、遗漏检查与参数映射；保留类型和单位归一化及普通模型输入校验 |
| 去结果核验 | 保留原始候选、指标和文件访问，跳过实际检查函数 | 不产生合格数量、通过判定或方法可比结论 |

共享后端只接受题目已审定的科学输入与有序尝试流，不返回任意请求的通用成功结果。条件、模型、数值和参数错误会被记录；已启动失败占用采样预算。整数 2 与数值等同的 2.0 可匹配，布尔值、不同数值、单位和条件含义不混同。

三组有相同候选、原始指标、CIF、上游输出目录、登记模板和公共模型输入能力的只读访问。统一科学默认值为 guidance_scale=2.0、step_lr=1e-5；基线省略时按公开协议填默认，显式不一致则明确拒绝，不偷偷覆盖。原始上游函数及其语义缺陷仍实际执行。基线的正常技能文档不能用缩减提示替换。

回放候选原始 CIF 与上游重新写出的 CIF 分别有结构版本；指标不自动继承到新文件。M 当前检查共享引擎原始版本，对重新序列化文件的任何结论都必须单独指明证据。生产验证器使用 demo 配置来允许明确标记的模板样本，研究执行后端是共享条件回放；不会调用生产 submit_job 或固定 NaCl 演示 worker。

## 本地检查

在独立 Python 环境安装项目和研究用依赖，然后执行：

```text
python -m pip install . -r benchmarks/study_drivers/requirements.txt
python -m unittest discover -s tests -v
node --test benchmarks/harness_study.test.mjs benchmarks/study_drivers/harness_bridge.test.mjs
```

Node 测试可用 MATPILOT_TEST_PYTHON 指定 Python。Windows 测试会核验关闭的是实际 Python worker；venv 的启动器和 worker 可能有不同 PID。回收仅针对本实例启动且仍存活的进程树。

CPU 测试、伪宿主契约测试、实际 Harness 回放联调和真实科学模型验收是不同证据。正式运行必须有 evidence_scope=harness_replay_end_to_end 的实际驱动验收记录；CPU 报告不能满足这个条件。

## 外部复核与封存

先生成待填写的复核包：

```text
python -m matpilot.study_workspace review-packet benchmarks/study/test_candidates.draft.json REVIEW_DIRECTORY
```

若已有完整待审定夹具，加 --fixtures FIXTURE_MANIFEST。模板的 approved 与 independent_of_implementation 均为 false，复核人信息为空。需要真实材料相关人员修改、审定内容并填写。题库或夹具改变后须重新生成哈希与复核，不把开发中已查看的题目称为未见测试集。

正式题库按 evaluation.freeze_suite 封存后，以 schedule 生成三组每题三次的 324 次主计划；消融子集需提前指定，并提供各自独立配置及验证报告。每份配置包括共享宿主、LLM、推理模式、科学参数与预算，以及模块、说明、完整源码清单和验收报告的哈希。删去重复、调整顺序或修改配置后，即使重算计划哈希，也不能通过重建检查。

export-runtime 接受 frozen、plan、inventory 和输出路径。inventory 指向实际模块、说明、共同能力文件、全部依赖清单、验证报告以及经独立审定的回放数据；它不是只填 true 的声明表。源码清单需要 source_revision 与 files[{path,sha256}]，路径相对清单目录且不得越界。验证报告绑定 configuration_identity，源清单哈希本身也须在配置中。程序核对身份和声明结构，不认证人员真实身份或科学标注正确性。

公开开发题使用 export-development 单独导出，不生成虚假的独立复核信息。也可用准备器一次完成源码复制、实际 CPU 烟测、身份绑定和导出：

```text
python benchmarks/study_drivers/prepare_development.py LOCAL_OUTPUT --provider PROVIDER_ID --model MODEL_ID --reasoning-mode off --host-version HARNESS_VERSION --max-output-tokens 8192
```

这里的宿主与模型参数必须填写当前实际配置。准备器不读取密钥、不创建费用账本、不启动 API；输出 runtime.json、environment.json、inventory.json、源码快照及 CPU 报告。运行所需 Python、Node 和研究依赖必须已安装。开发题的待审核流程可先完成，不以老师做题作为软件开发或联调的前置条件。

导出的控制器包只含公开题干、所需夹具编号和受控澄清回复，不含 gold、评分或允许答案。澄清回复只能经明确提问工具释放给 LLM。

### 显式选择开发题与消融组

不提供选择参数时，准备器仍导出全部 12 道开发题、U/U+/M 三组、每题每组 1 次，共 36 行。可用 `--task-ids` 和 `--groups` 在运行前固定较小的对照；例如按 `benchmarks/study/ablation_development_protocol.json` 选择四题和三个 MatPilot 组：

```text
python benchmarks/study_drivers/prepare_development.py ABLATION_OUTPUT --provider PROVIDER_ID --model MODEL_ID --reasoning-mode off --host-version HARNESS_VERSION --max-output-tokens 8192 --max-requests 16 --timeout-ms 180000 --task-ids D0101 D0201 D0202 D0502 --groups M M_without_mapping M_without_evidence
```

这条命令只准备本地包，不启动 LLM。输出保留 U、U+、M 和两个消融组的 **5 份驱动定义**，供运行器核验；实际 `tasks` 为 4 题，`rows` 仅包含所选三组的 **12 行**，未排入的 U/U+ 不会自动运行。`--repeats` 可显式设为 1、2 或 3；空列表、重复或未知题目/组名会被拒绝，选择任一消融组时必须同时选择 M 作为对照。

| 执行组 | requirement_mapping | evidence_checks |
|---|---|---|
| M | true | true |
| M_without_mapping | false | true |
| M_without_evidence | true | false |

三个 M 组使用完全相同的正常技能、研究预授权和 MatPilot 指令字节，指令哈希相等，不通过额外提示宣传某个开关已关闭。宿主、模型、推理模式、科学默认参数、采样及请求预算、源码和非消融运行选项保持一致。差别由 `implementation_group` 与上述真实功能开关传入同一 broker 执行；只改组名、改变提示或额外放宽预算不能通过配置核验。

`runtime.json` 的 `selection` 记录题目 ID、执行组、重复数、随机种子和原题库数量，`provenance.selection_sha256` 绑定该选择，计划哈希同时绑定配置和全部运行行。`preparation.json` 保存相同选择及包哈希；完整题库只保留身份哈希，运行包不加入评分答案。题干和受控澄清回复沿用原开发题，不为消融修改问题。

准备器运行实际复制源码的 CPU 工具链测试，再为各组生成绑定配置与源码的验证收据。收据会核对检查层实际开关及可观察行为，例如去映射时映射数量为 0、去核验时不产生已验证候选数量。其范围为 `cpu_tool_contract_only`，证明的是软件开关行为，**不是 LLM 已执行、任务正确率或插件提升结果**；不能据此填写 `harness_replay_end_to_end` 验收。

已有相应配置和清单时，底层导出命令也支持同样的选择：

```text
python -m matpilot.study_workspace export-development benchmarks/study/development_tasks.json ENVIRONMENT_JSON INVENTORY_JSON RUNTIME_JSON --task-ids D0101 D0201 D0202 D0502 --groups M M_without_mapping M_without_evidence
```

清单仍须包含 U/U+/M 的完整定义及所选消融组，不能借此跳过源码和收据核验。这是已公开开发题上的功能诊断；本例子集依据既有开发轨迹选择，不是盲选或正式效果试验。后续真实 LLM 运行应保留所有预选行及失败，由独立结果报告说明正面、负面或无差异观察。该功能不改变正式题库、夹具审定、实际 Harness 验收或评分要求，也不把开发子集结果推及完整任务正确率和人工提速。

## Harness 运行与费用

benchmarks/harness_study.mjs 为显式启动的研究插件；每组装载自己的已固定工具实现，关闭继承的非研究工具。benchmarks/study_drivers/harness_bridge.mjs 启动持久 Python broker.py；科学调用只能走对应上游函数，不能通过原始 sample 入口绕开。

插件配置需指定 packPath、outputDir、projectRoot、runtimeRoot、已有累计 ledgerPath、当前价格及输入/输出预占上限、单批 batch_guard_cny。这里不提供易过期的默认单价；实际付费运行前核对当前计费口径。加载只生成 ready.json；操作者提交绑定 runtime_pack_sha256 的 start.json 后才执行所选 run_ids。

沿用当前项目总账本，禁止缺失账本时从零重建。20 元是用户要求的累计提醒线，达到后记录并提示；它不是硬性停止额度。请求前预占，返回可靠用量后结算；失败或缺失用量保留预占，账本上界不冒充服务商实扣。单批额度用于防失控，不重置累计金额。

超时覆盖驱动打开、宿主创建和运行；取消后仍不响应的同进程代码会导致当前批停止、账本隔离并记录原因，不能虚称已经终止。保留完整原始事件、工具调用、失败、终态及费用引用，凭据字段脱敏。

## 导入与独立评分

正式包迁移、324 行批量归档、完整命令参数及四项评分/五个观察标志，见[正式评估归档与审核附录](formal_study_workflow.md)。旧 `import-run` 需补齐运行包、原始结果、事件和账本证明；正式汇总入口会重新核对归档工件。

每次运行生成 result.json、trace.json、events.jsonl；中断时另保留部分轨迹与日志。import-run 核对计划行、代码身份、末尾真实 turn/end、费用与状态后存入不可覆盖的正式记录。仅 whenIdle 或一个“完成”标签不能通过成功检查。

blind 命令生成 reviewer 子目录和独立 PRIVATE.mapping.json。只把 reviewer 目录交给评分者；控制器身份、系统提示、请求头与驱动内部日志不进入评分副本，文件路径替换为稳定别名。工具行为和自由文本仍可能暴露组别，因此明确为部分盲法。完整原始轨迹保留用于审计。

评分者只填写 grade、reviewer、evidence_notes；collect-grades 核对原始轨迹、评分副本与题干均未改变，再恢复运行对应。必须逐项评分，不由程序填造人类意见。未测人工主动操作时间保持 null，不能用墙钟运行时间代替。

正式汇总保留全部计划运行作为分母。缺记录、缺评分或身份不一致不能输出正式配对收益。报告任务正确率、可执行完成率、误拒绝、遗漏、错误启动、无依据结论，以及每正确任务成本；按题聚类计算差异与区间。任何提速或提升数字，都必须来自实际记录及独立评分。

## 开发结果的待用户审核包

无需伪造正式题库封存或老师意见，开发运行可直接生成独立待审材料：

```text
python -m matpilot.development_report RUNTIME_JSON RUN_DIRECTORY EXISTING_LEDGER OUTPUT_DIRECTORY
```

可重复提供 `--run-id ID` 明确选择子集；默认按运行包全部计划行审计，缺记录仍计为缺失。输出含report.md、metrics.json以及逐次可见题干、答复与工具记录，系统提示、请求上下文与内部流不进入公开副本。程序核验运行包与源码身份、轨迹与事件日志、终态、账本请求及费用的一致性；失败、截断和审计错误均保留。

自动汇总只报告对话终态、工具次数、运行墙钟与API计量；人工主动时间、任务正确率和提速保持未评价。当前用户选择在交付后自行审核，因此这一步不要求老师先做题。
