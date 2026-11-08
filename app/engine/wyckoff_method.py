"""Shared methodology, not an automatic phase or trading-action classifier."""
import re

METHOD_VERSION = "wyckoff-method-v2"
METHOD_CONTRACT = """
【威科夫方法统一约束 v2；优先于角色模板中的四阶段及阶段→动作套用】
威科夫涉及趋势背景、区间结构、事件和阶段假说四层，不能混为一个标签。
1. 分列趋势背景（上涨/下跌/横盘/不明）、带日期的事件、当前阶段假说和确认状态。循环包含吸筹、拉升、派发、下跌（Mark Down）；试盘是测试行为，不是固定的第二阶段。不要把下跌写成仍在派发，也不要把一个SC/AR等事件当作整个吸筹阶段成立。
2. SC是候选停止行为，AR是随后的反弹，ST是后续测试；暴跌后的反弹也可能属于再派发或下跌中继。单日反弹、缩量或整数关口收复均不能独自证明吸筹、真实主力抢筹或反转。没有SC也可能形成底部，不强行拼满教科书事件序列。
3. Spring需说明此前已经形成的交易区间、支撑依据、下探和收回的日期与价格，以及后续测试；没有区间时只能描述破低后回升或Spring候选。不得把初次SC/AR与成熟区间的Spring混写为同一已确认阶段。UT/UTAD、LPSY亦有各自结构位置，不用斜杠把不同事件当同义词。
4. 量价计算优先使用程序提供的volume_comparisons，写清比较起止日、成交量单位、分母是前一交易日还是前N日均量。端点缩量不等于逐日缩量；只有完整交易日序列全部下降才可称逐日萎缩。比较基准不可在辩论或总结时丢失；成交量不证明真实资金主体。
5. 有previous_wyckoff时对照上次日期与原判断，交代新增证据、维持/修订/失效的原因及替代解释；允许证据支持的跨日变化，不机械锁定旧结论。旧报告只是历史意见，不能充当新量价证据。没有前次可比记录则明说，不能杜撰。
6. 使用候选/本轮证据支持/转换待确认/证据不足等状态，分别列确认和失效条件。引用技术结论的辩论、交易员、裁决与总结必须保留这些限定；方法校验未通过的阶段不能升级为确定结论。阶段名称本身不产生买卖或加减仓授权，继续综合其他证据。局部证据不足仅限制该判断，不阻断整份分析。
7. previous_wyckoff.validation_status=rejected表示旧正文未通过方法校验，不是已经成立的上次阶段；不能因新报告重复引用它就升级为已确认。修订目标须说明旧目标去留、原始行情日期和字段、成立条件及本版已知时间；新增远期阻力不等于盘中已经突破，更不能倒填为先前已经发出的信号。
"""


def append_method_contract(prompt: str, agent_key: str = "") -> str:
    # All downstream roles can quote the technical report. Nontechnical analysts
    # need no extra methodology text unless their configured prompt uses it.
    nontechnical = {"financial_news", "china_market", "social_media", "fundamentals", "short_term_capital"}
    if agent_key in nontechnical and not re.search(r"威科夫|wyckoff", prompt, re.I):
        return prompt
    return prompt if METHOD_CONTRACT in prompt else prompt + METHOD_CONTRACT


def method_excerpt(text: str, limit: int = 5000) -> str | None:
    """Whole bounded methodology section; do not pass account/report miscellany."""
    lines = text.splitlines()
    start = None
    level = 0
    for index, line in enumerate(lines):
        heading = re.match(r"^\s*(#{1,6})\s+(.+)", line)
        if heading and re.search(r"威科夫|wyckoff", heading[2], re.I):
            start, level = index, len(heading[1])
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        heading = re.match(r"^\s*(#{1,6})\s+", lines[index])
        if heading and len(heading[1]) <= level:
            end = index
            break
    section = "\n".join(lines[start:end]).strip()
    return section if len(section) <= limit else None
