"""
情绪分析工具 - 股票市场情绪分析数据
"""

import logging

from app.engine.tools.common.tool_result import success_result, error_result, format_tool_result, ErrorCodes

logger = logging.getLogger(__name__)


def build_sentiment_payload(snapshot: dict, symbol: str) -> dict:
    """Pure rendering projection; keep the shared population immutable."""
    from app.engine.tools.datasources.news_classification import has_body_text
    items = snapshot.get("items") or []
    metadata = {key: value for key, value in snapshot.items() if key != "items"}
    counts = {key: sum(row.get("sentiment") == key for row in items)
              for key in ("positive", "negative", "neutral", "unknown")}
    tagged = sum(counts[key] for key in ("positive", "negative", "neutral"))
    score = (counts["positive"] - counts["negative"]) / tagged if tagged else None
    kinds = {str(row.get("source_kind") or "aggregator") for row in items}
    return {
        **metadata, "symbol": symbol, "sample_count": len(items),
        "sentiment_counts": counts, "labeled_count": tagged,
        "sentiment_score": score,
        "score_method": "(positive-negative)/labeled_count; unknown excluded; descriptive_only",
        "classification_status": (snapshot.get("classification_coverage") or {}).get("classification_status", "unknown"),
        "label_method": "provider_labels_plus_conservative_local_text_rules",
        "body_count": sum(has_body_text(row) for row in items),
        "source_kinds": sorted(kinds), "confidence": None,
        "samples": [{key: row.get(key) for key in (
            "title", "url", "source", "publish_time", "sentiment", "provider_sentiment",
            "sentiment_label_source", "sentiment_method", "sentiment_reason", "sentiment_evidence", "content_status"
        )} for row in items],
        "limitations": "仅为新闻舆情；未接入雪球/股吧等社交讨论。未标注不等于中性，新闻条数不等于高置信度。",
    }


def get_stock_sentiment(symbol: str, current_date: str) -> str:
    """Audit labels on the same frozen news population; unknown is not neutral."""
    import json
    from app.engine.tools.datasources.news import _fetch_news_snapshot
    try:
        snapshot = _fetch_news_snapshot(symbol, 15, current_date)
        payload = build_sentiment_payload(snapshot, symbol)
        return format_tool_result(success_result(json.dumps(payload, ensure_ascii=False, default=str)))
    except Exception as exc:
        logger.error("新闻情绪统计失败: %s", exc, exc_info=True)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, str(exc)))
