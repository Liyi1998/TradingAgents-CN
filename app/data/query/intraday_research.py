"""Single-security free native bars behind DataInterface; no account payloads."""
import asyncio
import math
from datetime import timedelta

from app.data.query.intraday_chart import market_clock
from app.engine.intraday_research import CN, expected_ends, prepare


def verified_daily_supplement(symbol, day, domain, rows):
    """One explicitly dated/unit-proved snapshot; no old-day or cross-source sum."""
    field = "main_net_inflow" if domain == "money_flow" else "amount"
    checked = []
    for row in rows:
        if (row.get("symbol") != symbol or str(row.get("trade_date", ""))[:10] != day
                or row.get("amount_unit") != "CNY" or not row.get("data_source")):
            continue
        value = row.get(field)
        if type(value) not in (int, float) or not math.isfinite(value):
            continue
        if domain == "money_flow":
            if (row.get("data_source") != "akshare" or row.get("flow_basis") != "eastmoney_order_size_v1"
                    or row.get("flow_date_verified") is not True or row.get("flow_date_basis") != "source_row_date"):
                continue
        elif row.get("price_basis") != "raw" or value < 0:
            continue
        checked.append(row)
    if not checked or len({row[field] for row in checked}) != 1:
        return {"date": day, "value": None, "verified": False, "reason": "缺少当日已核验字段或多份记录冲突"}
    return {"date": day, "value": checked[0][field], "verified": True,
            "source": checked[0]["data_source"], "amount_unit": "CNY", "field": field}


async def supplementary(di, symbol, day):
    async def read(domain):
        try:
            result = await asyncio.wait_for(di.read("CN", domain, symbol=symbol, start_date=day, end_date=day), 5)
            return verified_daily_supplement(symbol, day, domain, result.get("data") or [])
        except Exception:
            return {"date": day, "value": None, "verified": False, "reason": "本次读取未完成"}
    flow, amount = await asyncio.gather(read("money_flow"), read("daily_quotes"))
    return {"main_flow": flow, "day_amount": amount}


async def collect(di, symbol, now):
    day = now.astimezone(CN).date()
    calendar = await asyncio.wait_for(di.read("CN", "trade_calendar",
        start_date=day.isoformat(), end_date=(day+timedelta(days=10)).isoformat()), 8)
    session = market_clock(calendar.get("data") or [], now)
    rows = []
    issue = None
    if session.get("is_open") is True and expected_ends(now):
        try:
            from app.data.sources.cn.tencent.api.intraday_quotes import fetch_intraday_quotes
            frame = await asyncio.wait_for(fetch_intraday_quotes(symbol), 12)
            rows = [{**row, "data_source": "tencent", "bar_time_kind": "end"} for row in frame.to_dict("records")]
        except Exception:
            # Old or ambiguous rows cannot silently replace a failed live fetch.
            issue = "本次免费分钟行情获取失败；已保存的观察保留，不能视为当前条件已满足"
    result = prepare(symbol, rows, session, now)
    if result["status"] == "ready" and len(result["bars"]) == 8:
        result["supplementary"] = await supplementary(di, symbol, day.isoformat())
    if issue:
        result["issues"].append(issue)
    return result
