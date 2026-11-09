"""Check an explicitly labelled trend; never infer it from the phase name."""

import re

from app.engine.wyckoff_quotes import visible_chars

TREND_WORDS = {
    "up": r"上涨|上行|上升|升势",
    "down": r"下跌|下行|下降|跌势",
    "range": r"横盘|震荡|盘整|整理",
}


def check_trend_quote(trend, quote):
    if trend == "uncertain":
        return
    plain, _ = visible_chars(quote)
    clauses = re.findall(r"趋势背景\s*[：:]\s*([^；;。\n]+)", plain)
    if len(clauses) != 1:
        raise ValueError("阶段依据须引用唯一且完整的趋势背景与当前阶段段落")
    clause = re.split(r"(?:当前阶段|阶段假说|阶段判断|阶段定性|候选阶段|状态)\s*[：:为]", clauses[0])[0]
    # Look only at the current side of an explicit temporal transition. An old
    # downtrend followed by a range cannot substantiate trend=down on its own.
    current = re.split(r"但(?:目前|当前)?|(?:当前|目前)(?:为|处于|呈|是)?|转(?:为|入|向)|之后|后", clause)[-1]
    # Conditional/negated wording is not positive evidence for a known trend.
    if re.search(r"并非|不是|不属于|不支持|不再|不能|无法|未确认|尚未|未能|未出现|未发生|未形成|没有|并未|不处于|不呈|不构成|不代表|无明显|若|如果|可能|有望|预计|未来|一旦|等待|尚待", clause):
        raise ValueError("趋势背景含否定、假设或待确认表述，不能据此提交确定的趋势字段")
    if not re.search(TREND_WORDS[trend], current):
        names = {"up": "上涨/上行/上升", "down": "下跌/下行/下降", "range": "横盘/震荡/盘整/整理"}
        raise ValueError(f"趋势字段{trend}与原文趋势背景不一致；须在当前趋势描述中明确{names[trend]}")
