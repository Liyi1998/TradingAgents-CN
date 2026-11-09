"""
AKShare 每日指标 API（PE/PB/PS/市值/换手率等）

数据来源：百度股市通（stock_zh_valuation_baidu）+ 东方财富（stock_zh_a_hist）
- 百度股市通提供：总市值、PE(TTM)、PE(静)、PB、市现率
- 东方财富行情提供：换手率
"""
import asyncio
from datetime import date, datetime
import logging
import re
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.data.sources.base.exceptions import (
    DataFormatError,
    DataNotFoundError,
    DataSourceUnavailableError,
)
from app.data.sources.base.mappers import map_network_exception

logger = logging.getLogger(__name__)

_DOMAIN = "daily_indicators"
_HISTORY_SUBSOURCE_TIMEOUT = 8.0  # one missing optional sub-source must not erase completed fields


def parse_indicator_date(value: str) -> date:
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("Indicator dates must be YYYY-MM-DD or YYYYMMDD")
    return date.fromisoformat(text)


def snapshot_capture_date(trade_date: str, captured_at: datetime, is_open: bool) -> str:
    """Capture time limits scope; it does NOT prove a vendor observation date."""
    local = captured_at.astimezone(ZoneInfo("Asia/Shanghai"))
    requested = parse_indicator_date(trade_date) if trade_date else local.date()
    if requested != local.date() or not is_open or local.hour < 15:
        raise ValueError("Undated spot snapshots cannot satisfy historical or unclosed-session requests")
    return requested.isoformat()


def merge_indicator_history_frames(results: Dict, code: str,
                                   start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """Merge actual vendor dates, keeping PB-only successes and sparse dates."""
    cleaned = {}
    for field, frame in results.items():
        if frame is None or frame.empty:
            continue
        extra = [name for name in ("_unadjusted_close", "_quote_method")
                 if field == "turnover_rate" and name in frame]
        current = frame[["date", field, *extra]].copy()
        current["date"] = pd.to_datetime(current["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        current = current[current["date"].notna()].drop_duplicates("date", keep="last")
        if not current.empty:
            cleaned[field] = current
    if not cleaned:
        raise DataNotFoundError("akshare", _DOMAIN, f"code={code} no dated indicator observations")
    merged = pd.concat([frame[["date"]] for frame in cleaned.values()]).drop_duplicates("date")
    for frame in cleaned.values():
        merged = merged.merge(frame, on="date", how="left", validate="one_to_one")
    if start_date:
        merged = merged[merged["date"] >= parse_indicator_date(start_date).isoformat()]
    if end_date:
        merged = merged[merged["date"] <= parse_indicator_date(end_date).isoformat()]
    merged["symbol"] = code
    merged["_indicator_method"] = "baidu_valuation_history"
    merged["_date_verified"] = True
    return merged.sort_values("date").reset_index(drop=True)


async def fetch_daily_indicators_batch(trade_date: str) -> pd.DataFrame:
    """收盘后当日快照；源无时间戳，因此不可作为已验证的历史因子证据。"""
    from app.data.core.market import is_trading_day

    captured_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    # Reject backdating before any upstream request (even on a trading holiday).
    requested = parse_indicator_date(trade_date) if trade_date else captured_at.date()
    if requested != captured_at.date():
        raise DataNotFoundError("akshare", _DOMAIN, "Spot snapshots do not provide historical indicators")
    try:
        capture_date = snapshot_capture_date(
            trade_date, captured_at, await is_trading_day("CN", captured_at.date()))
    except ValueError as exc:
        raise DataNotFoundError("akshare", _DOMAIN, str(exc)) from exc
    try:
        import akshare as ak

        from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit

        def _fetch():
            wait_rate_limit()
            return ak.stock_zh_a_spot_em()

        frame = await asyncio.to_thread(_fetch)
    except Exception as exc:
        raise DataSourceUnavailableError(
            "akshare", _DOMAIN, f"全市场快照: {exc}"
        ) from exc
    if frame is None or frame.empty:
        raise DataNotFoundError("akshare", _DOMAIN, "全市场指标快照为空")

    result = pd.DataFrame(
        {
            "symbol": frame["代码"].astype(str).str.zfill(6),
            "trade_date": capture_date,
            "pe_dynamic": frame.get("市盈率-动态"),
            "pb": frame.get("市净率"),
            "turnover_rate": frame.get("换手率"),
            # Adapter 对百度估值使用亿元换算；批量快照明确标为元，避免重复放大。
            "total_mv": frame.get("总市值"),
            "circ_mv": frame.get("流通市值"),
            "volume_ratio": frame.get("量比"),
            "_mv_unit": "元",
            "_indicator_method": "eastmoney_spot_capture",
            "_date_verified": False,
        }
    )
    return result


async def fetch_daily_indicators_by_symbol(
    code: str, start_date: str = None, end_date: str = None
) -> pd.DataFrame:
    """
    获取单只股票每日估值指标（PE/PB/PS/总市值/换手率）。

    通过百度股市通获取估值数据，通过东方财富行情获取换手率，
    合并为一个 DataFrame 返回。

    Raises:
        NetworkError: 网络/超时异常（可重试）
        DataFormatError: AKShare 返回结构异常（不可重试）
        DataNotFoundError: 所有子源均无数据（不可重试）
        DataSourceUnavailableError: 其他未知异常
    """
    try:
        import akshare as ak
        from app.data.sources.cn.akshare.api.cash_dividends import fetch_cash_dividend_history

        # 确定时间范围
        period = _determine_period(start_date, end_date)
        start_date = parse_indicator_date(start_date).isoformat() if start_date else None
        end_date = parse_indicator_date(end_date).isoformat() if end_date else None

        # 用 dict 组织并发任务，避免依赖 results 的位置索引
        # key 为输出列名（与 _indicator_to_column 一致），value 为协程
        task_map: Dict[str, "asyncio.Future"] = {
            "total_mv": _fetch_valuation_safe(ak, code, "总市值", period),
            "pe_ttm": _fetch_valuation_safe(ak, code, "市盈率(TTM)", period),
            "pb": _fetch_valuation_safe(ak, code, "市净率", period),
            "turnover_rate": _fetch_turnover_from_quotes(ak, code, start_date, end_date),
            "_cash_dividends": fetch_cash_dividend_history(code),
        }

        # 并发执行；return_exceptions=True 让单个子源失败不致整批抛错
        keys = list(task_map.keys())
        raw_results = await asyncio.gather(
            *(asyncio.wait_for(task, timeout=_HISTORY_SUBSOURCE_TIMEOUT) for task in task_map.values()),
            return_exceptions=True,
        )
    except (asyncio.TimeoutError, ConnectionError, TimeoutError) as exc:
        # 网络异常：可重试
        raise map_network_exception(exc, "akshare", _DOMAIN)
    except (KeyError, IndexError, AttributeError, ValueError) as exc:
        # 数据格式异常：AKShare 返回结构不符合预期，不可重试
        raise DataFormatError("akshare", _DOMAIN, f"code={code}: {exc}")
    except Exception as exc:
        # 其他未知异常
        raise DataSourceUnavailableError("akshare", _DOMAIN, f"code={code}: {exc}")

    # 把 results 与 key 关联起来（Exception 转为 None）
    results: Dict[str, Optional[pd.DataFrame]] = {
        k: (None if isinstance(r, Exception) else r)
        for k, r in zip(keys, raw_results)
    }

    history = results.pop("_cash_dividends", None)
    merged = merge_indicator_history_frames(results, code, start_date, end_date)
    merged = attach_cash_dividend_yields(merged, history)
    logger.debug(f"AKShare 每日指标: {code} {len(merged)} 条")
    return merged.reset_index(drop=True)


def attach_cash_dividend_yields(frame: pd.DataFrame, history) -> pd.DataFrame:
    """Use only same-row, dated, unadjusted closes; never carry a yield forward."""
    from app.data.sources.cn.akshare.api.cash_dividends import derive_cash_dividend_yield

    frame = frame.copy()
    results = []
    for _, row in frame.iterrows():
        if history is None:
            derived = {"value": None, "status": "unavailable", "reason": "dividend_source_unavailable"}
        else:
            derived = derive_cash_dividend_yield(
                history, as_of=row["date"], close=row.get("_unadjusted_close"),
                close_date=row["date"], close_basis="unadjusted", close_source=row.get("_quote_method"),
            )
        results.append(derived)
    frame["dividend_yield_ttm"] = [result["value"] for result in results]
    frame["_dividend_ttm_provenance"] = [result.get("provenance") for result in results]
    frame["_dividend_ttm_diagnostic"] = [
        {key: value for key, value in result.items() if key not in {"value", "provenance"}}
        for result in results
    ]
    return frame


async def _fetch_valuation_safe(ak_module, code: str, indicator: str, period: str) -> Optional[pd.DataFrame]:
    """安全获取百度估值数据，失败返回 None。"""
    try:
        from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit

        def _fetch():
            wait_rate_limit()
            return ak_module.stock_zh_valuation_baidu(
                symbol=code, indicator=indicator, period=period,
            )

        df = await asyncio.to_thread(_fetch)
        if df is None or df.empty:
            return None

        col_name = _indicator_to_column(indicator)
        result = df[["date", "value"]].copy()
        result.columns = ["date", col_name]
        return result
    except Exception as e:
        logger.debug(f"AKShare 百度估值 {code}/{indicator} 获取失败: {e}")
        return None


async def _fetch_turnover_from_quotes(
    ak_module, code: str, start_date: str = None, end_date: str = None
) -> Optional[pd.DataFrame]:
    """Reuse the real raw-price route/adapter, including its Sina fallback.

    Missing turnover must not discard a usable dividend denominator. Neither a
    qfq close nor an unverified sub-source can become a raw price by relabeling.
    ``ak_module`` remains for compatibility with the existing private call.
    """
    try:
        from app.data.sources.cn.akshare.api.daily_quotes import fetch_daily_quotes

        start = (start_date or "20200101").replace("-", "")
        end = (end_date or "20991231").replace("-", "")
        df = await fetch_daily_quotes(code, start, end, period="daily", adjust="")
        if df is None or df.empty:
            return None
        return quote_history_indicator_frame(df)
    except Exception as e:
        logger.debug(f"AKShare 换手率 {code} 获取失败: {e}")
        return None


def quote_history_indicator_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Extract standardized dates and prices without assuming turnover exists."""
    from app.data.schema.domains.daily_indicators import finite_indicator
    from app.data.sources.cn.akshare.adapter import AKShareCNAdapter

    rows = []
    for quote in AKShareCNAdapter().adapt_daily_quotes(raw):
        conversion = quote.unit_conversion or ""
        method = ("eastmoney_daily_history" if conversion.startswith("akshare:stock_zh_a_hist/eastmoney;")
                  else "sina_daily_history" if conversion.startswith("akshare:stock_zh_a_daily/sina;") else None)
        close = (quote.close if method and quote.price_basis == "raw"
                 and finite_indicator(quote.close) and quote.close > 0 else None)
        rows.append({
            "date": quote.trade_date, "turnover_rate": quote.turnover_rate,
            "_unadjusted_close": close, "_quote_method": method,
        })
    return pd.DataFrame(rows, columns=["date", "turnover_rate", "_unadjusted_close", "_quote_method"])


def _indicator_to_column(indicator: str) -> str:
    """将百度估值指标名映射为内部字段名。"""
    mapping = {
        "总市值": "total_mv",
        "市盈率(TTM)": "pe_ttm",
        "市盈率(静)": "pe",
        "市净率": "pb",
        "市现率": "ps_ttm",
    }
    return mapping.get(indicator, indicator)


def _determine_period(start_date: str = None, end_date: str = None, *, today: date = None) -> str:
    """根据日期范围确定百度 API 的 period 参数。"""
    if not start_date and not end_date:
        return "近一年"

    current = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    end = parse_indicator_date(end_date) if end_date else current
    start = parse_indicator_date(start_date) if start_date else date(2020, 1, 1)
    if start > end:
        raise ValueError("Indicator start date must not follow end date")
    # Vendor periods are relative to NOW, not relative to the requested end.
    # A one-month range years ago still needs a multi-year upstream window.
    delta_days = (max(end, current) - start).days
    if delta_days > 365 * 8:
        return "全部"
    elif delta_days > 365 * 4:
        return "近十年"
    elif delta_days > 365 * 2:
        return "近五年"
    elif delta_days > 365:
        return "近三年"
    else:
        return "近一年"
