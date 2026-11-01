# MatPilot 跨材料推荐使用指南

源码集成版（0.2.0.dev14）补充：默认入口为 `auto`，无额外材料类别或性质证据参数时使用操作者 `automatic_ranking_policy`（默认 `ionic_mixed_valence_v1`）。明确 `material_class` 或性质证据时，`auto` 转入本指南的 `triage_v2` / 证据流程。显式 `triage_v2` 的行为保留。本版还提供可选实验策略 `ionic_ranked_v2` 和 `ionic_complexity_v3`；二者均未替换默认排序。

本版把原有流程中“人工逐个打开结构、确认身份、整理性质证据、选择下一批计算对象”的步骤，接入统一的检查、证据选择和推荐流程。输入是已登记的候选结构，输出是带理由的优先计算清单、需要复核的候选及完整审计。推荐步骤本身不调用生成模型、性质预测器、弛豫或 DFT。

当前可以证明软件按明确规则处理候选、追溯证据和保留未选结果。科学推荐准确率、人工用时和下游材料性能的提升还需要独立数据与同条件对照；本指南不给出未经测量的提升百分比。

## 材料范围与策略

Fe–Ni–O 保留作历史回归案例，推荐接口没有将其设为默认元素体系。每个任务或集合须声明自己的 `expected_elements`，程序核对实际 CIF 元素。盐类、金属/合金、共价晶体等可分别登记和调用；当前单个集合只有一组预期元素，不能把 Na–Cl 和 Si 两种元素集合混在同一个集合中，假定程序自动分组。

| 选择方式 | 当前行为 | 使用场景 |
|---|---|---|
| `triage_v2`，显式选择 | 每个候选先检查，再选等价组代表；根据声明材料类别和规则范围选择检查方法；提供可比性质证据时进入证据排序 | 新任务的一般入口 |
| `structural_v2` | 检查结构准备度，不施加离子价态规则 | 只需要结构检查，或材料类别不适合现有离子规则 |
| `ionic_mixed_valence_v2` | 在明确适用的离子体系中检查有限先验价态的整数电荷可行性；不适用时拒绝强行使用 | 已明确离子材料且规则支持的元素集合 |
| `evidence_v1` | 需要明确 `objective`；只采用显式选择、身份正确、元数据可比的指标；缺证据进入复核 | 已有性质证据，希望按目标排序 |
| `auto` | 读取默认排序配置；明确材料类别或性质证据时进入新版管线 | 源码集成版默认入口 |
| 各 `*_v1` 历史策略 | 保留旧版行为，便于重放和对照 | 历史复现 |

推荐工具的 `material_class` 可取 `auto / ionic / metallic / covalent / other / unknown`。知道是金属或共价材料时应明确填写，避免把元素组合误解为离子材料。`auto` 仅按元素判断现有离子规则能否使用，不能识别真实键合。规则不适用时只提供结构准备度检查，并在结果中说明回退原因。

同时提供 `objective` 时，使用 `auto`、`triage_v2` 或 `evidence_v1`；程序会报告实际策略 `evidence_v1`。历史策略不接受新增的性质排序或材料类别参数。

## 插件改变了哪些步骤

| 原有人工流程 | 插件当前承担的工作 | 应查看的证据 |
|---|---|---|
| 逐个核对结构文件和编号 | 核对候选、结构版本、文件 SHA256、元素、占位和周期性几何 | 每条候选的结构检查与排除原因 |
| 先挑一个代表，再发现代表不适用 | 先检查每个结构，再在等价组内选择符合要求、证据更优的代表 | `representative_id`、分层结果和未选候选审计 |
| 手动对照不同文件的性质值 | 将指标绑定到准确结构版本，核对方法、版本、权重、单位、参照和收敛信息 | `metric_selection_audit`、`ranking_metadata` |
| 按经验挑“看起来有希望”的结构 | 按已声明目标排序；相同证据优先级下才比较组成、空间群和规模覆盖 | `evidence_rank_key`、`selection_reason` |
| 只保存最终选中的几个文件 | 保留所有候选、所选与未选指标、缺失证据和输出不足 | `include_all_records=true` 的 JSON 结果及任务报告 |

先逐条检查的作用，是避免有问题的结构先占据代表位置，导致同组内另一个可用结构被提前折叠。结构匹配有容差，几何等价也不意味着两份性质证据完全相同。改进后的顺序能修复此类流程漏选；它本身不能证明材料性质预测更准确。

## 在 DeepSeek Harness 中使用

先确认 MatPilot 插件已启用。以下提示词可复制到 Harness，尖括号中的内容需要替换为真实 ID。无已有指标时使用第一种流程，不要让智能体把生成条件填成“测得性质”。

**只做结构与适用规则推荐：**

```text
请使用 MatPilot 插件，先读取 get_capabilities。
对已有任务 job_id=<真实任务ID> 调用 recommend_candidates：
selection_policy=triage_v2，material_class=unknown，limit=5，include_all_records=true。
这里的 unknown 是我尚未确定材料类别；若已有明确类别说明，请据其填写 ionic、metallic 或 covalent。
不要启动新的生成、预测、弛豫或 DFT。
请返回优先计算清单、每个未选候选的理由、是否输出不足，以及本次适用范围。
随后调用 build_report，job_id 不变，recommendation_limit=5，
recommendation_policy=triage_v2，material_class 与前一步相同。
报告说明推荐依据和未知证据，不把“优先计算”写成已验证稳定或材料性能提升。
```

**先选择评价方法，再按性质推荐：**

第一步只取方法与身份，不看有利数值：

```text
请使用 MatPilot 的 get_candidate_evidence，job_id=<真实任务ID>。
列出 method_groups 中的方法名、版本、权重身份、参照、单位、候选数量、
method_group_id 和 ambiguous_candidate_ids，暂不读取数值来挑选方法。
检查是否已有适合 energy_above_hull（eV/atom）的同方法、同参照证据。
不要调用模型，不要把缺失证据补成零，不要自动挑选最有利的指标。
```

第二步将按研究协议确定的方法组 ID 填入提示词：

```text
本轮采用 method_group_id=<已按研究协议确定的方法组ID>。
请从 get_candidate_evidence 返回的该组中复制明确的 metric_selections，
每项保留 job_id、candidate_id、structure_version_id、metric_id。
同一个候选存在多份指标时，先根据事先确定的来源/计算协议消除歧义；
无法消除时保持待复核，不按数值选最有利记录。
调用 recommend_candidates：job_id=<真实任务ID>，selection_policy=evidence_v1，
material_class=unknown，limit=5，include_all_records=true，
objective={"property":"energy_above_hull","unit":"eV/atom","direction":"minimize"}，
metric_selections 使用上面的完整身份列表。
不要运行生成、预测、弛豫或 DFT。报告采用的方法、排序理由、缺失证据和输出不足。
再调用 build_report，recommendation_limit=5，recommendation_policy=evidence_v1，
recommendation_objective 与上述 objective 完全相同，
recommendation_metric_selections 与上述身份列表完全相同，material_class 保持一致。
```

集合来源把 `job_id` 改为已登记的 `collection_id`，身份列表使用三个字段 `candidate_id / structure_version_id / metric_id`。目前 `build_report` 面向任务 `job_id`；集合推荐应保存返回的全量 JSON，不把集合 ID 填入任务报告接口。

若已经知道适用材料类别，用实际类别替换示例的 `unknown`。选择 `unknown` 会保留类别未知这一事实，不会推断出材料性质。

## 目标与硬性要求分开填写

`objective` 包含以下字段：

```json
{
  "property": "dft_band_gap",
  "unit": "eV",
  "direction": "minimize",
  "target_min": 1.5,
  "target_max": 2.0
}
```

这个例子表示先考虑落在 1.5–2.0 eV 范围内的已选证据；范围外按离范围的距离，再按指定方向排序。上下界是本次推荐偏好，不能替代原任务的硬性要求。若用户的原始条件是“必须满足某个界限”，应保留在任务需求台账中；推荐仍会排除未通过硬要求的结构。

`direction` 使用 `minimize` 或 `maximize`，单位必须与所选指标及性质目录一致，程序不会在这里偷偷换算单位。未选证据、缺失值、缺少方法或参照、未知/未收敛状态都不能获得一个默认“好分数”。

对于同性质多份有效指标，默认硬要求核验可能为 `unknown`。需要明确选取哪份指标来核验硬要求时，在 Harness 的 `screen_candidates` 或 `build_report.metric_selections` 中提供相应身份；这与排序用的 `recommendation_metric_selections` 是两套参数。排序偏好不能改变硬要求判定。当前 CLI 的 `screen` 使用默认核验，CLI `report --metric-selections-file` 对应的是推荐证据选择。

## CLI 操作步骤

下列命令用于已经安装 MatPilot 的 PowerShell 终端，`--config` 必须位于子命令前。`config.json` 指向当前操作者配置；本节只读取现有结果并生成推荐/报告，不提交计算。

```powershell
$MatPilotJobId = '<真实任务ID>'
matpilot --config .\config.json capabilities
matpilot --config .\config.json evidence $MatPilotJobId | Out-File -Encoding utf8 .\method-groups.json
```

阅读 `method-groups.json`，按评价协议确定方法组。以下代码只复制该方法组的身份列表；存在同候选多份指标时停止，避免自动选择有利数值：

```powershell
$MatPilotMethodGroupId = '<已按研究协议确定的方法组ID>'
$MatPilotCatalog = Get-Content -LiteralPath .\method-groups.json -Raw -Encoding UTF8 | ConvertFrom-Json
$MatPilotGroups = @($MatPilotCatalog.method_groups | Where-Object { $_.method_group_id -eq $MatPilotMethodGroupId })
if ($MatPilotGroups.Count -ne 1) { throw '没有找到唯一的方法组，请核对 ID。' }
if (@($MatPilotGroups[0].ambiguous_candidate_ids).Count -gt 0) { throw '存在多份指标，请先按来源协议明确选择。' }
ConvertTo-Json -InputObject @($MatPilotGroups[0].metric_selections) -Depth 10 | Out-File -Encoding utf8 .\metric-selections.json
```

创建 `objective.json`：

```json
{
  "property": "energy_above_hull",
  "unit": "eV/atom",
  "direction": "minimize"
}
```

按证据推荐，并保存全量结果：

```powershell
matpilot --config .\config.json recommend $MatPilotJobId --selection-policy evidence_v1 --material-class unknown --limit 5 --include-all-records --objective-file .\objective.json --metric-selections-file .\metric-selections.json | Out-File -Encoding utf8 .\recommendation.json
matpilot --config .\config.json report $MatPilotJobId --recommendation-policy evidence_v1 --material-class unknown --recommendation-limit 5 --objective-file .\objective.json --metric-selections-file .\metric-selections.json
```

报告命令返回 Markdown 与 JSON 的实际路径。报告按策略、推荐数量及相关选择信息保存；硬要求指标选择与推荐指标选择分别保留审计。

没有性质证据时，省略两份指标文件，使用结构/规则入口：

```powershell
matpilot --config .\config.json recommend $MatPilotJobId --selection-policy triage_v2 --material-class unknown --limit 5 --include-all-records
```

集合的对应命令是：

```powershell
matpilot --config .\config.json evidence-collection <已登记集合ID>
matpilot --config .\config.json recommend-collection <已登记集合ID> --selection-policy evidence_v1 --material-class unknown --limit 5 --include-all-records --objective-file .\objective.json --metric-selections-file .\collection-metric-selections.json
```

集合身份文件不需要 `job_id`；不要混用任务来源的四字段身份列表。

## 集合 schema 2 登记示例

集合由操作者登记。智能体只能选择 `get_capabilities` 中已有的集合 ID，不能临时提交任意 manifest 路径。

下例展示结构，所有尖括号均须替换；它是格式说明，不是可直接当真实证据使用的数据：

```json
{
  "schema_version": 2,
  "expected_elements": ["Na", "Cl"],
  "provenance": {
    "source": "<实际候选来源、批次与归档说明>",
    "evaluation_protocol": "<实际评价方法及版本来源>"
  },
  "records": [
    {
      "candidate_id": "<稳定候选ID>",
      "structure_version_id": "<原始结构版本ID>",
      "path": "C:/materials/archive/candidate.cif",
      "sha256": "<实际CIF的64位小写SHA256>",
      "parent_version_id": null,
      "transformation": "generated"
    }
  ],
  "metrics_by_structure_version": {
    "<原始结构版本ID>": []
  }
}
```

空数组表示该版本没有已登记指标，结构推荐仍可使用，性质排序会进入复核。已有指标应放入对应版本数组，完整保留 `metric_id`、候选与版本 ID、`property / value / unit`、`method`、`input_artifact`、`raw_output`、`convergence`、`reference_set`。不要只填一个性质数值或自行编造 `metric_id`。

`method` 必须明确记录 `name / version / weights_identity / parameters`，未知值用 `null`。两个 artifact 必须有绝对路径及实际 SHA256；评价输入的 SHA256 必须等于该结构版本的 SHA256。已有 `metric_id` 应按原内容核验，改变路径或元数据后不能继续沿用旧身份。`validate_metric` 是现有 Python 登记校验入口，正式导入应由操作者使用；推荐接口不会补算或生成缺失指标。

保存 manifest 后，将以下片段合并到当前操作者配置，保留其他已有配置：

```json
{
  "candidate_collections": {
    "nacl_archive": {
      "manifest_path": "C:/materials/archive/collection.json",
      "manifest_sha256": "<整个collection.json文件的64位小写SHA256>"
    }
  }
}
```

SHA256 可以用 PowerShell 计算：

```powershell
(Get-FileHash -LiteralPath 'C:\materials\archive\collection.json' -Algorithm SHA256).Hash.ToLowerInvariant()
```

manifest 改动后需要重新计算并登记校验值，再重载使用该配置的插件服务。`schema_version=2` 才接受 `metrics_by_structure_version`；候选和版本身份须唯一，指标不得绑定到集合外版本。集合上限为 256 个候选，每个版本最多 32 条指标。外部候选来源由 `provenance` 如实声明，当前运行后端不会改变其真实来源。

## 凸包指标、原始能量与准备度的区别

| property | 语义及允许范围 | 排序约束 |
|---|---|---|
| `energy_above_hull` | 按该评价协议得到的非负凸包距离；负值不符合本字段约定 | 同方法、同单位、同参照证据可用于跨组成排序 |
| `energy_above_reference_hull` | 相对一个固定参考凸包的带符号距离；候选低于该参考时可为负 | 必须保留明确参考身份与 SHA256；负值不能改名或截断成普通非负凸包距离 |
| `energy / total_energy` | 当前输入晶胞总能量 | 只允许同组成且同晶胞化学计量、同原子数比较 |
| `energy_per_atom / formation_energy` | 该方法的每原子能量或形成能 | 当前实现只在同组成内排序；跨组成统一排名拒绝 |
| `max_force / max_stress / max_abs_stress` | 明确定义的非负力或应力幅值 | 仅作为计算准备度线索，不等同于热力学稳定 |

两类 hull 指标都要求明确、非未知的参考身份和有效 SHA256。方法名、版本、权重、参数、单位或参考集合不兼容时，程序拒绝把它们放进一个全局排名。固定参考凸包的负值只说明低于该参考；参考数据是否完整、修正是否兼容、模型是否适用于该材料仍需科学审核。

## CHGNet 当前能做到哪一步

`prepare_property_prediction` 目前只为已登记结构准备本地静态预测计划。配置默认禁用，不自动下载权重，不运行预测，也没有向 Harness 暴露执行接口。它会报告 `disabled / weights_missing / weights_mismatch / environment_missing / unsupported / needs_review / ready` 等状态；`ready` 仅表示计划准备完成。

这里的材料类别词表与推荐路由不同，应按 `get_capabilities.property_prediction.operator_contract` 填写，例如 `ionic_crystal / metal_alloy / covalent_crystal`；结构状态填写 `original_generated` 或符合登记关系的 `relaxed`。不要把推荐接口的 `ionic` 直接当预测器参数。

后续操作者授权部署时，固定接口可输出每原子能量、最大原子力范数和最大绝对应力分量。它不直接输出凸包距离、带隙或离子电导。当前测试使用合成文件和注入进程结果，未完成真实 CHGNet 联调，也未证明科学准确率。权重、训练数据、验证领域、范围声明及接口细节见 [本地性质预测器契约](property_predictor_contract.md)。

## 如何判断一次推荐可用于后续计算安排

先核对 `requested_selection_policy / effective_selection_policy` 和材料类别路由，再检查实际输入数量、结构预检通过数、优先代表数和 `selected_count`。名额未满时应保留 `shortfall_count`，不能把复核候选静默补入优先名单。

性质排序还应核对所选方法组、原始结构版本、参照、`metric_selection_audit`、缺失候选和每条 `evidence_rank_key`。`all_candidate_audit_sha256` 用于追踪全量候选审计，所有未选指标保留在 JSON。选择身份或方法发生变化，应重新生成报告并说明变化。

当前最直接的产品价值是把人工初筛过程变成统一、可复核的操作链，并根据已有证据给出下一批优先计算对象。下一阶段的科学效果应使用未参与调参的多体系数据，按相同推荐名额或计算预算比较命中、漏选、未知和输出不足；真实用户流程提速则需另行测量操作时间。两类结果分别报告，不用软件测试通过率代替科学准确率。
