"""复权因子仓储。"""

from app.data.storage.mongo.repositories.base import MongoBaseRepo

from typing import Dict, List
from datetime import date

from pymongo import UpdateOne

from app.data.storage.mongo.client import get_motor_db
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.bulk_utils import batched_bulk_write
from app.data.storage.mongo.repositories.key_spec import build_filter


CN_ADJUSTMENT_EVIDENCE_FIELDS = (
    "data_source", "updated_at", "adj_factor", "fore_adj_factor", "back_adj_factor",
    "raw_factor", "adjustment_basis", "adjustment_contract", "adjustment_method",
    "adjustment_series_id", "record_type", "coverage_end", "volume_unit",
    "amount_unit", "unit_conversion", "field_provenance", "data_quality",
)
_SERIES_FIELDS = (
    "data_source", "adjustment_basis", "adjustment_contract", "adjustment_method",
    "adjustment_series_id", "record_type", "coverage_end",
)


def cn_adjustment_replacement(record: Dict) -> Dict:
    """Legacy/unknown writes also revoke absent evidence and auxiliary values."""
    return {**record, **{field: record.get(field) for field in CN_ADJUSTMENT_EVIDENCE_FIELDS}}


def _cn_batches(records: List[Dict]) -> Dict:
    batches = {}
    for record in records:
        try:
            build_filter("adj_factors", record)
        except KeyError:
            continue
        batches.setdefault(record["symbol"], []).append(record)
    for symbol, batch in batches.items():
        signatures = {tuple(record.get(field) for field in _SERIES_FIELDS) for record in batch}
        if len(signatures) != 1:
            raise ValueError(f"Refusing mixed adjustment snapshot evidence for {symbol}")
        dates = [record["trade_date"] for record in batch]
        if len(set(dates)) != len(dates):
            raise ValueError(f"Refusing duplicate adjustment dates in one write for {symbol}")
    return batches


async def _check_uncovered_events(coll, batches: Dict) -> None:
    """Reject residual in-range rows; never infer authority to delete them.

    This is a conservative preflight, NOT an atomic snapshot replacement.
    Multi-process publishers still require serialization/snapshot publication;
    readers must retain their single-series/horizon checks during bulk writes.
    """
    for symbol, batch in batches.items():
        first = batch[0]
        if first.get("record_type") != "event" or not all(first.get(field) for field in _SERIES_FIELDS):
            continue  # No complete claim: writes revoke absent row evidence.
        end = date.fromisoformat(first["coverage_end"]).isoformat()
        dates = [date.fromisoformat(record["trade_date"]).isoformat() for record in batch]
        if max(dates) > end:
            raise ValueError("Adjustment event exceeds its extraction horizon")
        residual = await coll.find_one({
            "symbol": symbol,
            "trade_date": {"$gte": min(dates), "$lte": end, "$nin": dates},
        }, {"_id": 0, "trade_date": 1})
        if residual:
            raise ValueError(
                f"Refusing adjustment snapshot with uncovered existing event for {symbol} "
                f"at {residual['trade_date']}; explicit snapshot reconciliation is required")


class AdjFactorsRepo(MongoBaseRepo):
    domain = "adj_factors"
    range_sort_direction = 1


    async def upsert_many(self, records: List[Dict], market: str) -> int:
        if not records:
            return 0
        db = get_motor_db()
        coll = db[get_collection_name("adj_factors", market)]
        if market == "CN":
            await _check_uncovered_events(coll, _cn_batches(records))
        ops = []
        for rec in records:
            try:
                filter_doc = build_filter("adj_factors", rec)
            except KeyError:
                continue  # 缺少唯一键字段的记录跳过
            ops.append(UpdateOne(
                filter_doc,
                {"$set": cn_adjustment_replacement(rec) if market == "CN" else rec},
                upsert=True,
            ))
        if not ops:
            return 0
        return await batched_bulk_write(coll, ops)

    async def get_by_symbol_and_range(
        self, symbol: str, market: str, start_date: str, end_date: str
    ) -> List[Dict]:
        db = get_motor_db()
        coll = db[get_collection_name("adj_factors", market)]
        cursor = coll.find(
            {"symbol": symbol, "trade_date": {"$gte": start_date, "$lte": end_date}},
            {"_id": 0},
        ).sort("trade_date", 1)
        records = await cursor.to_list(length=None)
        if market != "CN" or start_date > end_date or (records and records[0]["trade_date"] == start_date):
            return records
        # Events are effective from their date, not exact daily observations.
        # Keep the nearest predecessor even when its evidence is unknown or
        # mismatched: choosing an older matching series would hide a conflict.
        # The pure adjustment consumer rejects incomplete/mixed evidence.
        predecessor = await coll.find_one(
            {"symbol": symbol, "trade_date": {"$lt": start_date}},
            {"_id": 0}, sort=[("trade_date", -1)],
        )
        return ([predecessor] if predecessor else []) + records
