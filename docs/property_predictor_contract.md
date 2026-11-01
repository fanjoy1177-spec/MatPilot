# 本地性质预测器契约 v1

状态：独立进程适配器与软件保护测试已实现；本轮未安装、下载或执行真实 CHGNet。当前业务入口只准备计划，不向智能体暴露执行接口。测试中的权重、力、应力和能量均为明确标记的合成软件夹具，不能作为材料预测效果或推荐准确率证据。

## 官方接口与固定范围

本版固定 Python 包 `chgnet==0.4.2`、checkpoint 架构版本 `0.3.0`，同时要求操作者提供本地权重文件和 SHA256；包版本与 checkpoint 版本是两个字段。固定 worker 使用 `CHGNet.from_file(...)` 加载本地文件，并调用 `predict_structure(structure, task="efs", batch_size=1)`。官方接口返回能量、原子力和应力；本版不调用弛豫、分子动力学或 DFT。接口和单位以 [CHGNet v0.4.2 源码](https://github.com/CederGroupHub/chgnet/blob/v0.4.2/chgnet/model/model.py) 为准，包发行记录见 [PyPI 0.4.2](https://pypi.org/project/chgnet/0.4.2/)。

`0.3.0` 默认 checkpoint 使用 MPtrj 预训练；这不证明任意本地 checkpoint 的训练身份。操作者仍须登记 `training_data_identity`、权重来源和验证领域。官方 [模型说明](https://github.com/CederGroupHub/chgnet/tree/v0.4.2#available-pretrained-models) 区分 MPtrj、r2SCAN 等模型，不能把不同训练方法的能量混为一组。当前适配器只接受上面固定的架构版本。

## 集成接口

| 函数 | 作用 |
|---|---|
| `predictor_config_schema()` | 返回操作者配置字段、固定版本、适用范围词表和输出能力 |
| `validate_predictor_config(config=None)` | 校验配置；默认 `enabled=False`，拒绝任意命令、版本漂移和伪造校准声明 |
| `prepare_static_prediction(record, config, work_root, *, material_class, structure_state)` | 解析并校验登记结构，只准备固定 worker、输入 manifest、精确 argv 和计划 SHA256；不起进程 |
| `execute_static_prediction(plan, config, work_root, *, runner=None)` | 操作者专用执行函数，执行前重新核验计划、输入、权重和路径；当前不暴露给 LLM 工具 |

`record` 必须由服务按已登记 `candidate_id / structure_version_id` 查得，含实际 `path / sha256` 和版本关系。`config`、`work_root`、解释器路径和 checkpoint 路径来自操作者配置。智能体不能提交任意命令、路径或 Python 代码来替代这些配置。

准备结果有 `disabled`、`weights_missing`、`weights_mismatch`、`environment_missing`、`unsupported`、`needs_review` 或 `ready` 状态；多个问题完整保留在 `blockers` 中。`ready` 仅表示本地计划准备完成，独立环境中的包版本仍在 worker 启动时核验，不能写成真实模型联调成功。准备出的文件固定为 `input_manifest.json`、`worker.py`，后续执行原始结果为 `raw_prediction.json`。

`execute_static_prediction` 使用固定 argv、`shell=False`、隔离 Python 参数 `-I`、超时及可信工作目录。现有原始输出不覆盖；输入、权重、配置或 worker 改动会使原计划失效。`runner` 仅是软件测试注入点，带此参数的成功结果明确标记为 `injected_runner_contract_test` 和 `real_prediction_performed=False`，不可记为真实科学计算。

## 操作者配置

必填到可以执行的配置包括：绝对路径 `python_executable / checkpoint_path`，`checkpoint_sha256 / checkpoint_source`，`training_data_identity`，以及 `scope` 和 `validation_domain`。不配置这些字段仍可保留默认禁用状态。

`scope` 使用显式元素列表、材料类别列表、结构状态列表和原子数上限，不接受全元素通配符。类别词表包括离子晶体、金属/合金、共价晶体、分子晶体和多孔晶体；列在词表中只代表接口能表达该类别，操作者必须逐项声明自己有依据的适用范围，不代表 CHGNet 在所有类别上均已验证。

实际元素、原子数和有序占位从 CIF 读取；声明状态还要与登记的原始/弛豫版本关系一致。材料类别仍是显式声明，当前没有通用可靠的自动分类器。结构落在声明范围内只能记为 `within_declared_scope`，不能记为已独立校准。

`validation_domain` 必须记录 `status / description / source`。未知状态不运行预测；`declared` 说明操作者已声明领域，并不表示程序验证过论文或独立测试。`uncertainty` 固定为 `not_calibrated`，模型差异、单次预测或范围检查都不转成“稳定概率”。

官方本地加载接口会读取 PyTorch checkpoint。其 SHA256 固定的是文件身份，不能替代来源可信性核验；因此只有操作者配置的可信本地权重可进入执行路径，插件不接收会话附件作为可执行 checkpoint，也不自动下载。

## 输出与排序连接

| 输出 property | 定义 | 可支持的判断 |
|---|---|---|
| `energy_per_atom` | 静态模型能量，eV/atom | 同组成、同方法的相对能量排序；不能直接跨组成排序 |
| `max_force` | 所有原子的力向量欧氏范数最大值，eV/angstrom | 计算准备度线索；不是稳定性和弛豫成功证明 |
| `max_abs_stress` | 3×3 应力张量分量的绝对值最大值，GPa | 计算准备度线索；不是静水压力或独立稳定性指标 |

原始力和应力数组完整保留。数值、数组形状、单位、输入 SHA、候选与版本、包版本和 checkpoint SHA 都必须通过核验，才生成已有 `validate_metric` schema 的记录。静态推断的 `convergence=not_applicable`，不标记为弛豫收敛。返回指标不会自动写入数据库；后续登记应由服务执行，保留输入及原始输出证据。

这些输出没有相图参考集合，`reference_set=None`。本适配器不产生 `energy_above_hull`、`energy_above_reference_hull`、形成能、带隙或离子电导；已有生成目标也不能充当这些性质的已测值。跨组成稳定性筛选仍需要兼容方法与明确参考集合的凸包证据。

当前 `evidence_ranking` 只对既有指标的身份、原始文件、方法、权重、参照与收敛元数据进行校验。它与预测器的适用范围检查尚未自动联动，也不会追溯验证任意外部导入指标的材料类别。因此“指标元数据可比”不能写成“预测器领域已验证”。本适配器执行前已经检查自己的声明 scope，并把 scope、训练数据、验证领域和未校准声明写入 `method.parameters`；后续扩展应在统一指标登记契约中加入可审计的适用性证明，随后再接入排序门槛，不能默认为外部记录已经满足。

## 已验证与剩余验收

软件测试覆盖：默认禁用、缺失/错误权重、缺解释器、越界元素/类别/状态/规模、未知训练或验证领域、版本漂移、命令/路径篡改、输入/权重改动、跨候选输出、超时和失败、已归档结果不可覆盖，以及合成原始输出到三类指标的身份绑定。测试不导入 CHGNet、不加载模型、不给出科学准确率。

实际部署仍需在已授权环境完成依赖安装、可信权重登记、真实静态 smoke test，以及与独立标签对应的多体系评估。真实计算、费用和准确率应另记证据；本轮实现不能替代这些验收。
