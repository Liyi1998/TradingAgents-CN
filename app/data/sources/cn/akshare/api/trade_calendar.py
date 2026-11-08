"""AKShare A 股交易日历。"""

import asyncio
from datetime import timedelta

import pandas as pd

from app.data.sources.base.exceptions import DataNotFoundError, DataSourceUnavailableError


async def fetch_trade_calendar(
    exchange: str, start_date: str, end_date: str
) -> pd.DataFrame:
    """用新浪历史交易日构造包含休市日的日历。"""
    try:
        import akshare as ak

        frame = await asyncio.to_thread(ak.tool_trade_date_hist_sina)
    except Exception as exc:
        raise DataSourceUnavailableError(
            "akshare", "trade_calendar", str(exc)
        ) from exc
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        raise DataNotFoundError("akshare", "trade_calendar", "交易日列表为空")

    open_dates = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
    if open_dates.empty:
        raise DataNotFoundError("akshare", "trade_calendar", "交易日解析为空")
    requested_start = pd.to_datetime(start_date, errors="coerce")
    requested_end = pd.to_datetime(end_date, errors="coerce")
    lower = max(open_dates.min(), requested_start) if not pd.isna(requested_start) else open_dates.min()
    upper = min(open_dates.max(), requested_end) if not pd.isna(requested_end) else open_dates.max()
    if lower > upper:
        raise DataNotFoundError("akshare", "trade_calendar", "请求区间无已知交易日")

    open_set = {item.strftime("%Y-%m-%d") for item in open_dates}
    rows = []
    previous_open = None
    cursor = lower.normalize()
    while cursor <= upper.normalize():
        date_text = cursor.strftime("%Y-%m-%d")
        is_open = date_text in open_set
        rows.append(
            {
                "exchange": exchange or "SSE",
                "cal_date": date_text,
                "is_open": is_open,
                "pretrade_date": previous_open,
            }
        )
        if is_open:
            previous_open = date_text
        cursor += timedelta(days=1)
    return pd.DataFrame(rows)
