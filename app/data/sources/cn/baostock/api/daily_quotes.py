"""
BaoStock 日线行情 API
"""

import asyncio
import logging
from typing import Optional

import pandas as pd

from app.data.sources.base.exceptions import (
    DataFormatError,
    DataNotFoundError,
    DataSourceUnavailableError,
    NetworkError,
)
from app.data.sources.base.mappers import is_empty_result, map_network_exception
from .connection import baostock_session, bs

logger = logging.getLogger(__name__)

_DOMAIN_DAILY = "daily_quotes"
_DOMAIN_ADJ = "adj_factors"


def _to_baostock_code(symbol: str) -> str:
    """将纯数字代码转为 baostock 格式: sh.600000 / sz.000001

    BaoStock 不支持北交所（4xx/8xx/92x 开头），抛 DataNotFoundError。
    domain 参数由调用方上下文决定。
    """
    text = str(symbol).upper()
    if "." in text:
        code, suffix = text.rsplit(".", 1)
        if suffix in {"SH", "SZ"} and code.isdigit():
            return f"{suffix.lower()}.{code.zfill(6)}"
    code = str(symbol).zfill(6)
    if code.startswith(("4", "8", "92")):
        logger.warning(f"北交所股票 {code} 不被 BaoStock 支持")
        raise DataNotFoundError("baostock", "", f"北交所股票 {code} 不被 BaoStock 支持")
    if code.startswith(("60", "68", "90")):
        return f"sh.{code}"
    return f"sz.{code}"


def _check_rs_error(rs, source: str, domain: str, hint: str) -> None:
    """检查 BaoStock ResultSet 错误码，非 0 抛 DataSourceUnavailableError。

    BaoStock 的 rs.error_code 是字符串 '0' 表示成功，其他数字（6xxxx 系列多为
    网络/系统错误）按系统错误处理。
    """
    if rs is None:
        raise DataSourceUnavailableError(source, domain, f"{hint}: no response")
    err_code = getattr(rs, "error_code", "0")
    if err_code != "0":
        err_msg = getattr(rs, "error_msg", "") or ""
        raise DataSourceUnavailableError(
            source,
            domain,
            f"{hint}: error_code={err_code} {err_msg}".rstrip(),
        )


async def fetch_daily_quotes(
    code: str, start_date: str, end_date: str, adjust: str = ""
) -> Optional[pd.DataFrame]:
    """获取日线行情（默认不复权；显式请求可取前/后复权）

    异常分类：
    - asyncio.TimeoutError / ConnectionError / TimeoutError → NetworkError
    - rs.error_code != '0' → DataSourceUnavailableError
    - 空结果 → DataNotFoundError
    - DataFrame 组装异常 → DataFormatError
    - 其他 → DataSourceUnavailableError
    """
    flags = {"": "3", "raw": "3", "qfq": "2", "hfq": "1"}
    if not isinstance(adjust, str) or adjust not in flags:
        raise ValueError(f"unsupported BaoStock price adjustment: {adjust}")
    bs_code = _to_baostock_code(code)
    # BaoStock 要求 yyyy-mm-dd（带连字符），传 yyyyMMdd 会报“日期格式不正确”
    start, end = start_date, end_date
    hint = f"code={code}"

    def _fetch():
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag=flags[adjust],
        )
        _check_rs_error(rs, "baostock", _DOMAIN_DAILY, hint)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        return pd.DataFrame(rows, columns=rs.fields)

    try:
        async with baostock_session():
            try:
                df = await asyncio.to_thread(_fetch)
            except (asyncio.TimeoutError, ConnectionError, TimeoutError) as exc:
                raise map_network_exception(exc, "baostock", _DOMAIN_DAILY)
            except (KeyError, ValueError, AttributeError) as exc:
                raise DataFormatError("baostock", _DOMAIN_DAILY, f"{hint}: {exc}")

        if is_empty_result(df):
            logger.warning(f"BaoStock {_DOMAIN_DAILY} 返回空: {hint}")
            raise DataNotFoundError("baostock", _DOMAIN_DAILY, f"{hint} 无数据")

        from app.utils.timezone import now_tz
        df = df.copy()
        df["price_basis"] = "raw" if flags[adjust] == "3" else adjust
        if flags[adjust] != "3":
            df["adjustment_as_of"] = now_tz().date().isoformat()
        logger.info(f"BaoStock 行情: {code} {len(df)} 条")
        return df

    except (
        NetworkError,
        DataNotFoundError,
        DataSourceUnavailableError,
        DataFormatError,
    ):
        # 已分类的异常向上抛出，不再吞掉
        raise
    except Exception as exc:
        raise DataSourceUnavailableError(
            "baostock", _DOMAIN_DAILY, f"{hint}: {exc}"
        ) from exc


async def fetch_adj_factors(
    code: str, start_date: str, end_date: str
) -> Optional[pd.DataFrame]:
    """获取复权因子

    异常分类同 fetch_daily_quotes。
    """
    bs_code = _to_baostock_code(code)
    # An event before the requested quote window is its initial anchor. Query
    # the source's complete CN event history, then bound the normalized result;
    # requesting only [start_date,end_date] silently loses that anchor.
    from app.utils.timezone import now_tz
    start, end = "1990-01-01", end_date or now_tz().date().isoformat()
    hint = f"code={code}"

    def _fetch():
        rs = bs.query_adjust_factor(
            code=bs_code,
            start_date=start,
            end_date=end,
        )
        _check_rs_error(rs, "baostock", _DOMAIN_ADJ, hint)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        return pd.DataFrame(rows, columns=rs.fields)

    try:
        async with baostock_session():
            try:
                df = await asyncio.to_thread(_fetch)
            except (asyncio.TimeoutError, ConnectionError, TimeoutError) as exc:
                raise map_network_exception(exc, "baostock", _DOMAIN_ADJ)
            except (KeyError, ValueError, AttributeError) as exc:
                raise DataFormatError("baostock", _DOMAIN_ADJ, f"{hint}: {exc}")

        if is_empty_result(df):
            logger.warning(f"BaoStock {_DOMAIN_ADJ} 返回空: {hint}")
            raise DataNotFoundError("baostock", _DOMAIN_ADJ, f"{hint} 无数据")

        from app.data.processor.adjustment_contract import normalize_adjustment_events

        try:
            normalized = normalize_adjustment_events(
                df, source="baostock", symbol=bs_code.split(".")[-1],
                start_date=start_date, end_date=end,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataFormatError("baostock", _DOMAIN_ADJ, f"{hint}: {exc}") from exc
        if normalized.empty:
            raise DataNotFoundError("baostock", _DOMAIN_ADJ, f"{hint} 无有效复权事件")
        logger.info(f"BaoStock 复权因子: {code} {len(normalized)} 条")
        return normalized

    except (
        NetworkError,
        DataNotFoundError,
        DataSourceUnavailableError,
        DataFormatError,
    ):
        raise
    except Exception as exc:
        raise DataSourceUnavailableError(
            "baostock", _DOMAIN_ADJ, f"{hint}: {exc}"
        ) from exc
