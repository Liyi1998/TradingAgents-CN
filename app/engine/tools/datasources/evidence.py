"""Compact evidence companions declared by datasource needs, not role names."""
from __future__ import annotations

import json
import logging

from app.core.async_utils import run_async
from app.data.core.interface import DataInterface
from app.engine.tools.common.data_access import read_with_refresh
from app.engine.tools.common.tool_result import success_result, format_tool_result

logger = logging.getLogger(__name__)


def get_valuation_history(symbol: str, end_date: str, sessions: int = 120) -> str:
    """Return bounded actual-session summaries instead of a huge raw table."""
    try:
        result = run_async(DataInterface.get_instance().valuation_history_context(
            "CN", symbol.split(".")[0], end_date, sessions=sessions, timeout=12))
    except Exception as exc:
        logger.warning("历史估值辅助证据未取得: %s", exc)
        result = {"status": "unavailable", "reason": type(exc).__name__, "fields": {},
                  "limitations": "历史分位不可计算；继续使用已验证的当前价格和其他独立证据。"}
    return format_tool_result(success_result(json.dumps(result, ensure_ascii=False, default=str)))


def get_capital_context(symbol: str, end_date: str) -> str:
    """Share market value and the same news population with capital consumers."""
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.data.query.valuation_evidence import circulating_value_evidence
    from app.engine.tools.datasources.news import _fetch_news_snapshot
    from app.engine.tools.datasources.sentiment import build_sentiment_payload

    context = ACTIVE_ANALYSIS_CONTEXT.get() or {}
    day = context.get("last_actual_trade_date") or end_date
    symbol = symbol.split(".")[0]
    result = {"symbol": symbol, "analysis_date": end_date}
    try:
        read = read_with_refresh("CN", "daily_indicators", symbol=symbol,
            start_date=day, end_date=day, timeout=6,
            filters={"required_fields": ["circ_mv"], "expected_dates": [day],
                     "contract": "verified_dated_indicators", "calendar_verified": context.get("calendar_status") == "success"})
        result["circulating_market_value"] = circulating_value_evidence(read.get("data") or [], day)
    except Exception as exc:
        result["circulating_market_value"] = {"status": "unavailable", "value": None, "reason": type(exc).__name__}
    try:
        snapshot = _fetch_news_snapshot(symbol, 15, end_date)
        news = build_sentiment_payload(snapshot, symbol)
        result["news_text_direction"] = {key: news.get(key) for key in (
            "population", "range_start", "analysis_date", "classification_coverage", "classification_status",
            "sentiment_counts", "labeled_count", "sentiment_score", "score_method", "quality", "limitations")}
        result["news_text_direction"]["sample_count_30_calendar_days"] = news["sample_count"]
        result["news_text_direction"]["latest_publication_date"] = max(
            (str(row.get("publish_time") or "")[:10] for row in snapshot.get("items") or []), default=None)
        result["news_text_direction"]["daily_heat_change"] = None
        for key in ("publication_lag_calendar_days", "current_day_news_coverage", "completeness", "freshness_limit"):
            result["news_text_direction"][key] = snapshot.get(key)
        result["news_text_direction"]["sample_evidence"] = [
            {key: row.get(key) for key in ("title", "publish_time", "url", "sentiment", "sentiment_reason")}
            for row in (snapshot.get("items") or [])[:5]]
        result["news_text_direction"]["usage_limit"] = "仅描述已分类新闻文本子样本；不是当日全量新闻、真实散户情绪或主力建仓评分。缺历史基数不能计算热度环比。"
    except Exception as exc:
        result["news_text_direction"] = {"status": "unavailable", "sentiment_score": None, "reason": type(exc).__name__}
    return format_tool_result(success_result(json.dumps(result, ensure_ascii=False, default=str)))
