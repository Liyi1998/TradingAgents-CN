"""Task-level investment horizon rules layered over database-owned agent roles."""
from __future__ import annotations

from app.core.analysis_horizon import get_horizon_profile


def render_horizon_contract(value: object = None, *, agent_key: str = "") -> str:
    """Resolve the selected period once; missing legacy values keep short behavior.

    Database role definitions remain authoritative for role expertise. This final
    task contract overrides only their historical short-only time requirements.
    """
    profile = get_horizon_profile(value)
    if profile.key == "short":
        return ""
    return f"""
【本次任务的分析周期：{profile.label}（{profile.holding_period}）】
analysis_horizon={profile.key}。此周期适用于全部分析师、多空辩论、研究裁决、交易员、风险辩论、最终裁决和摘要；当前节点：{agent_key or '研究节点'}。
本段覆盖前文角色模板中冲突的时间要求：未来3天逐日买入、未来7天逐日卖出、未来7天短期协同、短线≤7个交易日/中线≤30个交易日、到期机械退出均不作为本次必做或验收要求。数据库角色职责及证据、账户约束仍须遵守。
研究问题：在未来{profile.holding_period}内，哪些经营或估值变化支持持有，哪些事实会推翻逻辑？此期间是评估范围，不是承诺收益期限或到期强制卖出日期。
各角色围绕同一周期：基本面审查跨季度盈利、现金流、负债及经营趋势；估值区分财务事实、预测假设和方法，给出有依据的悲观/基准/乐观情景及敏感性；新闻/情绪识别持续催化和证据时点；技术/资金结合周月趋势评估建仓时机，短线资金仅作辅助，不能单凭短期流入/流出决定数月投资结论。每个角色只完成自身职责，综合节点负责汇总。
多空及风险辩论检验上述持有逻辑、反证、估值假设、催化兑现条件、流动性与回撤风险；不要把短线分时、5日资金或近期情绪缺失扩大为整个中长线研究无效。有证据的部分继续，缺项明确来源、截至日期、覆盖范围及受限结论，不假造不可获取的历史或未来预测。
交易员与最终报告应包含：①本次周期与投资逻辑；②经营/估值情景及假设；③持续催化和反证；④有依据的分批建仓参考区间、条件及失效条件；⑤持有、减仓或退出的事实条件；⑥复查节点（中线可按月，长线可按季度，并在财报/重大公告/逻辑失效时提前复查）；⑦数据缺口与结论置信度。未来事件日期未核验时按事件触发，不编造具体日程。
价格参考必须说明是技术位置、估值区间还是条件情景；区间无法可靠计算时保留定性方案并说明缺什么，不编造逐日价格、胜率或固定仓位。支持位短暂跌破须结合所选周期、既有风险规则及逻辑失效证据评估，不自动套用短线固定百分比止损，也不得扩大用户既有风险预算。
当前账户动作、未来条件方案和情景估值分别表达；选择中长线本身不产生买入许可、0%总仓位、默认清仓或放宽止损。摘要保留周期、区间及条件，多个估值情景不得压成单一必达目标；没有唯一且明确的目标价时数值 target_price 可为空，key_indicators 用带条件和周期的文字表述。
其他周期的历史报告或记忆只作标明日期与周期的参考证据，不直接沿用其买卖计划。
"""


def append_horizon_contract(system_prompt: str, value: object = None, *, agent_key: str = "") -> str:
    rules = render_horizon_contract(value, agent_key=agent_key)
    return system_prompt if not rules or rules in system_prompt else system_prompt + rules


def horizon_memory_name(name: str, value: object = None) -> str:
    """Keep existing short memories while isolating new medium/long experience."""
    horizon = get_horizon_profile(value).key
    return name if horizon == "short" else f"{name}_{horizon}"
