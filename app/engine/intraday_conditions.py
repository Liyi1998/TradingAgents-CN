"""Explainable, bounded public-condition predicates, without LLM interpretation.

Only a complete grammar match may establish a full condition. Extracted price
or volume fragments are explicitly partial and never erase the remaining text.
"""
from __future__ import annotations

import math
import re

POLICY = "intraday-conditions-v2"
NUMBER = r"-?\d+(?:\.\d+)?"
OPS = r"不低于|不高于|不超过|超过|高于|低于|大于|小于|至少|达到|突破|跌破|击穿"
PRICE_SUBJECT = r"30分钟(?:K线)?收盘价|日K线收盘价|日线收盘价|收盘价|盘中(?:股价|价格)?|股价|价格"
PRICE = re.compile(rf"(?P<subject>{PRICE_SUBJECT})(?P<op>{OPS})(?P<n>{NUMBER})元(?P<tail>硬性止损底线|止损线|防线|支撑位|阻力位)?")
TOUCH = re.compile(rf"(?:盘中(?:股价|价格)?|股价|价格)?触及(?P<a>{NUMBER})元?(?:至|到|–|-)(?P<b>{NUMBER})元(?:支撑区间|阻力区间|阻力带|区间)?")
POINT_TOUCH = re.compile(rf"(?:盘中(?:股价|价格)?|股价|价格)触及(?P<n>{NUMBER})元")
VOLUME = re.compile(rf"(?P<subject>日线成交量|当日成交量|单日成交量|30分钟成交量|当日累计成交量|单日成交额|当日成交额)(?P<op>{OPS})(?P<n>{NUMBER})(?P<unit>亿股|万股|股|亿元|万元|元)")
COLOR = re.compile(r"30分钟(?:K线)?收(?P<color>阳线|阴线)")
RATIO = re.compile(rf"(?:本根)?30分钟成交量(?P<op>超过|不低于)前两根均量的(?P<n>{NUMBER})倍")
FLOW = re.compile(rf"(?:当日|单日)?主力(?:资金)?(?P<direction>净流入|净流出)(?P<op>{OPS})(?P<n>{NUMBER})(?P<unit>亿元|万元|元)")
PATTERNS = (PRICE, TOUCH, POINT_TOUCH, VOLUME, COLOR, RATIO, FLOW)
SCOPES = {"bar_close": "最新完整30分钟收盘价", "intraday_low": "今日已完成时段最低价",
          "intraday_high": "今日已完成时段最高价", "day_close": "当日收盘价（完整30分钟线汇总）",
          "day_volume": "当日成交量（完整30分钟线汇总）", "bar_volume": "最新30分钟成交量",
          "cumulative_volume": "今日已完成时段累计成交量", "day_amount": "当日已核验日线成交额",
          "main_flow": "当日主力净流入（东方财富大单口径）"}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def full_day(snapshot):
    return (snapshot.get("status") == "ready" and len(snapshot.get("bars") or []) == 8
            and snapshot["bars"][-1]["datetime"].endswith("15:00:00"))


def metric(snapshot, name):
    bars = snapshot.get("bars") or []
    if snapshot.get("status") != "ready" or not bars:
        phase = (snapshot.get("session") or {}).get("phase")
        state = "pending_session" if phase == "pre_open" or snapshot.get("waiting_for_first_bar") or (snapshot.get("session") or {}).get("is_open") is False else "missing_data"
        return {"state": state, "reason": "等待当日完整时段" if state == "pending_session" else "当日分钟线缺失、时段不连续或口径未通过校验"}
    if name.startswith("day_") or name == "main_flow":
        if not full_day(snapshot):
            return {"state": "pending_close", "reason": "原条件要求当日收盘数据，盘中尚不能确认"}
    value = {"bar_close": bars[-1]["close"], "intraday_low": min(b["low"] for b in bars),
             "intraday_high": max(b["high"] for b in bars), "day_close": bars[-1]["close"],
             "day_volume": sum(b["volume"] for b in bars), "bar_volume": bars[-1]["volume"],
             "cumulative_volume": sum(b["volume"] for b in bars)}.get(name)
    source = snapshot.get("source")
    if name in ("day_amount", "main_flow"):
        item = (snapshot.get("supplementary") or {}).get(name) or {}
        value = item.get("value") if item.get("date") == snapshot["date"] and item.get("verified") is True else None
        source = item.get("source")
        if not finite(value):
            return {"state": "missing_data", "reason": "缺少今日已核验的" + SCOPES[name] + "；未沿用旧日期或估算值"}
    return {"state": "ready", "value": value, "scope": SCOPES[name], "source": source,
            "as_of": snapshot.get("as_of"), "evidence_ids": [b["id"] for b in bars]}


def compare(value, op, target):
    return {"不低于": value >= target, "至少": value >= target, "达到": value >= target,
            "不高于": value <= target, "不超过": value <= target,
            "超过": value > target, "高于": value > target, "大于": value > target, "突破": value > target,
            "低于": value < target, "小于": value < target, "跌破": value < target, "击穿": value < target}[op]


def quantity(value, unit):
    scale, label = (1e8, "亿") if abs(value) >= 1e8 else (1e4, "万") if abs(value) >= 1e4 else (1, "")
    return f"{value / scale:,.2f}".rstrip("0").rstrip(".") + label + unit


def unsupported_reason(text):
    reasons = []
    for pattern, reason in [
        ("企稳|站稳|承接|有效|衰竭|放量|缩量", "企稳、承接或强弱描述缺少明确的持续根数、比较基准等判定标准"),
        ("公告|新闻|诉讼|法院|和解|监管|立案|利空|黑天鹅", "当前模块未接入公告或新闻事实核验"),
        ("连续[0-9一二三四五六七八九]+[日天]|近[0-9一二三四五六七八九]+个交易日", "尚未接入连续多日、统一口径的序列条件核验"),
        ("两融|融资|融券", "当前模块尚未接入两融条件核验"),
        ("净利润|毛利率|财务|减值", "当前模块尚未接入财务与披露事件条件核验"),
        ("沪深300|全市场|跌停", "当前模块尚未接入指数与全市场联动条件核验"),
        ("持仓|仓位|市价|挂单|清仓|减半|减磅", "涉及持仓或执行动作，未核验账户可卖数量，也不执行交易"),
        (r"20\d{2}[-年]\d|\d{2}-\d{2}", "原文含历史日期或历史锚点，不把它自动改成今天的事实"),
    ]:
        if re.search(pattern, text):
            reasons.append(reason)
    return "；".join(reasons) or "当前规则尚不能无歧义地解释完整原句，请保留原条件"


def predicate(pattern, match, snapshot):
    g = match.groupdict()
    values = [float(g[k]) for k in ("n", "a", "b") if g.get(k) is not None]
    if any(not finite(v) or v < 0 for v in values) or (pattern in (PRICE, TOUCH, POINT_TOUCH, RATIO) and any(v == 0 for v in values)):
        return {"state": "unsupported", "reason": "阈值或金额正负口径不明确，不能自动转换"}
    if pattern is PRICE:
        subject, op, target = g["subject"], g["op"], float(g["n"])
        name = "bar_close" if subject.startswith("30分钟") else "day_close" if "收盘" in subject else "intraday_low" if op in ("跌破", "击穿", "低于", "小于", "不高于", "不超过") else "intraday_high"
        item = metric(snapshot, name)
        if item["state"] != "ready":
            return item
        met = compare(item["value"], op, target)
        if name == "bar_close" and op in ("突破", "跌破", "击穿"):
            bars = snapshot["bars"]
            if len(bars) < 2:
                return {"state": "missing_data", "reason": "确认30分钟收盘穿越需要前一根完整K线"}
            met = bars[-2]["close"] <= target < item["value"] if op == "突破" else bars[-2]["close"] >= target > item["value"]
        return {**item, "state": "met" if met else "not_met", "reason": f"{item['scope']} {item['value']:g} 元；阈值 {target:g} 元"}
    if pattern in (TOUCH, POINT_TOUCH):
        a, b = (float(g["a"]), float(g["b"])) if pattern is TOUCH else (float(g["n"]), float(g["n"]))
        if a > b:
            return {"state": "unsupported", "reason": "原区间上下界顺序不明确"}
        item = metric(snapshot, "bar_close")
        if item["state"] != "ready":
            return item
        hits = [r for r in snapshot["bars"] if r["low"] <= b and r["high"] >= a]
        return {**item, "state": "met" if hits else "not_met", "reason": f"今日完整时段{'已' if hits else '尚未'}触及 {a:g}–{b:g} 元；只证明价格触及",
                "hit_times": [r["datetime"] for r in hits]}
    if pattern is VOLUME:
        subject, unit = g["subject"], g["unit"]
        is_amount = "额" in subject
        if is_amount != unit.endswith("元"):
            return {"state": "unsupported", "reason": "成交量与成交额单位不一致"}
        name = "day_amount" if is_amount else "bar_volume" if subject.startswith("30") else "cumulative_volume" if "累计" in subject else "day_volume"
        item = metric(snapshot, name)
        if item["state"] != "ready":
            return item
        target = float(g["n"]) * (1e8 if unit.startswith("亿") else 1e4 if unit.startswith("万") else 1)
        met = compare(item["value"], g["op"], target)
        unit = '元' if is_amount else '股'
        return {**item, "state": "met" if met else "not_met", "reason": f"{item['scope']} {quantity(item['value'], unit)}；阈值 {quantity(target, unit)}"}
    if pattern is FLOW:
        item = metric(snapshot, "main_flow")
        if item["state"] != "ready":
            return item
        target = float(g["n"]) * (1e8 if g["unit"] == "亿元" else 1e4 if g["unit"] == "万元" else 1)
        amount = max(0, -item["value"]) if g["direction"] == "净流出" else item["value"]
        return {**item, "state": "met" if compare(amount, g["op"], target) else "not_met",
                "reason": f"当日主力净流入 {quantity(item['value'], '元')}；{g['direction']}比较阈值 {quantity(target, '元')}"}
    item = metric(snapshot, "bar_close")
    if item["state"] != "ready":
        return item
    bars = snapshot["bars"]
    if pattern is COLOR:
        met = bars[-1]["close"] > bars[-1]["open"] if g["color"] == "阳线" else bars[-1]["close"] < bars[-1]["open"]
        return {**item, "state": "met" if met else "not_met", "reason": f"最新30分钟开 {bars[-1]['open']:g}、收 {bars[-1]['close']:g}；阳阴线不等于企稳"}
    if len(bars) < 3 or sum(b["volume"] for b in bars[-3:-1]) <= 0:
        return {"state": "missing_data", "reason": "缺少两根可比较的完整成交量，或均量为零"}
    ratio = bars[-1]["volume"] / (sum(b["volume"] for b in bars[-3:-1])/2)
    return {**item, "state": "met" if compare(ratio, g["op"], float(g["n"])) else "not_met", "reason": f"相邻两根均量之比 {ratio:.3f}；不是历史同时间量比"}


def numeric_fragments(text, snapshot):
    """Recover explicit numeric subconditions only; never approve the prose."""
    derived = []
    for m in re.finditer(rf"(日K线|日线)收盘价(?:站稳|收复)(?:MA\d+(?:阻力位|均线)?)?({NUMBER})元(?:之上|上方)?", text):
        derived.append((m.group(), "日线收盘价高于"+m.group(2)+"元", "仅比较原文给出的价格；未验证站稳、收复或MA是否已变化"))
    for m in re.finditer(rf"日线成交量(?:缩减至|萎缩至)({NUMBER})(亿股|万股|股)(以下|以内)", text):
        derived.append((m.group(), "日线成交量不超过"+m.group(1)+m.group(2), "仅比较明确的成交量上限；未证明相对哪一天缩量"))
    for m in re.finditer("30分钟(?:K线)?收企稳阳线", text):
        derived.append((m.group(), "30分钟K线收阳线", "只核验阳线；企稳缺少持续根数、幅度等定义"))
    for m in re.finditer(rf"股价(?:盘中)?回踩({NUMBER})元?(?:-|–|至)({NUMBER})元", text):
        derived.append((m.group(), "盘中触及"+m.group(1)+"至"+m.group(2)+"元", "仅核验原价带是否曾触及；未证明回踩方向或承接"))
    for m in re.finditer(rf"(?:股价|价格)(跌破|击穿)20\d{{2}}-\d{{2}}-\d{{2}}(?:日内回踩低点|极限低点对应止损防线)({NUMBER})元", text):
        derived.append((m.group(), "盘中价格"+m.group(1)+m.group(2)+"元", "仅比较原文已明确的价位；未验证历史低点含义或后续执行要求"))
    for original, canonical, note in derived:
        result = leaf(canonical, snapshot)
        yield {**result, "text": original, "partial": True, "reason": result["reason"]+"；"+note}


def leaf(text, snapshot):
    clean = re.sub(r"^(?:当(?!日)|若|如果)", "", text.strip().strip("，,。；; "))
    for pattern in PATTERNS:
        match = pattern.fullmatch(clean)
        if match:
            return {"text": text, **predicate(pattern, match, snapshot)}
    return {"text": text, "state": "unsupported", "reason": unsupported_reason(text)}


def evaluate_condition(text, snapshot):
    # Mixed conjunctions without explicit grouping are not assigned an invented
    # priority. Individual fragments can still show evidence beneath the text.
    text = str(text or "")
    has_and, has_or = bool(re.search("且|并且|同时", text)), "或" in text
    parts = re.split(r"(?:，|,)?(?:并且|且|同时|或)", text)
    checks = [leaf(p, snapshot) for p in parts if p.strip()]
    if not checks:
        checks = [leaf(text, snapshot)]
    if has_and and has_or:
        checks.append({"text": "组合关系", "state": "unsupported", "reason": "同时包含且/或，原文未提供明确分组，未擅自决定优先级"})
    blockers = [c for c in checks if c["state"] not in ("met", "not_met")]
    state = "met" if (any(c["state"] == "met" for c in checks) if has_or else all(c["state"] == "met" for c in checks)) else "not_met"
    # An explicit OR can be proved by one supported branch, even while the
    # other supported branch awaits close/data. Unparsed prose never qualifies.
    resolved_or = has_or and not has_and and any(c["state"] == "met" for c in checks) and not any(c["state"] == "unsupported" for c in checks)
    if blockers and not resolved_or:
        state = next(s for s in ("unsupported", "missing_data", "pending_session", "pending_close") if any(c["state"] == s for c in blockers))
    # Price fragments remain useful even when the overall prose is unsupported.
    partial = []
    for check in checks:
        if check["state"] != "unsupported" or check["text"] == "组合关系":
            continue
        for pattern in PATTERNS:
            for match in pattern.finditer(check["text"]):
                partial.append({"text": match.group(), "partial": True, **predicate(pattern, match, snapshot)})
        partial.extend(numeric_fragments(check["text"], snapshot))
    reason = {"met": "按原文的且/或关系，该完整规则本次满足；不代表可执行交易", "not_met": "该规则本次尚未满足",
              "unsupported": "暂不能自动核验完整原句；下方仅列可核验的子条件，未省略其余要求",
              "missing_data": "规则可以计算，但缺少指定日期或口径的数据", "pending_close": "条件要求收盘数据，等待当日收盘",
              "pending_session": "等待当日已完成交易时段"}[state]
    if resolved_or and blockers:
        reason = "原文明确为或关系，其中一项已满足；其他项仍保留等待或缺数据状态，不代表可执行交易"
    return {"state": state, "reason": reason, "checks": checks + partial[:12], "policy": POLICY,
            "missing_data": [c["reason"] for c in checks+partial if c["state"] == "missing_data"],
            "unsupported": [c["reason"] for c in checks if c["state"] == "unsupported"]}
