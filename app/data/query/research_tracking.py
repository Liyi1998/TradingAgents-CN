"""Bounded single-stock evidence for daily research; all access through DI."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

from app.data.core.analysis_context import _calendar_days, verified_future_sessions
from app.data.core.interface import DataInterface
from app.data.core.requirements import analysis_filters
from app.data.query.intraday_chart import market_clock
from app.engine.research_tracking import digest, iso_day
from app.llm.jev_decisions import closed_sessions

DOMAINS = ("daily_quotes", "daily_indicators", "money_flow", "margin_trading", "news", "financial_data")
DATE_FIELDS = {"news": "publish_time", "financial_data": "announce_date"}
VOLATILE = {"_id", "updated_at", "created_at", "fetched_at", "read_at", "ingested_at", "last_updated", "sync_time"}


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, (list, tuple)):
        return [clean(x) for x in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def domain_block(domain, result, as_of, symbol):
    rows, omitted = [], 0
    for original in result.get("data") or []:
        if not isinstance(original, dict) or str(original.get("symbol", "")) != symbol:
            omitted += 1
            continue
        observed = iso_day(original.get(DATE_FIELDS.get(domain, "trade_date")))
        if not observed or observed > as_of:
            omitted += 1
            continue
        row = clean(original)
        if domain == "daily_quotes" and (row.get("price_basis") != "raw" or row.get("period") != "daily" or row.get("volume_unit") != "share"):
            omitted += 1
            continue
        # News remains an excerpt with its explicit scope; do not silently claim
        # the entire article or every market event has been checked.
        if domain == "news":
            for key in ("content", "summary"):
                if len(str(row.get(key) or "")) > 2000:
                    row[key] = str(row[key])[:2000]
                    row["excerpt_only"] = True
        rows.append({"id": domain + ":" + digest(row)[:20], "date": observed, "data": row})
    rows = list({row["id"]: row for row in rows}.values())
    rows.sort(key=lambda r: (r["date"], r["id"]))
    limit = 140 if domain == "daily_quotes" else 30 if domain == "news" else 8 if domain == "financial_data" else 12
    sampled = len(rows) > limit
    rows = rows[-limit:]
    quality = result.get("quality") or {}
    status = quality.get("status", "unknown")
    limitations = []
    if omitted:
        limitations.append(f"{omitted}条记录缺少对应股票、有效日期或统一价格口径，未用作当前证据")
    if sampled:
        limitations.append(f"仅提供最近{limit}条有效记录")
    if status not in {"success", "no_events", "not_applicable"}:
        limitations.append("数据窗口不完整或获取失败；不等于没有风险或没有事件")
    latest = max((r["date"] for r in rows), default=None)
    if domain in {"daily_quotes", "daily_indicators", "money_flow", "margin_trading"} and latest != as_of and status != "not_applicable":
        status = "stale" if rows else "unavailable"
        limitations.append(f"本轮需要截至{as_of}，已获取截至{latest or '未知'}")
    if domain == "news":
        limitations.append("新闻接口返回的是有限样本，不证明全量公告、行业和政策事件均已覆盖")
    return {"status": status, "latest_date": latest, "rows": rows, "limitations": limitations}


def price_bars(block, as_of):
    grouped = {}
    for item in block.get("rows", []):
        r = item["data"]
        if r.get("price_basis") != "raw" or r.get("period") != "daily" or r.get("volume_unit") != "share":
            continue
        if not r.get("data_source") or any(type(r.get(k)) not in (int, float) or r[k] <= 0 for k in ("open", "high", "low", "close")):
            continue
        if not r["low"] <= min(r["open"], r["close"]) <= max(r["open"], r["close"]) <= r["high"]:
            continue
        grouped.setdefault(r["data_source"], {})[item["date"]] = {"date": item["date"], "data_source": r["data_source"],
            **{k: r.get(k) for k in ("open", "high", "low", "close", "volume")}, "evidence_id": item["id"]}
    if not grouped:
        return [], None
    source = max(grouped, key=lambda s: (as_of in grouped[s], len(grouped[s]), s))
    bars = sorted(grouped[source].values(), key=lambda r: r["date"])
    current = grouped[source].get(as_of)
    peers = [g[as_of]["close"] for g in grouped.values() if as_of in g]
    if current and peers and max(peers) - min(peers) > max(peers) * .002:
        return bars, None
    return bars, current


def daily_metrics(bars, sessions):
    """Only consecutive, verified exchange sessions count toward rolling values."""
    by_day = {bar["date"]: bar for bar in bars}
    output = {"price_basis": "raw", "as_of": sessions[-1] if sessions else None}
    for window in (5, 10, 20):
        days = sessions[-window:]
        if len(days) == window and all(day in by_day for day in days):
            output[f"ma{window}"] = round(sum(by_day[day]["close"] for day in days)/window, 4)
    if len(sessions) >= 6 and all(day in by_day and type(by_day[day].get("volume")) in (int, float) for day in sessions[-6:]):
        average = sum(by_day[day]["volume"] for day in sessions[-6:-1])/5
        if average > 0:
            output["volume_vs_previous_5_sessions"] = round(by_day[sessions[-1]]["volume"]/average, 4)
    from app.data.query.volume_comparisons import volume_comparisons
    output["volume_comparisons"] = volume_comparisons(bars, sessions)
    return output


async def trading_window(now: datetime, di):
    di = di or DataInterface.get_instance()
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    calendar = await asyncio.wait_for(di.read("CN", "trade_calendar",
        start_date=(local.date()-timedelta(days=400)).isoformat(),
        end_date=(local.date()+timedelta(days=30)).isoformat()), 12)
    sessions = closed_sessions(calendar.get("data") or [], now, count=1)
    if not sessions:
        raise ValueError("交易日历不足，无法确认最近已结束的交易日")
    as_of = sessions[-1]
    anchor = datetime.fromisoformat(as_of).date()
    recent = closed_sessions(calendar.get("data") or [], now, count=20)
    return as_of, anchor, recent, calendar.get("data") or []


def evidence_filters(symbol, domain, start, as_of, recent):
    filters = {"price_basis": "raw", "period": "daily", "expected_dates": recent} if domain == "daily_quotes" else {}
    return analysis_filters("CN", domain, symbol, start, as_of, filters,
        {"market": "CN", "recent_trade_dates": recent, "calendar_status": "success" if len(recent) == 20 else "partial"})


def chan_context(bars, as_of, calendar):
    """Bind structures to the chart's complete exchange calendar and raw basis."""
    days = _calendar_days(calendar)
    start = datetime.fromisoformat(bars[0]["date"]).date() if bars else datetime.fromisoformat(as_of).date()
    end = datetime.fromisoformat(as_of).date()
    dates = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    future = verified_future_sessions(calendar, end)
    return {"price_basis": "raw", "currency": "CNY", "calendar_complete": bool(dates) and all(days.get(d) is not None for d in dates),
            "expected_sessions": [d for d in dates if days.get(d)], "next_session": future[0] if future else None}


async def collect_market_chart(symbol: str, now: datetime, *, di=None) -> dict:
    """Latest completed daily bars, without model calls or research mutations."""
    di = di or DataInterface.get_instance()
    as_of, anchor, recent, calendar = await trading_window(now, di)
    start = (anchor-timedelta(days=240)).isoformat()
    result = await asyncio.wait_for(di.read_ready("CN", "daily_quotes", symbol, timeout=20,
        start_date=start, end_date=as_of, filters=evidence_filters(symbol, "daily_quotes", start, as_of, recent)), 24)
    block = domain_block("daily_quotes", result, as_of, symbol)
    bars, price = price_bars(block, as_of)
    return {"as_of": as_of, "bars": bars, "price": price, "quality": block["status"],
        "limitations": block["limitations"], "next_sessions": verified_future_sessions(calendar, anchor),
        "session": market_clock(calendar, now), "chan_context": chan_context(bars, as_of, calendar)}


async def collect_evidence(symbol: str, now: datetime, *, di=None) -> dict:
    di = di or DataInterface.get_instance()
    as_of, anchor, recent, calendar = await trading_window(now, di)
    gate = asyncio.Semaphore(2)

    async def one(domain):
        days = 500 if domain == "financial_data" else 240 if domain == "daily_quotes" else 14
        start = (anchor-timedelta(days=days)).isoformat()
        filters = evidence_filters(symbol, domain, start, as_of, recent)
        try:
            async with gate:
                result = await asyncio.wait_for(di.read_ready("CN", domain, symbol, timeout=20,
                    start_date=start, end_date=as_of, filters=filters), 24)
            return domain, domain_block(domain, result, as_of, symbol)
        except Exception:
            return domain, {"status": "unavailable", "latest_date": None, "rows": [], "limitations": ["本次数据获取失败，其他模块继续更新"]}

    domains = dict(await asyncio.gather(*(one(domain) for domain in DOMAINS)))
    bars, price = price_bars(domains["daily_quotes"], as_of)
    return {"as_of": as_of, "collected_at": now.isoformat(), "domains": domains,
        "bars": bars, "price": price, "next_sessions": verified_future_sessions(calendar, anchor),
        "recent_sessions": recent, "daily_metrics": daily_metrics(bars, recent),
        "price_basis": "raw", "currency": "CNY", "chan_context": chan_context(bars, as_of, calendar),
        "limitations": ["盘后日线研究，不含当前盘中承接或可成交性", "不复权价格可能受除权除息影响；跨期跳变须核对公司行为，不能当作复权收益"]}
