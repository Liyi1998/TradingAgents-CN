"""Bounded single-symbol context for the structure auxiliary. No model calls."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.engine.intraday_research import CN


async def _read(di, domain, issues, **kwargs):
    try:
        result = await asyncio.wait_for(di.read("CN", domain, auto_refresh=False, **kwargs), 8)
        return result.get("data") or []
    except Exception as exc:
        issues.append(f"{domain} 未读取：{type(exc).__name__}")
        return []


async def collect(di, symbol: str, now):
    """Free 30-minute bars plus already stored calendar, factors and daily closes.

    Stored reads do not refresh the market. A missing series stays missing.
    """
    issues = []
    rows = []
    try:
        from app.data.sources.cn.tencent.api.intraday_quotes import fetch_intraday_quotes

        frame = await asyncio.wait_for(fetch_intraday_quotes(symbol), 12)
        rows = [{**row, "data_source": "tencent", "bar_time_kind": "end"} for row in frame.to_dict("records")]
    except Exception as exc:
        issues.append(f"30分钟行情未取得：{type(exc).__name__}")
    local = now.astimezone(CN).date()
    start = (local - timedelta(days=40)).isoformat()
    end = (local + timedelta(days=10)).isoformat()
    today = local.isoformat()
    calendar, adjustments, daily, index_rows = await asyncio.gather(
        _read(di, "trade_calendar", issues, start_date=start, end_date=end),
        _read(di, "adj_factors", issues, symbol=symbol, start_date=start, end_date=today),
        _read(di, "daily_quotes", issues, symbol=symbol, start_date=start, end_date=today),
        _read(di, "index_quotes", issues, symbol="000300.SH", start_date=start, end_date=today),
    )
    return {
        "rows": rows,
        "calendar": calendar,
        "adjustments": adjustments,
        "daily": daily,
        "index_rows": index_rows,
        "index_symbol": "000300.SH",
        "issues": issues,
    }
