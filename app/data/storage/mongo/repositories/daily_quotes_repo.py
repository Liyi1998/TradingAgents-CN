"""日线行情仓储。"""

from app.data.storage.mongo.repositories.base import MongoBaseRepo

from typing import Dict, List, Optional
import math
import sys

from pymongo import UpdateOne

from app.data.storage.mongo.client import get_motor_db
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.bulk_utils import batched_bulk_write
from app.data.storage.mongo.repositories.key_spec import build_filter


CN_QUOTE_EVIDENCE_FIELDS = (
    "data_source", "updated_at", "price_basis", "adjustment_as_of",
    "adjustment_contract", "volume_unit", "amount_unit", "unit_conversion",
    "field_provenance", "data_quality",
    "reported_pct_chg", "price_change_status",
)

_RAW_SOURCE_CONVERSIONS = {
    "akshare": (
        "akshare:stock_zh_a_hist/eastmoney;volume:lot100->share*100;amount:CNY->CNY",
        "akshare:stock_zh_a_daily/sina;volume:share->share;amount:CNY->CNY",
    ),
    "baostock": ("baostock:identity",),
}

_RAW_OBSERVATION_FIELDS = (
    "open", "high", "low", "close", "pre_close", "change", "pct_chg",
    "volume", "amount", "turnover_rate",
)


def trusted_raw_quote(record: Dict) -> bool:
    """Only existing producer contracts can protect a canonical raw observation."""
    close = record.get("close")
    source = record.get("data_source")
    return (record.get("price_basis") == "raw"
            and record.get("volume_unit") == "share"
            and record.get("amount_unit") == "CNY"
            and isinstance(source, str)
            and record.get("unit_conversion") in _RAW_SOURCE_CONVERSIONS.get(source, ())
            and isinstance(close, (int, float)) and not isinstance(close, bool)
            and math.isfinite(close) and close > 0)


def cn_quote_update(record: Dict):
    """Reject an unproven same-key downgrade atomically, never borrowing proof.

    The DuckDB batch re-fetches overlap dates. Keeping *only* old metadata while
    replacing its OHLCV would certify another observation. Preserve the entire
    existing raw document instead, including timestamps. Unproven new keys and
    already-unproven rows still retain the prior evidence-revocation behavior.
    A newly verified raw provider can replace the whole observation normally.
    """
    replacement = cn_quote_replacement(record)
    if trusted_raw_quote(replacement):
        return {"$set": replacement}
    sources = [{"$and": [
        {"$eq": ["$data_source", source]},
        {"$in": ["$unit_conversion", list(conversions)]},
    ]} for source, conversions in _RAW_SOURCE_CONVERSIONS.items()]
    known_raw = {"$and": [
        {"$eq": ["$price_basis", "raw"]},
        {"$eq": ["$volume_unit", "share"]},
        {"$eq": ["$amount_unit", "CNY"]},
        {"$isNumber": "$close"},
        {"$gt": ["$close", 0]},
        {"$lte": ["$close", sys.float_info.max]},
        {"$or": sources},
    ]}
    return [{"$replaceWith": {"$cond": [known_raw, "$$ROOT", {
        "$mergeObjects": ["$$ROOT", {"$literal": replacement}],
    }]}}]


def cn_quote_replacement(record: Dict) -> Dict:
    """Replace evidence with the new observation, never inherit a prior basis.

    Legacy sparse writes remain partial but cannot inherit provenance. A
    source-proven raw observation clears absent numeric fields: otherwise a
    sparse suspension bar could certify another source's old volume/amount.
    """
    replacement = dict(record)
    if trusted_raw_quote(record):
        replacement.update({field: record.get(field) for field in _RAW_OBSERVATION_FIELDS})
    replacement.update({field: record.get(field) for field in CN_QUOTE_EVIDENCE_FIELDS})
    if record.get("price_basis") not in {"qfq", "hfq"}:
        replacement["adjustment_as_of"] = None
        replacement["adjustment_contract"] = None
    return replacement


class DailyQuotesRepo(MongoBaseRepo):
    domain = "daily_quotes"
    range_sort_direction = 1

    """stock_daily_quotes 集合仓储。"""

    async def ensure_indexes(self, market: str) -> None:
        """保证批量幂等写入和最新日期查询都有对应索引。"""
        db = get_motor_db()
        coll = db[get_collection_name("daily_quotes", market)]
        await coll.create_index(
            [
                ("symbol", 1),
                ("trade_date", 1),
                ("period", 1),
                ("data_source", 1),
            ],
            unique=True,
            name="daily_quote_identity_unique",
        )
        await coll.create_index(
            [("trade_date", -1)], name="daily_quote_latest_date"
        )

    async def upsert_many(self, records: List[Dict], market: str) -> int:
        if not records:
            return 0
        db = get_motor_db()
        coll = db[get_collection_name("daily_quotes", market)]
        ops = []
        for rec in records:
            try:
                filter_doc = build_filter("daily_quotes", rec)
            except KeyError:
                continue  # 缺少唯一键字段的记录跳过
            ops.append(UpdateOne(
                filter_doc,
                cn_quote_update(rec) if market == "CN" else {"$set": rec},
                upsert=True,
            ))
        if not ops:
            return 0
        # 冷启动会按页写入数万条 DuckDB 日线。500 条一批会产生大量 MongoDB
        # 往返；日线文档较小，5000 条仍远低于 MongoDB 批量限制。
        return await batched_bulk_write(coll, ops, batch_size=5000)

    async def get_by_symbol_and_range(
        self, symbol: str, market: str, start_date: str, end_date: str,
        period: Optional[str] = None,
    ) -> List[Dict]:
        db = get_motor_db()
        coll = db[get_collection_name("daily_quotes", market)]
        query = {"symbol": symbol, "trade_date": {"$gte": start_date, "$lte": end_date}}
        if period:
            query["period"] = period
        # 升序（时间自然序）：K线取尾部最新、周期聚合等消费方依赖此序，
        # 展示层的"最新优先"由查看器路由统一处理。
        cursor = coll.find(query, {"_id": 0}).sort("trade_date", 1)
        return await cursor.to_list(length=None)

    async def get_latest_date(self, symbol: str, market: str) -> Optional[str]:
        db = get_motor_db()
        coll = db[get_collection_name("daily_quotes", market)]
        query = {} if symbol == "__all__" else {"symbol": symbol}
        doc = await coll.find_one(
            query, {"trade_date": 1}, sort=[("trade_date", -1)]
        )
        return doc["trade_date"] if doc else None
