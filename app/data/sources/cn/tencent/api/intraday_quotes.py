"""Tencent native, historical 30-minute OHLCV; never synthesize extrema."""

import asyncio
import json
import math
import socket
from datetime import datetime
import urllib.error
import urllib.request

import pandas as pd

from app.data.sources.base.exceptions import DataFormatError, DataNotFoundError
from app.data.sources.base.mappers import map_network_exception
from .market_quotes import _quote_code

_SOURCE = "tencent"
_DOMAIN = "intraday_quotes"
_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline?param="


def parse_intraday_payload(payload: dict, symbol: str) -> list[dict]:
    """Read native bars only. Unspecified extra columns are not turnover."""
    quote_code = _quote_code(symbol)
    body = (payload.get("data") or {}).get(quote_code) or {}
    rows = body.get("m30") or []
    records = []
    for item in rows:
        if not isinstance(item, list) or len(item) < 6:
            continue
        try:
            stamp = datetime.strptime(str(item[0]), "%Y%m%d%H%M")
            if any(isinstance(value, bool) for value in item[1:6]):
                continue
            opened, closed, high, low, lots = (float(value) for value in item[1:6])
            if not all(math.isfinite(value) for value in (opened, closed, high, low, lots)):
                continue
            if min(opened, closed, high, low) <= 0 or lots < 0 or high < max(opened, closed) or low > min(opened, closed):
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        records.append({
            "symbol": str(symbol).split(".", 1)[0].zfill(6),
            "datetime": stamp.strftime("%Y-%m-%d %H:%M:%S"),
            "freq": "30min", "open": opened, "high": high, "low": low,
            "close": closed, "volume": lots * 100, "amount": None,
            "ohlc_kind": "native_bar", "price_basis": "raw",
            "volume_unit": "share", "amount_unit": None,
            "unit_conversion": "tencent_native_m30:lot*100;turnover_not_reported",
        })
    return records


async def fetch_intraday_quotes(symbol: str) -> pd.DataFrame:
    quote_code = _quote_code(symbol)

    def _fetch() -> bytes:
        request = urllib.request.Request(
            f"{_URL}{quote_code},m30,,160", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()

    try:
        payload = json.loads((await asyncio.to_thread(_fetch)).decode("utf-8"))
        records = parse_intraday_payload(payload, symbol)
    except (
        asyncio.TimeoutError, ConnectionError, TimeoutError,
        urllib.error.URLError, socket.timeout,
    ) as exc:
        raise map_network_exception(exc, _SOURCE, _DOMAIN) from exc
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
        raise DataFormatError(_SOURCE, _DOMAIN, str(exc)) from exc

    if not records:
        raise DataNotFoundError(_SOURCE, _DOMAIN, f"symbol={symbol} 无可验证的30分钟K线")
    return pd.DataFrame(records)
