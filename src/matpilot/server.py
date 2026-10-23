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
    def screen_candidates(job_id: str) -> dict:
        """仅对原始生成版本执行 CIF 解析、逐项要求、几何及去重检查；变换版本指标通过 get_results 读取，不计为新增生成候选，不计算材料性能。"""
        return service.screen_candidates(job_id)

    @app.tool(annotations=read)
    def compare_runs(job_ids: list[str], metric_selections: list[MetricSelection] | None = None) -> dict:
        """默认比较2到10个任务的数量和耗时。可选2到20个明确指标身份，核验方法可比性，此时允许单任务多版本或已知真实模型间采用同一评价协议；不自动选指标、不作材料优劣或模型性能排名，禁止混合示例与真实生成。"""
        return service.compare_runs(job_ids, [selection.model_dump() for selection in metric_selections]
                                    if metric_selections is not None else None)

    @app.tool(annotations=write)
    def build_report(job_id: str) -> dict:
        """生成含参数、版本、来源、完整事件和科学结论边界的 Markdown 报告与 JSON 证据。"""
        return service.build_report(job_id)

    return app
