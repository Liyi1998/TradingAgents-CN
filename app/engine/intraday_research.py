"""Deterministic, prefix-only intraday observations. Never trading authority.

The 30-minute contract uses exchange-native bars labelled by interval end.
Unclosed bars, future rows and unverified units cannot establish conditions.
Public report clauses are preserved verbatim; unsupported clauses stay unknown.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

from app.engine.research_tracking import digest
from app.engine.intraday_conditions import evaluate_condition

CN = ZoneInfo("Asia/Shanghai")
ENGINE = "intraday-observation-v2"
ENDS = (600, 630, 660, 690, 810, 840, 870, 900)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def expected_ends(now):
    local = now.astimezone(CN)
    # Allow 60 seconds for publication. This also excludes the just-closing bar.
    return [local.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0)
            for m in ENDS
            if local >= local.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0) + timedelta(seconds=60)]


def prepare(symbol, rows, session, now):
    if now.tzinfo is None:
        raise ValueError("aware clock required")
    local = now.astimezone(CN)
    day = local.date().isoformat()
    base = {"engine": ENGINE, "symbol": symbol, "date": day, "checked_at": now.isoformat(),
            "frequency": "30min", "price_basis": "raw", "currency": "CNY", "volume_unit": "share",
            "bars": [], "status": "insufficient", "issues": [], "source": None,
            "as_of": None, "delay_seconds": None, "session": session}
    if session.get("date") != day or session.get("is_open") is not True:
        base["issues"] = ["当日交易日历未确认或休市；未把上一交易日数据当作今日盘中判断"]
        return base
    expected = expected_ends(now)
    if not expected:
        base["waiting_for_first_bar"] = True
        base["issues"] = ["尚无已完成且过发布等待期的30分钟K线，首根最早10:01后核验"]
        return base
    allowed = {d.strftime("%Y-%m-%d %H:%M:%S") for d in expected}
    groups, conflicts, rejected = {}, set(), 0
    for row in rows:
        stamp = str(row.get("datetime") or "")
        if stamp[:10] != day or stamp not in allowed:
            continue
        if (str(row.get("symbol")) != symbol or row.get("freq") != "30min"
                or row.get("ohlc_kind") != "native_bar" or row.get("price_basis") != "raw"
                or row.get("volume_unit") != "share" or row.get("bar_time_kind") != "end"
                or not row.get("data_source")
                or any(not finite(row.get(k)) or row[k] <= 0 for k in ("open", "high", "low", "close"))
                or not finite(row.get("volume")) or row["volume"] < 0
                or not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]):
            rejected += 1
            continue
        source = row["data_source"]
        bar = {k: row[k] for k in ("datetime", "open", "high", "low", "close", "volume")}
        bar["id"] = digest({"source": source, **bar})[:24]
        old = groups.setdefault(source, {}).get(stamp)
        if old and old != bar:
            conflicts.add(stamp)
        groups[source][stamp] = bar
    if not groups:
        base["issues"] = ["尚未取得符合原生OHLC、时间戳及股数口径的当日分钟线"]
        return base
    source = max(groups, key=lambda s: (max(groups[s]), len(groups[s]), s))
    bars = sorted(groups[source].values(), key=lambda r: r["datetime"])
    for stamp in allowed:
        peers = [g[stamp] for g in groups.values() if stamp in g]
        if peers and any(max(p[k] for p in peers) - min(p[k] for p in peers) > .011
                         for k in ("open", "high", "low", "close")):
            conflicts.add(stamp)
    missing = sorted(allowed - set(groups[source]))
    issues = []
    if missing:
        issues.append("已完成时段缺少分钟线：" + "、".join(s[11:16] for s in missing))
    if conflicts:
        issues.append("分钟线存在重复或多源价格冲突，暂停条件判断")
    if rejected:
        issues.append(f"{rejected}条记录口径未验证或无效，已剔除")
    base.update(bars=bars, source=source, as_of=bars[-1]["datetime"],
                delay_seconds=int((local-datetime.fromisoformat(bars[-1]["datetime"]).replace(tzinfo=CN)).total_seconds()),
                issues=issues, status="ready" if not (missing or conflicts) else "insufficient")
    return base


def reference_zones(snapshot):
    """Opening range + confirmed local pivots; no ATR guesses or trailing stops.

    Each pivot requires its right neighbour to be complete, so it only becomes
    visible at that neighbour's end. Prices never overwrite report levels.
    """
    if snapshot["status"] != "ready":
        return []
    bars = snapshot["bars"]
    if not bars:
        return []
    day = snapshot["date"]
    zones = []

    def zone(label, kind, lower, upper, evidence):
        zones.append({"id": digest([label, evidence])[:20], "label": label, "kind": kind,
                      "lower": lower, "upper": upper, "observed_at": evidence[-1]["datetime"],
                      "valid_until": day, "evidence_ids": [b["id"] for b in evidence],
                      "source": snapshot["source"], "state": "intraday_candidate",
                      "condition": "仅为已完成30分钟量价结构参考；未替代原报告点位或授权交易"})

    zone("开盘波动范围（09:30–10:00）", "range", bars[0]["low"], bars[0]["high"], bars[:1])
    pivots = []
    for i in range(1, len(bars)-1):
        a, b, c = bars[i-1:i+2]
        if b["low"] < min(a["low"], c["low"]):
            pivots.append(("30分钟局部低点参考", "support", b["low"], min(b["open"], b["close"]), [a, b, c]))
        if b["high"] > max(a["high"], c["high"]):
            pivots.append(("30分钟局部高点参考", "resistance", max(b["open"], b["close"]), b["high"], [a, b, c]))
    for item in pivots[-4:]:
        zone(*item)
    return zones


def evaluate(snapshot, levels, conditions):
    bars = snapshot.get("bars") or []
    ready = snapshot["status"] == "ready" and bool(bars)
    last = bars[-1] if bars else {}
    rows = []
    for level in levels[:16]:
        row = {"id": digest(level)[:20], "label": level.get("label", "原报告点位"),
               "lower": level.get("lower"), "upper": level.get("upper"),
               "condition": level.get("condition") or "原文未提供可核验的完整条件",
               "source_date": level.get("source_report_date") or level.get("source_research_date") or level.get("source_date"),
               "state": "missing_data", "price_state": "unknown", "evidence_ids": [], "category": "level", "kind": level.get("kind"),
               "reason": "分钟证据或点位价格口径不足"}
        lower, upper = row["lower"], row["upper"]
        if level.get("expired") or (level.get("valid_until") and str(level["valid_until"])[:10] < snapshot["date"]):
            row.update(state="expired", reason="原点位已过有效期；盘中行情不会为它续期")
        elif ready and level.get("price_basis") == "raw" and finite(lower) and finite(upper) and 0 < lower <= upper:
            touched = last["low"] <= upper and last["high"] >= lower
            hits = [b for b in bars if b["low"] <= upper and b["high"] >= lower]
            below = last["close"] < lower and level.get("kind") in ("exit", "support")
            row.update(state="breach_candidate" if below else "candidate" if touched else "not_triggered",
                       price_state="touch" if touched else "above" if last["close"] > upper else "below",
                       day_touched=bool(hits), first_touch_at=hits[0]["datetime"] if hits else None,
                       latest_close=last["close"], interval_end=last["datetime"],
                       evidence_ids=[last["id"]],
                       reason="最新30分钟收盘已低于原保护参考下沿；请核验原退出条件，未下移止损" if below else "最新完整30分钟触及该价格带；其他前提需单独核验" if touched else "最新完整30分钟未触及；今日是否曾触及另列，不代表没有风险")
        elif level.get("price_basis") != "raw":
            row["reason"] = "原点位未证明为不复权口径，不能直接与当前分钟价格比较"
        row["checks"] = []
        rows.append(row)
    for index, clause in enumerate(conditions[:20]):
        text = str(clause.get("text") or "")
        name = {"entry": "入场条件", "exit": "退出条件", "veto": "否决条件"}.get(clause.get("kind"), "策略条件")
        row = {"id": digest(clause)[:20], "label": f"{name} {index+1}", "condition": text,
               "category": "strategy", "kind": clause.get("kind"), "previous_state": clause.get("state"),
               "evidence_ids": [], **evaluate_condition(text, snapshot)}
        if row["state"] == "met" and clause.get("kind") == "veto":
            row["state"] = "invalidated"
        rows.append(row)
    volume_ratio = None
    if ready and len(bars) >= 3:
        average = sum(b["volume"] for b in bars[-3:-1])/2
        if average > 0:
            volume_ratio = round(last["volume"]/average, 3)
    opening = None
    if ready:
        a, b, price = bars[0]["low"], bars[0]["high"], last["close"]
        opening = {"lower": a, "upper": b, "latest_close": price,
                   "position": "高于开盘范围" if price > b else "低于开盘范围" if price < a else "位于开盘范围内"}
    return {"conditions": rows, "zones": reference_zones(snapshot), "opening_reference": opening, "volume_ratio_previous_two_bars": volume_ratio,
            "wyckoff_candidate": "分钟收盘低于前两根低点，日线结构可能承压，待盘后复核" if ready and len(bars) >= 3 and last["close"] < min(b["low"] for b in bars[-3:-1])
                else "分钟收盘高于前两根高点，日线结构可能变化，待盘后复核" if ready and len(bars) >= 3 and last["close"] > max(b["high"] for b in bars[-3:-1]) else None,
            "trade_authorized": False, "sellable_position": "unknown",
            "limitations": ["原支撑、阻力、止损及其日期保持不变；不会因下跌向下移动止损",
                "仅使用已完成30分钟K线，可能存在发布延迟；成交量比为相邻两根对比，不是同时间历史量比",
                "未核验实时新闻、财务变化或账户可卖数量；不假定当日买入股份可卖",
                "分钟候选不能替代日线威科夫确认；未调用模型，不生成新增仓位授权"]}
