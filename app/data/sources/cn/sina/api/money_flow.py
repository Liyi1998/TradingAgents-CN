"""新浪财经个股日级资金流。

作为东财 push2/push2his 受 IP 风控时的独立备用源。
端点与风控域名均不同，不会与 AKShare 的东财请求一起失效。
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request

import pandas as pd

from app.data.sources.base.exceptions import (
    DataFormatError,
    DataNotFoundError,
    DataSourceUnavailableError,
)
from app.data.sources.base.mappers import map_network_exception

_DOMAIN = "money_flow"
_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_qsfx_zjlrqs"
)


def _prefix(symbol: str) -> str:
    from app.data.schema.domains.money_flow import normalize_money_flow_symbol
    from app.data.sources.cn.stock_name_utils import infer_exchange

    code = normalize_money_flow_symbol(symbol)
    prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(infer_exchange(code))
    suffix = str(symbol).strip().upper().rsplit(".", 1)
    if prefix is None or (len(suffix) == 2 and suffix[1].lower() != prefix):
        raise ValueError("money-flow symbol/exchange mismatch")
    return f"{prefix}{code}"


async def fetch_money_flow(symbol: str, days: int = 120) -> pd.DataFrame:
    """获取最近若干个交易日的资金流。"""

    def _fetch() -> list[dict]:
        query = urllib.parse.urlencode(
            {
                "page": 1,
                "num": max(1, min(int(days), 240)),
                "sort": "opendate",
                "asc": 0,
                "daima": _prefix(symbol),
            }
        )
        request = urllib.request.Request(
            f"{_URL}?{query}",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            text = response.read().decode("utf-8", "ignore")
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end < start:
            raise ValueError("返回内容不包含 JSON 数组")
        payload = json.loads(text[start : end + 1])
        if not isinstance(payload, list):
            raise TypeError("资金流返回值不是数组")
        return payload

    try:
        rows = await asyncio.to_thread(_fetch)
    except (asyncio.TimeoutError, ConnectionError, TimeoutError, OSError) as exc:
        raise map_network_exception(exc, "sina", _DOMAIN) from exc
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DataFormatError("sina", _DOMAIN, f"symbol={symbol}: {exc}") from exc
    except Exception as exc:
        raise DataSourceUnavailableError(
            "sina", _DOMAIN, f"symbol={symbol}: {exc}"
        ) from exc

    if not rows:
        raise DataNotFoundError("sina", _DOMAIN, f"symbol={symbol} 无资金流数据")

    frame = pd.DataFrame(rows)
    from app.data.schema.domains.money_flow import normalize_money_flow_symbol

    frame["symbol"] = normalize_money_flow_symbol(symbol)
    frame["flow_api"] = "MoneyFlow.ssl_qsfx_zjlrqs"
    return frame
