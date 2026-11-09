"""
BaoStock 股票基础信息 API
"""
import asyncio
import logging
from datetime import date
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
from .daily_quotes import _check_rs_error, _to_baostock_code
from app.data.processor.industry_contract import INDUSTRY_CONTRACT, industry_label

logger = logging.getLogger(__name__)

_DOMAIN = "stock_basic"


def merge_industry_data(basic: pd.DataFrame, industries: pd.DataFrame, as_of: str) -> pd.DataFrame:
    """Join the source's dated taxonomy, never its security-type field."""
    fields = {"code", "updateDate", "industry", "industryClassification"}
    if not fields.issubset(industries.columns):
        raise ValueError("BaoStock industry response lacks dated taxonomy fields")
    rows = industries.copy()
    rows["industry"] = rows["industry"].map(industry_label)
    rows["industryClassification"] = rows["industryClassification"].map(industry_label)
    dates = pd.to_datetime(rows["updateDate"], errors="coerce")
    rows = rows[dates.notna() & (dates <= pd.Timestamp(as_of))
                & rows["industry"].notna() & rows["industryClassification"].notna()].copy()
    rows["industry_as_of"] = pd.to_datetime(rows["updateDate"]).dt.strftime("%Y-%m-%d")
    # A conflicting same-date label is not resolved by arbitrary row ordering.
    conflict = rows.groupby(["code", "industry_as_of"])[["industry", "industryClassification"]].nunique()
    if (conflict > 1).any().any():
        raise ValueError("conflicting BaoStock industry labels")
    rows = rows.sort_values("industry_as_of").drop_duplicates("code", keep="last")
    rows["industry_source"] = "baostock:query_stock_industry"
    rows["industry_contract"] = INDUSTRY_CONTRACT
    rows = rows.rename(columns={"industryClassification": "industry_classification"})
    keep = ["code", "industry", "industry_source", "industry_classification", "industry_as_of", "industry_contract"]
    return basic.merge(rows[keep], on="code", how="left", validate="one_to_one")


async def fetch_stock_list(symbol: Optional[str] = None, end_date: Optional[str] = None) -> Optional[pd.DataFrame]:
    """获取 A 股股票列表

    异常分类：
    - asyncio.TimeoutError / ConnectionError / TimeoutError → NetworkError
    - rs.error_code != '0' → DataSourceUnavailableError
    - 空结果 → DataNotFoundError
    - DataFrame 组装异常 → DataFormatError
    - 其他 → DataSourceUnavailableError
    """

    cutoff = min(date.fromisoformat(end_date), date.today()).isoformat() if end_date else date.today().isoformat()
    code = _to_baostock_code(symbol) if symbol and symbol != "__all__" else ""

    def _fetch():
        rs = bs.query_stock_basic(code=code)
        _check_rs_error(rs, "baostock", _DOMAIN, "stock_list")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        df = df[df["type"] == "1"]
        industry_rs = bs.query_stock_industry(code=code, date=cutoff)
        _check_rs_error(industry_rs, "baostock", _DOMAIN, "query_stock_industry")
        industry_rows = []
        while industry_rs.next():
            industry_rows.append(industry_rs.get_row_data())
        industries = pd.DataFrame(industry_rows, columns=industry_rs.fields)
        return merge_industry_data(df, industries, cutoff)

    try:
        async with baostock_session():
            try:
                df = await asyncio.to_thread(_fetch)
            except (asyncio.TimeoutError, ConnectionError, TimeoutError) as exc:
                raise map_network_exception(exc, "baostock", _DOMAIN)
            except (KeyError, ValueError, AttributeError) as exc:
                raise DataFormatError("baostock", _DOMAIN, str(exc))

        if is_empty_result(df):
            logger.warning(f"BaoStock {_DOMAIN} 返回空: 股票列表无数据")
            raise DataNotFoundError(
                "baostock", _DOMAIN, "query_stock_basic 返回空"
            )

        # 去掉 sh./sz. 前缀
        if "code" in df.columns:
            df["code"] = df["code"].str.replace(r"^(sh|sz)\.", "", regex=True)
        logger.info(f"BaoStock 股票列表: {len(df)} 只")
        return df

    except (
        NetworkError,
        DataNotFoundError,
        DataSourceUnavailableError,
        DataFormatError,
    ):
        raise
    except Exception as exc:
        raise DataSourceUnavailableError(
            "baostock", _DOMAIN, str(exc)
        ) from exc
