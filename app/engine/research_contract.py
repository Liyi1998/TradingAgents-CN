"""Research assessments are not account approvals or executable orders."""
from __future__ import annotations

import re

RESEARCH_OUTPUT_RULES = """
【研究输出与账户执行分离】
本系统生成学习研究报告，不执行订单，也不要求虚构一个会外风控委员会或审批人。
单项缺失仅限制依赖该项的判断；有证据的价格、技术结构、财务趋势和定性风险继续分析。
WATCH/不新增不等于不能提出条件式研究方案。已有可追溯日线支撑阻力时，可给出条件入场参考、技术失效/保护参考、条件目标，以及未来3/7个实际交易日的观察方案；写清来源、计算、触发条件、失效条件和缺项，非预测必达、非当前买卖委托。不为每日编造漂移的价格或胜率。
技术目标不同于基本面公允价值；未获当前买入建议不同于没有研究参考价。账户成本/股数/净值/原规则缺失，只限制个人损益、股数、仓位适配与订单，不应单独禁止非个性化技术风险界限。不得猜测或放宽用户既有风控。
未来尚未发生的回测确认标为触发条件，不声称已经发生；缺少历史策略回测时不报胜率，但可计算明确假设入场/退出/目标的几何盈亏比并注明非收益优势证明。
给出有依据的综合定性风险和研究置信度，说明评估范围与局限；模型自评可量化但必须标为主观自评、非校准概率/胜率，不用默认50/70填空。确实不能评估时写具体原因，不以部分可选数据缺失把全部结论置未知。
当前分账户动作仍服从最终裁决；被否决的当前买卖/仓位不能升级为生效指令。条件研究方案必须与当前动作分栏表达。
"""


def append_research_contract(
    system_prompt: str, analysis_horizon: object = None, *, agent_key: str = ""
) -> str:
    """Share the same runtime boundary across analyst and later-stage entry points."""
    from app.core.analysis_horizon import normalize_analysis_horizon
    from app.engine.analysis_horizon import append_horizon_contract

    horizon = normalize_analysis_horizon(analysis_horizon)
    rules = RESEARCH_OUTPUT_RULES
    if horizon != "short":
        rules = rules.replace("以及未来3/7个实际交易日的观察方案", "以及所选分析周期内的观察与复查方案")
        # Remove a previously appended legacy contract when a caller explicitly
        # selects a longer period; never retain contradictory task instructions.
        system_prompt = system_prompt.replace(RESEARCH_OUTPUT_RULES, "")
    if rules not in system_prompt:
        system_prompt += rules
    from app.engine.wyckoff_method import append_method_contract
    return append_method_contract(append_horizon_contract(system_prompt, horizon, agent_key=agent_key), agent_key)


def qualitative_confidence(text: str) -> str | None:
    """Keep an explicitly stated judgement without manufacturing a number."""
    match = re.search(r"(?:决策|评级|裁决|整体|研究)?置信度[|｜：:\s*]*(中低|低至中|中高|中至高|较低|较高|低|中|高)(?=[|｜；。\n\s*]|$)", text or "")
    return match.group(1) if match else None
