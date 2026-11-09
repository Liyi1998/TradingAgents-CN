"""AKShare 资金流向 API"""
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

_DOMAIN = "money_flow"


def money_flow_market(symbol: str) -> tuple[str, str]:
    """Route a validated identity, including BSE, to AKShare's market code."""
    from app.data.schema.domains.money_flow import normalize_money_flow_symbol
    from app.data.sources.cn.stock_name_utils import infer_exchange

    code = normalize_money_flow_symbol(symbol)
    exchange = infer_exchange(code)
    market = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(exchange)
    suffix = str(symbol).strip().upper().rsplit(".", 1)
    if market is None or (len(suffix) == 2 and suffix[1].lower() != market):
        raise ValueError("money-flow symbol/exchange mismatch")
    return code, market


def bind_money_flow_identity(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Attach only the requested identity; never invent or rewrite source dates."""
    from app.data.schema.domains.money_flow import normalize_money_flow_symbol

    code, _ = money_flow_market(symbol)
    result = frame.copy()
    for column in ("symbol", "股票代码", "代码"):
        if column in result:
            for value in result[column].dropna():
                if normalize_money_flow_symbol(value) != code:
                    raise ValueError("money-flow response symbol mismatch")
    if "日期" not in result:
        raise ValueError("money-flow history has no source date")
    # A column name alone is not date evidence; invalid/empty source dates fail.
    dates = pd.to_datetime(result["日期"], errors="coerce")
    if dates.isna().any():
        raise ValueError("money-flow history has invalid source dates")
    result["symbol"] = code
    result["flow_api"] = "stock_individual_fund_flow"
    result["flow_date_verified"] = True
    result["flow_date_basis"] = "source_row_date"
    return result


async def fetch_money_flow_by_symbol(
    symbol: str,
) -> pd.DataFrame:
    """获取源实际提供的近约100个交易日资金流，调用方按日期过滤。

    不把当日排名扩展成历史，也不承诺超出源窗口的任意旧日期可用。

    Args:
        symbol: 6位股票代码（不带后缀）

    Raises:
        NetworkError: 网络/超时异常（可重试）
        DataFormatError: AKShare 返回结构异常（不可重试）
        DataNotFoundError: 返回空数据（不可重试）
        DataSourceUnavailableError: 其他未知异常
    """
    try:
        code, market = money_flow_market(symbol)
        import akshare as ak

        def _fetch():
            from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit
            wait_rate_limit()
            return ak.stock_individual_fund_flow(stock=code, market=market)

        df = await asyncio.to_thread(_fetch)
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
    if is_empty_result(df):
        logger.warning(f"AKShare 资金流向返回空: symbol={symbol}")
        raise DataNotFoundError("akshare", _DOMAIN, f"symbol={symbol} 无资金流向数据")

    try:
        df = bind_money_flow_identity(df, symbol)
    except (TypeError, ValueError) as exc:
        raise DataFormatError("akshare", _DOMAIN, f"symbol={symbol}: {exc}") from exc

    logger.debug(f"AKShare 资金流向: {symbol} {len(df)} 条")
    return df


async def fetch_money_flow_rank(trade_date: str) -> pd.DataFrame:
    """全市场资金流排名（东财 ak.stock_individual_fund_flow_rank）。

    该接口只提供「今日」快照且不含日期列，历史日期不可查：调用方传入
    trade_date，非当日直接抛 DataNotFoundError（批量同步按日循环时跳过）；
    当日仅注入采集日并标记日期未验证，不得用于已完成五日历史计算。
    单位与逐股接口一致（元），adapter 直接映射。
    """
    from datetime import datetime

    from app.data.core.market import get_market_timezone

    date_str = str(trade_date).replace("-", "")
    today = datetime.now(get_market_timezone("CN")).strftime("%Y%m%d")
    if date_str != today:
        raise DataNotFoundError(
            "akshare", _DOMAIN, f"排名接口仅支持当日数据: {trade_date}"
        )

    try:
        import akshare as ak

        def _fetch():
            from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit
            wait_rate_limit()
            return ak.stock_individual_fund_flow_rank(indicator="今日")

        df = await asyncio.to_thread(_fetch)
    except (asyncio.TimeoutError, ConnectionError, TimeoutError) as exc:
        raise map_network_exception(exc, "akshare", _DOMAIN)
    except (KeyError, IndexError, AttributeError, ValueError) as exc:
        raise DataFormatError("akshare", _DOMAIN, f"rank: {exc}")
    except Exception as exc:
        raise DataSourceUnavailableError("akshare", _DOMAIN, f"rank: {exc}")

    if is_empty_result(df):
        logger.warning("AKShare 资金流排名返回空")
        raise DataNotFoundError("akshare", _DOMAIN, "资金流排名无数据")

    df["日期"] = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    df["flow_api"] = "stock_individual_fund_flow_rank"
    # Ranking has no returned trading date. Capture time is not historical proof.
    df["flow_date_verified"] = False
    df["flow_date_basis"] = "capture_date"
    logger.info(f"AKShare 资金流排名: {len(df)} 条")
    return df
