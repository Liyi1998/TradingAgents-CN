"""Narrow CN event-snapshot publication rules shared by refresh and scheduler."""

from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo


CN_ADJUSTMENT_HISTORY_START = "1990-01-01"


def cn_adjustment_snapshot_horizon(records) -> str:
    """A checkpoint comes from one returned event snapshot, never its last event."""
    from app.data.processor.adjustment_contract import ADJUSTMENT_BASIS, ADJUSTMENT_CONTRACT

    if not records:
        raise ValueError("Empty CN adjustment snapshot has no proven horizon")
    keys = ("symbol", "data_source", "adjustment_contract", "adjustment_basis",
            "adjustment_method", "adjustment_series_id", "record_type", "coverage_end")
    signatures = {tuple(record.get(key) for key in keys) for record in records}
    first = records[0]
    methods = {"akshare": "akshare_qfq_reciprocal", "baostock": "baostock_fore_multiplier"}
    if (len(signatures) != 1 or not all(first.get(key) for key in keys)
            or first["adjustment_contract"] != ADJUSTMENT_CONTRACT
            or first["adjustment_basis"] != ADJUSTMENT_BASIS
            or first["record_type"] != "event"
            or first["adjustment_method"] != methods.get(first["data_source"])):
        raise ValueError("CN adjustment publication requires one complete normalized snapshot")
    horizon = datetime.strptime(first["coverage_end"], "%Y-%m-%d").date().isoformat()
    if horizon != first["coverage_end"]:
        raise ValueError("Invalid CN adjustment snapshot horizon")
    for record in records:
        effective = datetime.strptime(record["trade_date"], "%Y-%m-%d").date().isoformat()
        if effective > horizon:
            raise ValueError("Adjustment event exceeds its snapshot horizon")
    return horizon


def refresh_fetch_range(market: str, domain: str, start: str, end: str,
                        requested_start: Optional[str] = None,
                        requested_end: Optional[str] = None) -> Tuple[str, str]:
    """A CN event snapshot is never sliced to a latest-event/checkpoint tail.

    BaoStock fetches from 1990 and AKShare fetches its full event source. The
    normalizer retains a predecessor if the baseline predates 1990. The user's
    end remains binding; a read-window start must not truncate the stored series.
    """
    if market == "CN" and domain == "adj_factors":
        return CN_ADJUSTMENT_HISTORY_START, requested_end or end
    return requested_start or start, requested_end or end


async def assert_cn_adjustment_horizon(symbol: str, end_date: str) -> None:
    """Reject future claims and historical downgrades before source I/O/write.

    This is a conservative preflight, not an atomic multi-writer publication.
    Existing repository batch guards and single-series reader gates still apply.
    """
    from app.data.processor.adjustment_contract import ADJUSTMENT_BASIS, ADJUSTMENT_CONTRACT
    from app.data.storage.mongo.client import get_motor_db
    from app.data.storage.mongo.collections import get_collection_name

    requested = datetime.strptime(end_date, "%Y-%m-%d").date()
    if requested > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ValueError("A current source response cannot prove a future adjustment horizon")
    coll = get_motor_db()[get_collection_name("adj_factors", "CN")]
    cursor = coll.find({
        "symbol": symbol, "record_type": "event",
        "adjustment_contract": ADJUSTMENT_CONTRACT,
        "adjustment_basis": ADJUSTMENT_BASIS,
        "adjustment_series_id": {"$type": "string", "$ne": ""},
        "$or": [
            {"data_source": "akshare", "adjustment_method": "akshare_qfq_reciprocal"},
            {"data_source": "baostock", "adjustment_method": "baostock_fore_multiplier"},
        ],
    }, {"_id": 0, "coverage_end": 1})
    async for record in cursor:
        try:
            covered = datetime.strptime(record.get("coverage_end", ""), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if covered > requested:
            raise ValueError(
                "Historical CN adjustment refresh would downgrade a newer snapshot; "
                "read the existing event series at the requested as-of date instead")
