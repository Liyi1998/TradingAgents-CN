"""腾讯财经 A 股准实时行情接口封装。"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import urllib.error
import urllib.request
from typing import List

import pandas as pd

from app.data.sources.base.exceptions import DataFormatError, DataNotFoundError
from app.data.sources.base.mappers import map_network_exception

logger = logging.getLogger(__name__)

_SOURCE = "tencent"
_DOMAIN = "market_quotes"
_QUOTE_URL = "https://qt.gtimg.cn/q="


def _quote_code(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if "." in value:
        code, suffix = value.split(".", 1)
        return f"{suffix.lower()}{code}"
    if value.startswith(("SH", "SZ", "BJ")):
        return value.lower()
    code = value.zfill(6)
    if code.startswith(("4", "8")):
        return f"bj{code}"
    if code.startswith(("6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _as_float(parts: list[str], index: int, multiplier: float = 1.0):
    try:
        value = parts[index]
        return float(value) * multiplier if value else None
    except (IndexError, TypeError, ValueError):
        return None


async def fetch_market_quotes(symbols: List[str]) -> pd.DataFrame:
    if not symbols:
        raise DataNotFoundError(_SOURCE, _DOMAIN, "股票代码为空")

    identities = {_quote_code(symbol): str(symbol).upper() for symbol in symbols}
    codes = ",".join(identities)

    def _fetch() -> str:
        request = urllib.request.Request(
            f"{_QUOTE_URL}{codes}", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read().decode("gbk", errors="replace")

    try:
        text = await asyncio.to_thread(_fetch)
    except (
        asyncio.TimeoutError,
        ConnectionError,
        TimeoutError,
        urllib.error.URLError,
        socket.timeout,
    ) as exc:
        raise map_network_exception(exc, _SOURCE, _DOMAIN) from exc

    records = []
    candidates = 0
    for line in text.split(";"):
        if "~" not in line:
            continue
        parts = line.strip().split("~")
        if len(parts) < 40:
            continue
        candidates += 1
        symbol = str(parts[2] or "").zfill(6)
        if not symbol.strip("0"):
            continue
        match = re.search(r"v_((sh|sz|bj)\d+)=", line)
        quote_code = match.group(1) if match else ""
        original = identities.get(quote_code, symbol)
        exchange = quote_code[:2].upper() if quote_code else ""
        is_index = original in {"000001.SH", "000300.SH", "399001.SZ", "399006.SZ"}
        records.append(
            {
                "symbol": original if is_index else symbol,
                "exchange": exchange,
                "asset_type": "index" if is_index else "stock",
                "name": parts[1].strip(),
                "price": _as_float(parts, 3),
                "pre_close": _as_float(parts, 4),
                "open": _as_float(parts, 5),
                # 腾讯 A 股成交量单位为手，标准 Schema 使用股。
                "volume": _as_float(parts, 6, 100),
                "update_time": parts[30],
                "pct_chg": _as_float(parts, 32),
                "high": _as_float(parts, 33),
                "low": _as_float(parts, 34),
                # 腾讯 A 股成交额字段单位为万元，标准 Schema 使用元。
                "amount": _as_float(parts, 37, 10000),
                "turnover_rate": _as_float(parts, 38),
                # The position is observed; its TTM/static/dynamic basis is not
                # verified by this contract. Keep the reported value separate.
                "pe_reported": _as_float(parts, 39),
                # 腾讯行情的总市值/流通市值单位为亿元，统一转成元。
                "total_mv": _as_float(parts, 44, 100_000_000),
                "circ_mv": _as_float(parts, 45, 100_000_000),
                "pb": _as_float(parts, 46),
            }
        )

    if candidates and not records:
        raise DataFormatError(_SOURCE, _DOMAIN, "腾讯行情响应格式无法解析")
    if not records:
        raise DataNotFoundError(_SOURCE, _DOMAIN, f"symbols={symbols} 无数据")
    return pd.DataFrame(records)
