"""AKShare 融资融券 API"""
import asyncio
import inspect
import logging

import pandas as pd

from app.data.sources.base.exceptions import (
    DataFormatError,
    DataNotFoundError,
    DataSourceUnavailableError,
)
from app.data.sources.base.mappers import is_empty_result, map_network_exception

logger = logging.getLogger(__name__)

_DOMAIN = "margin_trading"


async def fetch_margin_trading(
    symbol: str,
    trade_date: str | None = None,
) -> pd.DataFrame:
    """获取个股融资融券数据。

    Args:
        symbol: 6位股票代码（不带后缀）

    Raises:
        NetworkError: 网络/超时异常（可重试）
        DataFormatError: AKShare 返回结构异常（不可重试）
        DataNotFoundError: 返回空数据（不可重试）
        DataSourceUnavailableError: 其他未知异常
    """
    try:
        import akshare as ak

        def _fetch():
            from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit
            wait_rate_limit()
            code = symbol.zfill(6)
            if code.startswith(("6", "9")):
                fn = ak.stock_margin_detail_sse
            elif code.startswith(("0", "2", "3")):
                fn = ak.stock_margin_detail_szse
            else:
                return None

            # 新版 AKShare 的沪深接口均改为按 date 返回全市场明细；旧版沪市接口
            # 曾接受 code。按实际签名调用，避免版本升级后 unexpected keyword。
            params = inspect.signature(fn).parameters
            if "date" in params:
                frame = None
                last_empty_date_error = None
                # 交易所接口在周末、节假日或当日数据尚未发布时，
                # 有的 AKShare 版本会直接抛出空表 Length mismatch。
                # 逐日回退至最近可用交易日，避免把正常缺数据误判为数据源故障。
                for candidate in _candidate_trade_dates(trade_date):
                    try:
                        candidate_frame = fn(date=candidate)
                    except ValueError as exc:
                        if "Length mismatch" not in str(exc):
                            raise
                        last_empty_date_error = exc
                        continue
                    filtered = _filter_symbol(candidate_frame, code)
                    if not is_empty_result(filtered):
                        # SZSE's dated endpoint omits the date column in its
                        # response. Preserve the actual successful query date,
                        # including a previous trading day used as fallback.
                        filtered = filtered.copy()
                        if not any(column in filtered.columns for column in ("信用交易日期", "日期", "trade_date")):
                            filtered["trade_date"] = f"{candidate[:4]}-{candidate[4:6]}-{candidate[6:8]}"
                        return filtered
                    frame = filtered
                if frame is None and last_empty_date_error is not None:
                    raise last_empty_date_error
            elif "code" in params:
                frame = fn(code=code)
            else:
                frame = fn()
            return _filter_symbol(frame, code)

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
        logger.warning(f"AKShare 融资融券返回空: symbol={symbol}")
        raise DataNotFoundError("akshare", _DOMAIN, f"symbol={symbol} 无融资融券数据")

    logger.info(f"AKShare 融资融券: {symbol} {len(df)} 条")
    return df


def _filter_symbol(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """从交易所全市场明细中筛出目标股票；兼容沪深字段名差异。"""
    if df is None or df.empty:
        return df
    for col in ("标的证券代码", "证券代码", "股票代码", "代码"):
        if col in df.columns:
            return df[df[col].astype(str).str.extract(r"(\d{6})", expand=False) == symbol]
    # 旧接口可能已经是单股结果；仅在确实没有代码列时原样返回。
    return df


def _candidate_trade_dates(reference_date: str | None = None) -> list[str]:
    """返回待尝试的近期工作日（YYYYMMDD，新到旧）。

    AKShare 深市明细接口 stock_margin_detail_szse 按日期查询全市场，
    需传入一个有效的交易日。保留 10 个工作日候选，可覆盖
    普通周末、长假和当日数据延迟，又不需要在同步函数里调用交易日历。
    """
    from datetime import datetime, timedelta

    try:
        today = (
            datetime.strptime(str(reference_date)[:10], "%Y-%m-%d")
            if reference_date
            else datetime.now()
        )
    except ValueError:
        today = datetime.now()

    candidates = []
    for offset in range(0, 16):
        day = today - timedelta(days=offset)
        if day.weekday() < 5:
            candidates.append(day.strftime("%Y%m%d"))
        if len(candidates) == 10:
            break
    return candidates
