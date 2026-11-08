"""
AKShare 日线行情 API
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

_DOMAIN = "daily_quotes"


def validate_daily_quote_options(period: str, adjust: str) -> None:
    if not isinstance(period, str) or period not in {"daily", "weekly", "monthly"}:
        raise DataFormatError("akshare", _DOMAIN, "period must be daily/weekly/monthly")
    if not isinstance(adjust, str) or adjust not in {"", "qfq", "hfq"}:
        raise DataFormatError("akshare", _DOMAIN, "adjust must be empty/raw, qfq or hfq")


def bind_daily_quote_source(df: pd.DataFrame, *, source_api: str) -> pd.DataFrame:
    """Attach actual API evidence; do not infer source units from column names.

    Official AKShare stock docs: stock_zh_a_hist volume is lots of 100 shares,
    while stock_zh_a_daily volume is shares. Both amount fields are CNY.
    https://akshare.akfamily.xyz/data/stock/stock.html
    Installed implementations only parse these volume/amount values; they do
    not perform the Eastmoney lot-to-share conversion for us.
    """
    contracts = {
        "stock_zh_a_hist": ("eastmoney", "lot100"),
        "stock_zh_a_daily": ("sina", "share"),
    }
    if source_api not in contracts:
        raise DataFormatError("akshare", _DOMAIN, "Unknown daily quote API contract")
    source, volume_unit = contracts[source_api]
    result = df.copy()
    result["quote_api"] = source_api
    result["quote_source"] = source
    result["quote_units_verified"] = True
    result["volume_unit"] = volume_unit
    result["amount_unit"] = "CNY"
    return result


async def fetch_daily_quotes(
    code: str,
    start_date: str,
    end_date: str,
    period: str = "daily",
    adjust: str = "qfq",
) -> pd.DataFrame:
    """获取 A 股历史行情

    Raises:
        NetworkError: 网络/超时异常（可重试）
        DataFormatError: AKShare 返回结构异常（不可重试）
        DataNotFoundError: 返回空数据（不可重试）
        DataSourceUnavailableError: 其他未知异常
    """
    validate_daily_quote_options(period, adjust)
    try:
        import akshare as ak

        period_map = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}
        ak_period = period_map.get(period, "daily")

        def _fetch():
            from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit
            wait_rate_limit()
            try:
                primary_df = ak.stock_zh_a_hist(
                    symbol=code, period=ak_period,
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust=adjust,
                )
                return primary_df, "stock_zh_a_hist"
            except Exception as exc:
                # 东财 stock_zh_a_hist 在部分网络环境被整体拒绝
                # （RemoteDisconnected）；回退到新浪 stock_zh_a_daily（仅日线，
                # 列名 date/open/... 与 adapter 的英文字段映射兼容）。
                # 注意 requests.ConnectionError 不是内置 ConnectionError 子类，
                # 这里按异常名判定网络族错误
                if period != "daily" or not isinstance(
                    exc, (ConnectionError, TimeoutError)
                ) and type(exc).__name__ not in (
                    "ConnectionError",
                    "ConnectTimeout",
                    "ReadTimeout",
                    "ChunkedEncodingError",
                    "ProtocolError",
                ):
                    raise
                from app.data.sources.cn.akshare.api.adj_factors import _to_sina_symbol
                wait_rate_limit()
                sina_df = ak.stock_zh_a_daily(
                    symbol=_to_sina_symbol(code), adjust=adjust if adjust != "" else "",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                )
                if sina_df is not None and not sina_df.empty:
                    sina_df = sina_df.reset_index() if sina_df.index.name == "date" else sina_df
                return sina_df, "stock_zh_a_daily"

        df, source_api = await asyncio.to_thread(_fetch)
    except (asyncio.TimeoutError, ConnectionError, TimeoutError) as exc:
        # 网络异常：可重试
        raise map_network_exception(exc, "akshare", _DOMAIN)
    except (KeyError, IndexError, AttributeError, ValueError) as exc:
        # 数据格式异常：AKShare 返回结构不符合预期，不可重试
        raise DataFormatError("akshare", _DOMAIN, f"code={code}: {exc}")
    except Exception as exc:
        # 其他未知异常
        raise DataSourceUnavailableError("akshare", _DOMAIN, f"code={code}: {exc}")

    # 空结果：业务正确但无数据，不可重试
    if is_empty_result(df):
        logger.warning(f"AKShare 行情返回空: code={code}")
        raise DataNotFoundError("akshare", _DOMAIN, f"code={code} 无行情数据")

    # 东财/新浪接口返回的 df 不含代码列，adapter 会把 symbol 解析成
    # "".zfill(6)="000000" 入库；与 daily_indicators 一致，这里注入 symbol 列
    df = bind_daily_quote_source(df, source_api=source_api)
    df["symbol"] = code
    df["quote_period"] = period
    # Declare the actual CN request basis. Do not certify a common historical
    # qfq normalization snapshot merely from the requested range end.
    df["price_basis"] = "raw" if adjust == "" else adjust
    if adjust in {"qfq", "hfq"}:
        from app.utils.timezone import now_tz
        df["adjustment_as_of"] = now_tz().date().isoformat()

    logger.debug(f"AKShare 行情: {code} {len(df)} 条")
    return df
