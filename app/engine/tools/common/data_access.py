"""工具层数据访问公共函数。

「读取 → 库空自动补数 → 再读」的编排已下沉到数据层
（DataInterface.read(auto_refresh=True)，见 app/data/core/interface.py）；
本模块保留为同步上下文的薄封装，engine 工具统一经此取数，
符号归一化（000001.SZ/sz000001/1 → 000001）同样由数据层完成。
"""

import logging
from typing import Any, Dict, Optional

from app.core.async_utils import run_async

logger = logging.getLogger(__name__)


def render_data_quality(result: dict | None) -> str:
    """Machine-produced missingness remains visible inside the model's evidence."""
    import json
    quality = (result or {}).get("quality") or {"status": "failed", "reason": "no_read_result"}
    keys = ("status", "reason", "data_cutoff", "missing_fields", "missing_dates",
            "missing_fields_by_date", "requirement_contract", "contract_issues", "sources",
            "refresh_status", "refresh_source", "fallback_from", "refresh_error")
    keys += ("optional_field_status", "daily_cross_check", "peer_refresh_status")
    payload = {key: quality[key] for key in keys if key in quality}
    return ("数据获取与缺项说明：" + json.dumps(payload, ensure_ascii=False, default=str)
            + "\n缺项如实披露，继续分析其余证据；依赖缺项的数值、评分或合计不可计算，禁止补零或无限重试。")


def needs_bounded_refresh(result: dict, market: str, end_date: str | None = None) -> bool:
    """Complete historical ranges do not need wall-clock refresh; current stale data do."""
    if result.get("quality", {}).get("status") not in {"success", "no_events", "not_applicable"}:
        return True
    if result.get("quality", {}).get("status") in {"no_events", "not_applicable"}:
        return False
    from datetime import datetime
    from app.data.core.market import get_market_timezone
    today = datetime.now(get_market_timezone(market)).date().isoformat()
    if end_date and str(end_date)[:10] < today:
        return False
    freshness = result.get("freshness")
    return getattr(freshness, "value", freshness) != "fresh"


def read_with_refresh(
    market: str,
    domain: str,
    symbol: Optional[str] = None,
    timeout: int = 30,
    **read_kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """经 DataInterface 读取数据；库空时数据层自动补数后重读一次。

    Args:
        market: 市场类型（CN/HK/US）
        domain: 数据域（daily_quotes / financial_data / ...）
        symbol: 股票代码（容忍多种写法，数据层自动归一）；无 symbol 时不触发补数
        timeout: 数据层按需刷新超时秒数
        **read_kwargs: 透传给 di.read 的参数（start_date/end_date/filters 等）

    Returns:
        DataInterface.read 的完整 envelope（含 data/freshness/auto_refreshed 等），
        无数据或读取失败时返回 None。不抛异常，失败只记 warning。
    """
    from app.data.core.interface import DataInterface

    for key in ("start_date", "end_date"):
        value = str(read_kwargs.get(key) or "")
        if len(value) == 8 and value.isdigit():
            read_kwargs[key] = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    di = DataInterface.get_instance()

    try:
        return run_async(di.read_ready(market, domain, symbol=symbol, timeout=timeout, **read_kwargs))
    except Exception as exc:
        logger.warning("有界数据获取 %s/%s/%s 失败: %s", market, domain, symbol, exc, exc_info=True)
        return {"data": [], "market": market, "domain": domain, "symbol": symbol,
                "quality": {"status": "failed", "reason": type(exc).__name__}}
