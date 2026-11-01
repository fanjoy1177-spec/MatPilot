# 候选代表推荐：从结构文件到可审计的后续计算清单

本版合并后的默认入口为 `auto`：无额外证据或材料类别参数时读取操作者 `automatic_ranking_policy`（安装默认 `ionic_mixed_valence_v1`）；明确 `objective` / `metric_selections` 或 `material_class` 时转入已有的 `triage_v2` / 证据管线。显式 `triage_v2`、`structural_v2`、`ionic_mixed_valence_v2`、`evidence_v1` 与历史策略均保留。真实指标证据不会被学到的代理分数覆盖；显式 `ionic_ranked_v2` 与证据或材料类别参数组合会明确报错。

MatPilot core `0.2.0.dev9` 新增第 10 个 MCP 工具 `recommend_candidates`。它读取已有 CIF，核验身份、过滤不满足条件的结构、去重，再给出有限数量的代表候选及选择理由。推荐不执行 MatterGen、弛豫或 DFT，也不调用 LLM API；Harness 中的自然语言交互费用由宿主模型单独产生。

该功能简化的是“拿到模型输出后，逐个检查、去重、整理下一批计算对象”的流程。它不修改原始候选，不能据此宣称生成模型、材料稳定性、催化性能或实验结果得到提升。

## 当前自动策略：跨体系混合价态检查

推荐默认使用 `selection_policy="auto"`。程序先完成 CIF 身份核验、组成与满占位检查及代表去重，再根据**声明的目标元素集合**选择策略：

| 策略 | 用途与适用范围 |
|---|---|
| `auto` | 支持的离子体系使用 `ionic_mixed_valence_v1`；不支持时明确回退 `structural_v1`，返回回退原因 |
| `ionic_mixed_valence_v1` | 在已声明价态模型内检查整数电荷分配，允许同一种金属使用多种正价态，再从优先代表池做多样性推荐 |
| `structural_v1` | 通用结构检查、去重与覆盖推荐，不作电荷可行性判断 |
| `fe_ni_o_relaxation_v1` | 保留旧 Fe-Ni-O 统一价态启发式，用于复现历史结果，不作为新的自动默认 |

新规则目前要求恰有一种注册阴离子：O/S/Se 按 -2、F/Cl/Br/I 按 -1、N 按 -3；其余元素须为规则支持的金属阳离子。金属正价态采用运行环境中 pymatgen 的公开 ICSD 价态先验，没有可用正价态时再尝试 common 价态；实际采用的元素、价态和目录来源随结果记录。氧化物中的 Fe、Co、Ni、Mn、Li 及符合该模型的卤化物、硫化物、氮化物可进入这一流程。它并不涵盖所有氧化物、共价材料或复杂阴离子体系；例如多种注册阴离子共存、金属合金、非金属阳离子或没有可用先验时应查看明确的回退或未知说明。

程序求解的是有限价态集合内的整数电荷可行性。以 Fe3O4 为例，允许同一元素的不同位点采用不同价态，避免因为“整种 Fe 只能选一个价态”而漏掉形式电荷可行的组成。结果中的分配见证只证明这种整数分配在规则内存在，**不代表预测了原子真实价态、混合价态比例、稳定性或催化性能**。

结构门通过且电荷检查为 `feasible` 的代表可进入优先弛豫池；`no_solution` 只表示在所列价态模型内没有解，不能据此认定材料不可能。未知或模型不适用的候选需要复核；不为凑数量而补齐。新策略不沿用旧 Fe-Ni-O 的密度、体积与 0.8 Å 罚分，也不产生物性评分；通用结构门仍核验小于 0.5 Å 的周期碰撞。旧几何启发式只在显式 legacy 策略中保留。

可以向 Harness 提出：“对这批 Li-Co-O 或 Fe-Ni-O 候选检查结构与混合价态电荷可行性，推荐最多 12 个代表，列出分配依据和需要人工复核的原因；不要执行弛豫。”宿主仍需使用真实任务 ID 或能力表中登记的集合 ID：

```json
{"collection_id":"已登记集合ID","limit":12,"selection_policy":"auto","include_all_records":true}
```

返回的 `requested_selection_policy`、`effective_selection_policy`、`selection_policy_resolution` 和 `fallback_reason` 明确说明实际执行路径。回退到结构推荐时，电荷可行性保持未评估，不能把“没有运行规则”解释成“已通过化学检查”。逐候选的 `relaxation_priority.metrics.charge_feasibility` 保留求解状态和可行性见证；外部性能标签不会用于改写这些检查。

任务报告使用相同 `recommendation_policy`，按策略保存独立审计。显式旧策略可复现历史行为，新旧结果不能混成同一次独立验证。

## 历史 dev10：自动 Fe-Ni-O 统一价态分级

以下描述历史 core 0.2.0.dev10，当前默认策略以上一节为准。该版本的推荐默认使用 selection_policy="auto"。目标元素恰为 Fe、Ni、O 时，自动执行结构核验、代表去重、优先弛豫分级、优先池多样性推荐。其它元素体系继续使用 structural_v1；不能把 Fe-Ni-O 的规则套用到其它材料。

可以在 Harness 中说：“把本轮 Fe-Ni-O 候选先分成优先弛豫、人工复核、暂缓，再从优先池选4个覆盖不同组成和空间群的代表，解释每个入选及排除原因，不执行计算。”实际调用：

    recommend_candidates(collection_id="mattergen_fresh_20261026", limit=4, selection_policy="auto", include_all_records=true)

显式 selection_policy="structural_v1" 可重放 dev9 的宽松结构检查加排序；selection_policy="fe_ni_o_relaxation_v1" 强制执行专用分级，不适用的元素体系会报错。job_id 和 collection_id 都支持同一策略。生成后的推荐仍由工具调用触发；分级和排序已在同一个推荐调用内部执行，不需要用户预先填写优先名单。任务报告可传 recommendation_limit=4 和 recommendation_policy="auto"，专用策略写到独立策略目录。

分级规则移植自本项目旧 MG-Flow 的明确启发式：初始100分；严重短距<0.8 Å扣50，否则共价半径比<0.65扣25；每原子体积不在[5,40] Å³扣20；密度不在[1,12] g/cm³扣15；统一Fe/Ni(+2,+3)、O(-2)价态组合未找到电荷平衡扣15。已知分数至少85且该价态提示通过时为“优先弛豫”，否则至少60为“人工复核”，低于60为“暂缓”；未知测量不能进入优先池。组成、占位等结构检查不合格的记录单独保留。

这些是下一步工作流建议，不是物性评分。统一价态提示没有考虑同元素混合价态，失败仅表示需要复核，不能据此认定材料不可能、无效或不可合成。程序修正了旧脚本遗漏周期自镜像的距离检查，仍不做弛豫、能量或DFT计算。数量不足就返回不足，不补算、不把人工复核候选自动提为优先。

本轮补充验证沿用同12个新生成候选，并非新增独立实验：11个通过结构检查，5个优先弛豫、6个人工复核，从优先池选出4个。插件为4组成/4空间群/2规模档/46输入位点；最小结构基线为3/4/2/43，顺序基线与插件集合相同。多覆盖1种组成的代价是增加3个位点，不能解释为费用或物性改善。证据见 artifacts/triage-candidate-comparison-20261026.json；旧dev9结果保留，不将新46与旧40跨不同资格池计算优化率。

以下为通用结构策略与注册接口说明。当前 auto 以首节的支持范围与混合价态规则为准；上述 dev10 数据保留为历史参考。

## 在 Harness 中使用

确认原生插件已经连接包含 **ionic_mixed_valence_v1 策略的 Python 核心**。仅更新插件界面包而仍指向旧版 Python 目录，不会增加新工具。重新连接后先调用 `get_capabilities`，查看版本、`candidate_recommendation` 和其中的 `collection_ids`。

可以输入：

> 对任务返回的候选做代表结构推荐，最多保留 12 个。说明组成、空间群和计算规模覆盖，列出排除原因；不要执行新的生成或弛豫，也不要把推荐说成材料性能更好。

宿主调用的工具参数二选一：

```json
{"job_id":"实际任务ID","limit":12}
```

```json
{"collection_id":"已登记集合ID","limit":12}
```

`job_id` 必须来自实际任务，`collection_id` 必须来自能力表；不能同时提供，也不能把文件夹路径当作集合 ID。`limit` 为 1–256，默认 12；合格代表不足时返回实际数量，不补造结构。常规生成任务的采样上限仍由原有配置控制，此推荐上限不会增加采样预算。

默认返回入选记录、排除原因计数、覆盖度、算法参数和全量候选审计 SHA256。需要逐个复核全部候选时，另加 `"include_all_records":true`。审计哈希标识本次完整核验记录；它不是材料性质认证。

## 检查与排序规则

1. 核对每份 CIF 的 SHA256、唯一候选 ID、单一数据块和解析完整性。
2. 从 CIF 原子位点重新读取组成；元素集合须与任务或登记集合明确声明的目标完全一致。缺少或多出元素均排除，不从生成结果反推“用户目标”。
3. 要求 CIF 明确给出满占位，并能解析为有序结构。占位缺失、部分占位或无序结构不进入本版推荐池；这是一项工作流适用范围限制，不意味着这些结构没有研究价值。
4. 检查正晶胞体积及小于 0.5 Å 的周期性短接触，包括同一原子的周期镜像。该宽松预检不是能量、化学合理性或稳定性判定。
5. 重新分析空间群，按指定公差做结构等价分组。未知结果排除，不填成 P1，也不当作“已经确认不同”。任务来源还必须通过既有硬性要求检查，`unknown` 不算通过。
6. 仅从合格代表中选择。每一步按新增的**约化组成、空间群、输入晶胞原子数档位**覆盖计分，权重固定为 1:1:1；平手先选较小晶胞，再按候选 ID 排序。档位为 1–10、11–20、21 个及以上原子。

集合模式的“组成完整”仅指目标元素集合完整，不保证固定化学计量，也不自动核验对话中新追加的性质要求。任务模式则沿用任务已有的逐项硬性要求；不得把 Fe–Ni–O 元素集合合格表述成已经满足 FeNiO2 精确化学式。

空间群是离散类别，不把空间群编号之间的差值解释为结构相似度。推荐的对称分析使用 `symprec=0.1 Å`、角容差 `5°`；既有硬性空间群核验使用 `0.01 Å`。比较结果时应保留各自公差，不能把两者视为同一测量。结构等价检查使用 `ltol=0.2`、`stol=0.3`、角容差 `5°`，允许原胞处理与比例缩放，不进行超胞搜索。

推荐中的原子数来自输入 CIF，可能不同于历史标准原胞原子数。它只表示晶胞规模，不能直接换算成实际 GPU/DFT 时间、费用或物理优劣。贪心覆盖也不保证全局最优。

## 登记已有候选集合

由操作者在 MatPilot 配置 JSON 中登记；LLM 不能借此添加任意目录。配置示意：

```json
{
  "candidate_collections": {
    "campaign-a": {
      "manifest_path": "collections/campaign-a.json",
      "manifest_sha256": "替换为清单文件实际的64位小写SHA256"
    }
  }
}
```

清单示意：

```json
{
  "schema_version": 1,
  "expected_elements": ["Fe", "Ni", "O"],
  "provenance": {"kind": "archived_mattergen_candidates", "run_id": "实际运行标识"},
  "records": [
    {"candidate_id": "candidate-001", "path": "cifs/candidate-001.cif", "sha256": "替换为CIF实际的64位小写SHA256"}
  ],
  "priority_candidate_ids": ["candidate-001"],
  "priority_selection_basis": "明确说明选择依据及原始记录来源"
}
```

示例哈希占位文字必须替换，不能直接使用。清单路径相对配置文件目录解析，CIF 路径相对清单目录解析。清单最多 256 条，每个 CIF 最大 4 MiB。每次推荐均重新验证清单及 CIF 哈希；文件更新后应由操作者重新审核并登记哈希。

`priority_candidate_ids` 和 `priority_selection_basis` 是可选的明确筛选偏好，不是物性结论。只有来源记录确实存在时，才能称其为“历史优先弛豫名单”；未提供时不会伪造该标签。重新通过的结构仍只是“结构预检通过、待物性验证”。

## 命令行及报告

```text
matpilot --config config.json recommend 实际任务ID --limit 12
matpilot --config config.json recommend-collection campaign-a --limit 12
matpilot --config config.json recommend-collection campaign-a --limit 12 --include-all-records
matpilot --config config.json recommend-collection campaign-a --limit 12 --selection-policy ionic_mixed_valence_v1
matpilot --config config.json recommend-collection campaign-a --limit 12 --selection-policy fe_ni_o_relaxation_v1
matpilot --config config.json report 实际任务ID --recommendation-limit 12
```

Harness 的 `build_report` 可传 `recommendation_limit` 和 `recommendation_policy`。显式通用结构策略写入报告目录的 `recommendations/12/`；离子或旧 Fe-Ni-O 策略写入 `recommendations/实际策略ID/12/`，保存推荐表及全量审计。有显式指标选择时，推荐子目录位于该选择对应的报告目录内，不覆盖默认或其他选择的报告。当前 `build_report` 仍以 `job_id` 为入口；登记集合的推荐返回结构化 JSON，不会自动写入任务报告。

## 对比证据如何解释

- **2026-10-26 本轮新生成数据**：必须用本轮真实运行记录、CIF 哈希和实际完成数量建立对比；生成、核验或推荐尚未完成时，不填写推测数字，不用旧样本补成新结果。
- **2026-07-23 历史 128 候选参考池**：属于已归档的 MatterGen 输出。可在同一批文件上比较原始顺序、简单规则、随机选择、旧 MG-Flow 流程与新推荐策略，但必须标成历史同池重放，不能说成本轮通过插件新生成。
- **固定 NaCl 演示样本**：只验证软件链路，不能作为真实 MatterGen 生成质量或新能源材料效果证据。

候选来源由任务或清单记录给出，与当前连接配置的 `demo/native/wsl` 后端分别说明。已有材料筛选数据、独立的新生成数据及固定演示数据不能混合统计成一次实验。

同池对比能说明排除情况、重复结构比例、组成/空间群/规模覆盖、可复现程度以及记录的交互步骤。实际材料性质改善、人员操作时间缩短和总计算费用降低仍需要相应的独立测量。旧 MG-Flow 已有候选多样性选择流程，本次插件的贡献在于把这些步骤接入工具调用、统一核验与审计，并提供通用且明确的选择规则。

## 显式优先池排序：ionic_ranked_v2

`ionic_ranked_v2` 完整沿用 dev11 的结构资格、去重、调用者显式偏好和混合价态门控，只对同一优先代表池重新排序。需要人工复核或未通过预检的结构不会因为分数较高而进入推荐，也不会为凑数量放宽资格门。安装默认 `auto` 仍使用 `ionic_mixed_valence_v1`。操作者可在配置中将 `automatic_ranking_policy` 设为 `ionic_ranked_v2`，能力表会报告当前值；配置接受 ionic_mixed_valence_v1、ionic_ranked_v2、ionic_complexity_v3 三个值；推荐工具参数不能修改该配置。显式新策略的存在不代表新的独立验证已通过，本项目的本机默认切换须按预先冻结的部署判据执行，无需为切换而修改已经冻结的排序代码。

该排序使用历史开发集低凸包距离标签训练的固定参数。**训练使用标签，推理不读取候选真实性能标签**，因此不能写成“整个方法没有使用性能标签”。逐候选的未校准相对分数用于比较当前优先池中的对象，不是稳定概率、凸包距离的预测数值或已验证的弛豫成功率；数据库中已弛豫结构的验证也不能直接推广到新生成结构或实验性能。

结果顶层 `priority_ranking` 记录固定模型身份、哈希、训练信息、覆盖调整与回退原因；逐候选 `ranking_evidence` 保存特征、标准化值、贡献和相对分数。模型训练覆盖 O/F/S/N 阴离子体系；其他受混合价态门支持的阴离子可完成资格检查，但相对排序会整体回退到 dev11 覆盖选择，并记录超出训练域的警告。`relaxation_priority` 仍单独解释 dev11 资格门，不把训练分数写回资格门。特征不可用时按记录的规则回退到 dev11 的覆盖排序，必须保留回退警告，不能将未知分数填成可靠预测。

```json
{"collection_id":"已登记集合ID","limit":12,"selection_policy":"ionic_ranked_v2","include_all_records":true}
```

任务报告传 `recommendation_policy="ionic_ranked_v2"`，独立写入 `recommendations/ionic_ranked_v2/数量/`，保留相对评分、覆盖结果、警告和全量审计。它不会覆盖默认报告或 dev11/legacy 策略报告。该调用只读取现有候选，不生成结构、不执行弛豫或 DFT。


## 实验性约化式排序：ionic_complexity_v3

本策略保留 dev11 的结构与混合价态资格门及推荐名额，只在相同优先池中按约化整数化学式的原子总数降序排序，同值时按已核验 CIF SHA256、候选 ID 排序。复制晶胞不改变主排序值；文件内容变化可改变 SHA256 平局顺序。规则选择参考过历史开发标签，没有拟合模型，推理不读取物性标签，也不输出物性分数或稳定概率。

显式传入 `selection_policy=ionic_complexity_v3` 可使用此规则；`automatic_ranking_policy` 配置也接受它，但安装默认仍为 `ionic_mixed_valence_v1`。不合法组成或文件身份导致整池回退旧排序，并给出原因。输出 `priority_ranking` 和逐条 `ranking_evidence` 保留约化式计数、启发式键、旧选择和回退说明。

`triage_v2`、`structural_v2`、`ionic_mixed_valence_v2`、`evidence_v1`、材料类别和指标选择能力均保留。`auto` 遇到明确材料类别或性质证据时优先进入这些管线，不使用启发式替换已有可比性质证据。显式实验排序与新增材料类别/性质排序参数组合会明确报错，避免静默丢弃参数。

本版本整合两条开发工作；安装和发布仍需分别验收。跨体系筛选效果见独立冻结验证结果；软件测试和两例开发回归不构成独立科学证明。
