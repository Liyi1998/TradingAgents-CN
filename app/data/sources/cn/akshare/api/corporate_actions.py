"""AKShare A 股公司行为（免费分红送配）接口。"""

import asyncio
import logging

import pandas as pd

from app.data.sources.base.exceptions import (
    DataFormatError,
    DataNotFoundError,
    DataSourceUnavailableError,
)

logger = logging.getLogger(__name__)
_DOMAIN = "corporate_actions"


async def fetch_corporate_actions(
    symbol: str, start_date: str = "", end_date: str = ""
) -> pd.DataFrame:
    """获取个股历史分红、送股和转增记录，不依赖 Tushare。"""
    try:
        import akshare as ak

        def _fetch():
            from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit

            wait_rate_limit()
            # 巨潮资讯字段稳定且包含派息日；失败时降级到东方财富。
            try:
                frame = ak.stock_dividend_cninfo(symbol=symbol.zfill(6))
                if frame is not None and not frame.empty:
                    return frame
            except Exception as exc:
                logger.info("巨潮分红接口失败，降级东方财富: %s", exc)
            return ak.stock_fhps_detail_em(symbol=symbol.zfill(6))

        frame = await asyncio.to_thread(_fetch)
    except (KeyError, IndexError, AttributeError, ValueError) as exc:
        raise DataFormatError("akshare", _DOMAIN, f"symbol={symbol}: {exc}") from exc
    except Exception as exc:
        raise DataSourceUnavailableError(
            "akshare", _DOMAIN, f"symbol={symbol}: {exc}"
        ) from exc

    if frame is None or frame.empty:
        raise DataNotFoundError("akshare", _DOMAIN, f"symbol={symbol} 无公司行为")

    frame = frame.copy()
    frame["symbol"] = symbol.zfill(6)
    date_col = next(
        (name for name in ("除权日", "除权除息日") if name in frame.columns), None
    )
    if date_col and (start_date or end_date):
        dates = pd.to_datetime(frame[date_col], errors="coerce")
        mask = dates.notna()
        if start_date:
            mask &= dates >= pd.Timestamp(start_date)
        if end_date:
            mask &= dates <= pd.Timestamp(end_date)
        frame = frame[mask].copy()
    if frame.empty:
        raise DataNotFoundError(
            "akshare", _DOMAIN, f"symbol={symbol} 在请求日期范围内无公司行为"
        )
    return frame
