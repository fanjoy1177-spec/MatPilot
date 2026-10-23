# 36题候选夹具覆盖对照（合成、待审核）

本表只核验CPU回放情境。通过不表示AI答题正确、科学性质已证明或正式题库已审定。

候选版本 2；夹具关联 36/36；CPU通过 36；失败 0。生成路线缺口：当前定义内未发现。

| 题号 | 情境 | 输入、路线或状态 | CPU核验 | 待审问题 |
|---|---|---|---|---|
| T0101 | generation | MatterGen chemical_system=Fe-Ni-O；采样上限4；析氧活性无性质指标。 | passed | 仍待独立领域审定 |
| T0102 | generation | 共享已登记cubic_nacl模板；NaCl、221、Wyckoff a/b；采样2。 | passed | 仍待独立领域审定 |
| T0103 | generation | Na-Cl元素生成后按NaCl配比筛选；6次中包含配比不符样本，不能承诺6个合格候选。 | passed | 仍待独立领域审定 |
| T0104 | blocked_request | 保留原始要求并澄清或解释能力边界；无获准采样路线。 | passed | 仍待独立领域审定 |
| T0105 | blocked_request | 保留原始要求并澄清或解释能力边界；无获准采样路线。 | passed | 仍待独立领域审定 |
| T0106 | blocked_request | 保留原始要求并澄清或解释能力边界；无获准采样路线。 | passed | 仍待独立领域审定 |
| T0201 | generation | 1200 meV归一到1.2 eV引导值；2次；无带隙结果指标。 | passed | 仍待独立领域审定 |
| T0202 | generation | 80000 MPa归一到80 GPa，使用ml_bulk_modulus；2次。 | passed | 仍待独立领域审定 |
| T0203 | generation | Fe-O与凸包上能量0.05 eV/atom联合引导；最多4次。 | passed | 仍待独立领域审定 |
| T0204 | blocked_request | 保留原始要求并澄清或解释能力边界；无获准采样路线。 | passed | 仍待独立领域审定 |
| T0205 | blocked_request | 每原子口径已明确；50 meV/atom硬上限需后验评价证据，不能改成引导目标；无可保证此要求的采样路线。 | passed | 原题已写每原子凸包上能量，不能判为缺每原子口径。保持题干不变，修正gold允许从上下文读取meV/atom并换算；真正阻塞项是硬上限需要相容的后验评价证据，不能替换为引导目标。 |
| T0206 | blocked_request | 保留原始要求并澄清或解释能力边界；无获准采样路线。 | passed | 仍待独立领域审定 |
| T0301 | generation | 共享已登记cubic_nacl模板；NaCl、221、Wyckoff a/b；采样2。 | passed | 仍待独立领域审定 |
| T0302 | generation | Fe-Ni-O元素条件；2次；耐腐蚀性未评价，不新增带隙条件。 | passed | 仍待独立领域审定 |
| T0303 | generation | 已明确无条件生成；mattergen_base，空条件，2次。 | passed | 仍待独立领域审定 |
| T0304 | blocked_request | 保留原始要求并澄清或解释能力边界；无获准采样路线。 | passed | 仍待独立领域审定 |
| T0305 | blocked_request | 目录仍公开可查；未获用户选择或采用模板的授权，固定澄清保持未选择；无获准预测路线。 | passed | 公共目录实际有登记模板，因此不能假设无模板可查。新版明确允许读取和展示目录，但没有用户选定或授权采用的模板；固定回复不选择，保留必要结构规格/授权缺失而不对任一组隐藏目录。 |
| T0306 | blocked_request | 保留原始要求并澄清或解释能力边界；无获准采样路线。 | passed | 仍待独立领域审定 |
| T0401 | generation | 共享已登记cubic_nacl模板；NaCl、221、Wyckoff a/b；采样2。 | passed | 仍待独立领域审定 |
| T0402 | generation | Fe-O与凸包上能量0.04 eV/atom联合引导；最多2次。 | passed | 仍待独立领域审定 |
| T0403 | generation | 磁密度0.1 mu_B/angstrom^3与HHI 0.2联合引导；2次。 | passed | 仍待独立领域审定 |
| T0404 | blocked_request | 保留原始要求并澄清或解释能力边界；无获准采样路线。 | passed | 仍待独立领域审定 |
| T0405 | blocked_request | 保留原始要求并澄清或解释能力边界；无获准采样路线。 | passed | 仍待独立领域审定 |
| T0406 | blocked_request | 保留原始要求并澄清或解释能力边界；无获准采样路线。 | passed | 仍待独立领域审定 |
| T0501 | candidate_read | 两个NaCl候选的独立ID/版本及不同带隙指标；只选c_b。 | passed | 仍待独立领域审定 |
| T0502 | candidate_read | c_a的s_a1/s_a2版本别名映射、父版本和合成弛豫变换，指标分别1.1/1.6 eV。 | passed | 仍待独立领域审定 |
| T0503 | candidate_read | 相同结构经整体平移产生不同CIF字节；真实StructureMatcher应识别一组。 | passed | 仍待独立领域审定 |
| T0504 | candidate_read | 同单位但不同势模型的两份能量证据；不提供可比稳定性排序。 | passed | 仍待独立领域审定 |
| T0505 | candidate_read | 有来源的合成数据库查询超时记录；novelty保持unknown，查询结果null。 | passed | 仍待独立领域审定 |
| T0506 | candidate_read | 可解析、元素和几何初筛通过的Fe-Ni-O样本；无催化/腐蚀性质指标。 | passed | 仍待独立领域审定 |
| T0601 | generation | 新版明确授权MatterGen基础无条件生成；总尝试≤3、每批≤2，不强制用尽。CPU固定探针为2+1，非唯一合法分批或评分答案。 | passed | 原题没有生成方式授权，gold又把每批最多2过度限定为必须2+1。新版显式授权MatterGen基础无条件模式，保留总尝试≤3、每批≤2；2+1、1+1+1等合法分批都可接受，上限不要求用尽。不向原题追补授权，也不按当前实现选择唯一分批答案。 |
| T0602 | execution_read | 3次已有尝试：2个已归档结构+第3次失败记录；state=partial。 | passed | 仍待独立领域审定 |
| T0603 | execution_read | 2个已归档结构；停止已确认且来源记录为stop_requested，不重新生成。 | passed | 仍待独立领域审定 |
| T0604 | execution_read | 2次预算均已使用，1产物1失败；remaining=0，不重置预算。 | passed | 仍待独立领域审定 |
| T0605 | execution_read | 心跳迟到但termination未确认，state=running；不得认定退出或重复启动。 | passed | 仍待独立领域审定 |
| T0606 | execution_read | 原CIF与附注释后的观察字节，实际哈希不符；旧指标/报告不能当成当前证据。 | passed | 仍待独立领域审定 |
