"""Bounded, owner-scoped prior methodology context; no report mutations."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.engine.wyckoff_method import method_excerpt


async def previous_wyckoff(db, *, user_id, symbol, analysis_date, horizon, now=None):
    if not user_id or not symbol:
        return None
    day = date.fromisoformat(analysis_date)
    now = now or datetime.now(timezone.utc)
    # Historical analysis cannot read a later-created backdated report. Current
    # day runs can see earlier same-day completed reports.
    cutoff = min(now, datetime.combine(day, time.max, ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc))
    query = {"user_id": user_id, "stock_symbol": symbol, "analysis_horizon": horizon,
        "market_type": {"$in": ["CN", "A股", "A", "中国A股"]}, "status": "completed",
        "analysis_date": {"$lte": day.isoformat()}, "created_at": {"$lt": cutoff}}
    cursor = db.analysis_reports.find(query, {"analysis_id": 1, "analysis_date": 1,
        "created_at": 1, "reports.market_report": 1}).sort([("analysis_date", -1), ("created_at", -1), ("_id", -1)]).limit(10)
    async for row in cursor:
        text = (row.get("reports") or {}).get("market_report")
        excerpt = method_excerpt(text) if isinstance(text, str) else None
        if excerpt:
            rejected = "威科夫方法校验：阶段结构未通过校验" in text
            return {"analysis_id": row["analysis_id"], "report_date": row["analysis_date"],
                "known_at": row["created_at"].isoformat(), "source_quote": excerpt,
                "validation_status": "rejected" if rejected else "historical_only",
                "validation_error": text.split("\n\n", 1)[0] if rejected else None,
                "status": "historical_opinion_not_current_evidence"}
    return None
