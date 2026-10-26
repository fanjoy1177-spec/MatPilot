from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .service import Service


class MetricSelection(BaseModel):
    """Explicit evidence identity; selection never happens by formula or value."""
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    structure_version_id: str = Field(min_length=1)
    metric_id: str = Field(min_length=1)


INSTRUCTIONS = """材策 MatPilot 材料研究插件。先调用 get_capabilities 获取当前模型能力，再把用户需求转换为 prepare_plan 的 request。
request 字段：raw_request, requirements(逐项原文需求), model_id='mattergen'或'diffcsp', template_id(DiffCSP所需，来自已登记模板), checkpoint(可省略), conditions, review_targets(字符串数组), num_samples(最大采样预算，默认16), valid_candidates_target(合格候选目标，可省略), batch_size(默认2), seed(默认42), unconditional(默认false), clarifications(字符串数组), search_mode(默认direct)。
自然语言请求必须提供 requirements，格式见 get_capabilities.requirements_contract。每条需求保留唯一 requirement_id、source 的 quote（优先只复制精确原文，程序定位；重复引文才提供 start/end）、kind、property、value、unit、strength、relation；位置按 Unicode 字符计数，end 不包含。程序生成每条要求的参数和检查映射。完整保留原始数字和单位，不能为了通过校验删去原文或缩短 raw_request。结构化入口没有原文时会记录为 structured_input，不伪装成完成自然语言理解。字段取值严格遵循 kind_rules：research_goal 必须 relation=goal，review_targets 可留空由需求记录自动生成。演示、先审批、仅计划、不得编造结论等是工作流指令，完整保留在 raw_request 并遵守当前宿主与后端策略，不把它们填成材料性质或虚构执行参数。
DiffCSP++ 只接受已登记并核对的模板，conditions 使用 chemical_formula 与 space_group，template_id 必须在需求中保留；不能从化学式猜测 Wyckoff 信息。repair_options 是待用户审阅的修改建议，不能自动套用。
固定化学式不能静默放宽为元素集合；若用户明确接受有限预算内先生成再筛选，可选择 search_mode='generate_then_filter'，仍保留化学计量后验检查且不保证得到足量合格结构。数值上下界没有适用评价器时不能承诺达标。
conditions 使用能力表中的字段，数字默认采用其标准单位，也可使用 {value,unit,relation:'target'}。chemical_system 用 Fe-Ni-O 这样的元素集合，不能把固定配比化学式偷偷改成元素集合。
完整保留用户约束，raw_request 应完整复制用户原文。不能擅自删除或弱化冲突条件；定性要求不得编造数值；上下界和区间不能假装成目标值。只有真正缺失、冲突或模糊的必需信息才放入 clarifications；不要为用户未提出的可选参数追加澄清。催化、耐腐蚀、合成等研究目标放入 review_targets 并解释后续验证需求，除非用户要求性能保证，否则这类后验目标不阻塞合法的生成方案。
prepare_plan 返回 blocked 或 needs_clarification 时不得提交。展示 ready 方案的条件、后验目标、数量和当前 demo/native/wsl 后端，取得用户对该方案的执行授权后，将相同 plan_id 作为 approved_plan_id 提交。重试同次提交必须复用 idempotency_key。程序记录计划匹配，不会证明自然语言授权真实发生，宿主仍需执行工具权限策略。
submit_job 立即返回任务号；长任务使用 get_job 查询。只能查询提交实际返回的 job_id，plan_id 不是 job_id，提交失败时不得编造任务号。失败先读错误，禁止自动扩大预算或放宽材料条件。只在明确终止状态下 resume。
get_results 返回 structure_versions 与 metrics_by_structure_version；指标只属于其明确的 candidate_id 和 structure_version_id，不得沿用父版本指标。需要比较性质证据时，compare_runs 可提供 metric_selections，每项包含 job_id、candidate_id、structure_version_id、metric_id，允许同一任务内显式选择多个版本。不得按化学式替代身份、自动选择最有利指标或对方法不可比的数值作材料优劣排名。
screen_candidates 和 build_report 可提供相同身份格式的 metric_selections，用明确选定的指标核验原始生成版本；未明确选择时，多份同性质有效指标保持 unknown。每个版本每项性质至多选一个指标，不能自动选择有利数值；所有未选指标保留在报告证据中。选择记录不改变采样预算或生成候选数，结论仅适用于所选方法和指标。
recommend_candidates 是只读候选后处理工具，job_id 与 collection_id 必须且只能提供一个。collection_id 只能来自 get_capabilities 的已登记集合；不能把任意目录或文件作为模型输入。集合来源独立于当前 demo/native/wsl 生成后端，已归档的真实候选不能误称为本轮生成或固定示例。默认推荐12个，最多256；默认仅返回入选记录与排除原因计数，需要全量审计才设 include_all_records=true。程序重新检查CIF身份、组成完整性、显式满占位和结构预检，去重后按组成/空间群/规模档位等权覆盖排序。优先弛豫名单仅为明确筛选偏好，结构预检通过仍待物性验证；不得宣称稳定、合成成功、性质优化或实际DFT费用降低。build_report 的 recommendation_limit 可把推荐及全量审计写入独立报告，不覆盖默认报告。
示例模式是固定 NaCl 样本，不能说成 MatterGen 或 AI 生成。执行完成不是材料性能验证。报告只使用工具返回证据；不得宣称已有催化性能、耐腐蚀性、实验验证、AI 准确率提升或流程提速百分比。"""


def make_server(settings: Settings) -> FastMCP:
    service = Service(settings)
    app = FastMCP("MatPilot", instructions=INSTRUCTIONS)
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

    @app.tool(annotations=read)
    def get_capabilities() -> dict:
        """获取当前模型、检查点、允许的条件组合、单位、计算上限与示例模式说明。"""
        return service.get_capabilities()

    @app.tool(annotations=write)
    def prepare_plan(request: dict) -> dict:
        """验证并保存完整任务方案，不执行生成。request.conditions.chemical_system 必须是 'Fe-Ni-O' 形式的字符串，不能用数组或固定化学式；review_targets 是字符串数组。数值可用 {value:1200,unit:'meV',relation:'target'}。source 优先只传精确 quote，由程序定位；字段与 relation 取值见 get_capabilities.requirements_contract.kind_rules，完整示例见 request_example。冲突不会自动丢弃。"""
        return service.prepare_plan(request)

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    def submit_job(plan_id: str, idempotency_key: str, approved_plan_id: str) -> dict:
        """提交已经获用户授权的 ready 方案；可能占用 GPU。重复提交复用同一幂等键。"""
        return service.submit_job(plan_id, idempotency_key, approved_plan_id)

    @app.tool(annotations=read)
    def get_job(job_id: str) -> dict:
        """查询持久化任务状态、实际完成数量、批次和错误；不把心跳延迟当作已终止。"""
        return service.get_job(job_id)

    @app.tool(annotations=write)
    def control_job(job_id: str, action: str) -> dict:
        """stop 请求停止；resume 只恢复已明确停止、失败或部分完成的任务。"""
        return service.control_job(job_id, action)

    @app.tool(annotations=read)
    def get_results(job_id: str) -> dict:
        """取得原始候选、每个结构版本及经工件校验的 metrics_by_structure_version；无指标的版本返回空数组，不继承父版本性质。"""
        return service.get_results(job_id)

    @app.tool(annotations=read)
    def screen_candidates(job_id: str, metric_selections: list[MetricSelection] | None = None) -> dict:
        """仅筛选原始生成版本，不计算性质。可显式选择1到80个指标身份进入逐项核验；每版每项性质最多一个，未选性质仍按默认规则检查，多值默认unknown。返回全部与实际采用的指标，不将变换版本计为新增候选。"""
        return service.screen_candidates(job_id, [selection.model_dump() for selection in metric_selections]
                                         if metric_selections is not None else None)

    @app.tool(annotations=read)
    def recommend_candidates(job_id: str | None = None, collection_id: str | None = None,
                             limit: int = 12, include_all_records: bool = False) -> dict:
        """对已有候选做只读后处理与计算规划，不生成、弛豫或优化物性。二选一提供job_id或已登记collection_id；limit为1到256，默认12。重新核验文件/组成/满占位/空间群及去重，按类别覆盖推荐。默认仅返回入选记录和排除计数，全量审计用include_all_records=true。"""
        return service.recommend_candidates(job_id, collection_id, limit, include_all_records)

    @app.tool(annotations=read)
    def compare_runs(job_ids: list[str], metric_selections: list[MetricSelection] | None = None) -> dict:
        """默认比较2到10个任务的数量和耗时。可选2到20个明确指标身份，核验方法可比性，此时允许单任务多版本或已知真实模型间采用同一评价协议；不自动选指标、不作材料优劣或模型性能排名，禁止混合示例与真实生成。"""
        return service.compare_runs(job_ids, [selection.model_dump() for selection in metric_selections]
                                    if metric_selections is not None else None)

    @app.tool(annotations=write)
    def build_report(job_id: str, metric_selections: list[MetricSelection] | None = None,
                     recommendation_limit: int | None = None) -> dict:
        """生成 Markdown 与 JSON。明确指标选择按哈希独立保存。可选recommendation_limit为1到256，将代表推荐与全量审计保存到独立recommendations/k子目录，不覆盖默认或指标选择报告；推荐仅用于后处理规划，不证明物性改善。"""
        return service.build_report(job_id, [selection.model_dump() for selection in metric_selections]
                                    if metric_selections is not None else None, recommendation_limit)

    return app
