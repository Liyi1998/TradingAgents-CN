"""
AKShare 复权因子 API — 通过 stock_zh_a_daily 的 qfq-factor / hfq-factor 模式获取。
"""
import asyncio
import logging

import pandas as pd

from app.data.sources.base.exceptions import (
    DataFormatError,
    DataNotFoundError,
    DataSourceUnavailableError,
)
from app.data.sources.base.mappers import is_empty_result, map_network_exception

logger = logging.getLogger(__name__)

_DOMAIN = "adj_factors"


def _to_sina_symbol(symbol: str) -> str:
    """标准代码 → 新浪格式 (sh600000 / sz000001 / bj430001)。"""
    code = str(symbol).zfill(6)
    if code.startswith(("60", "68", "90")):
        return f"sh{code}"
    elif code.startswith(("0", "3", "20")):
        return f"sz{code}"
    elif code.startswith(("4", "8", "92")):
        return f"bj{code}"
    return f"sh{code}"


async def fetch_adj_factors(
    symbol: str,
    start_date: str = None,
    end_date: str = None,
) -> pd.DataFrame:
    """获取 A 股复权因子（前复权 + 后复权）。

    分别调用 stock_zh_a_daily 的 qfq-factor 和 hfq-factor 模式，
    合并为包含 symbol / trade_date / qfq_factor / hfq_factor 的 DataFrame。
    """
    try:
        import akshare as ak

        from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit

        sina_code = _to_sina_symbol(symbol)

        def _fetch_qfq():
            wait_rate_limit()
            return ak.stock_zh_a_daily(symbol=sina_code, adjust="qfq-factor")

        def _fetch_hfq():
            wait_rate_limit()
            return ak.stock_zh_a_daily(symbol=sina_code, adjust="hfq-factor")

        qfq_df, hfq_df = await asyncio.gather(
            asyncio.to_thread(_fetch_qfq),
            asyncio.to_thread(_fetch_hfq),
        )
    except (asyncio.TimeoutError, ConnectionError, TimeoutError) as exc:
        # 网络异常：可重试
        raise map_network_exception(exc, "akshare", _DOMAIN)
    except (KeyError, IndexError, AttributeError, ValueError) as exc:
        # 数据格式异常：AKShare 返回结构不符合预期，不可重试
        raise DataFormatError("akshare", _DOMAIN, f"symbol={symbol}: {exc}")
    except Exception as exc:
        # 其他未知异常
        raise DataSourceUnavailableError("akshare", _DOMAIN, f"symbol={symbol}: {exc}")

    # 空结果：业务正确但无数据，不可重试
    if is_empty_result(qfq_df):
        logger.warning(f"AKShare 前复权因子为空: {symbol}")
        raise DataNotFoundError("akshare", _DOMAIN, f"symbol={symbol} 无前复权因子")

    # 重置索引使 date 成为普通列
    if qfq_df.index.name == "date":
        qfq_df = qfq_df.reset_index()
    if hfq_df is not None and not hfq_df.empty and hfq_df.index.name == "date":
        hfq_df = hfq_df.reset_index()

    # These are effective-date events, not a daily table. Normalize the full
    # response before selecting the interval, keeping its preceding anchor.
    # Sina qfq_factor is a divisor; storing it as a price multiplier was wrong.
    from app.data.processor.adjustment_contract import normalize_adjustment_events
    from app.utils.timezone import now_tz

    try:
        qfq_df = qfq_df.copy()
        qfq_df["date"] = pd.to_datetime(qfq_df["date"], errors="raise").dt.date
        if hfq_df is not None and not hfq_df.empty:
            hfq_df = hfq_df.copy()
            hfq_df["date"] = pd.to_datetime(hfq_df["date"], errors="raise").dt.date
            merged = qfq_df.merge(hfq_df[["date", "hfq_factor"]], on="date", how="left")
        else:
            merged = qfq_df
        normalized = normalize_adjustment_events(
            merged, source="akshare", symbol=str(symbol).zfill(6),
            start_date=start_date, end_date=end_date or now_tz().date().isoformat(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DataFormatError("akshare", _DOMAIN, f"symbol={symbol}: {exc}") from exc

    if normalized.empty:
        raise DataNotFoundError("akshare", _DOMAIN, f"symbol={symbol} 合并后无数据")

    logger.debug(f"AKShare 复权因子: {symbol} {len(normalized)} 条")
    return normalized
