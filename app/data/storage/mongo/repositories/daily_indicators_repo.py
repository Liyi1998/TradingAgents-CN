"""每日指标仓储。"""

from app.data.storage.mongo.repositories.base import MongoBaseRepo

from typing import Dict, List
import sys

from pymongo import UpdateOne

from app.data.storage.mongo.client import get_motor_db
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.bulk_utils import batched_bulk_write
from app.data.storage.mongo.repositories.key_spec import build_filter
from app.data.schema.domains.daily_indicators import (
    INDICATOR_COVERAGE_FIELDS, INDICATOR_VALUE_FIELDS, finite_indicator,
)


def _trusted_expression(field: str, trade_date: str):
    prefix = f"$field_provenance.{field}"
    checks = [{"$eq": [prefix + ".date_verified", True]},
              {"$eq": [prefix + ".as_of", trade_date]}]
    basis = {"pe_ttm": "ttm", "dividend_yield": "annual", "dividend_yield_ttm": "ttm"}.get(field)
    if basis:
        checks.append({"$eq": [prefix + ".basis", basis]})
    return {"$and": checks}


def _field_status(field: str, trade_date: str, missing_status: str):
    value = "$" + field
    finite = {"$and": [{"$isNumber": value}, {"$gte": [value, -sys.float_info.max]},
                       {"$lte": [value, sys.float_info.max]}]}
    prefix = f"$field_provenance.{field}"
    return {"$cond": [finite, {"$cond": [
        {"$and": [{"$eq": [prefix + ".date_verified", True]},
                  {"$eq": [prefix + ".as_of", trade_date]}]},
        {"$cond": [_trusted_expression(field, trade_date), "available", "unverified_basis"]},
        "unverified_date",
    ]}, missing_status]}


def indicator_merge_pipeline(record: Dict) -> List[Dict]:
    """Atomic field-level merge. A sparse/unverified quote cannot downgrade evidence.

    data_source is the latest contributing document source; field_provenance is
    authoritative for each value after a multi-source merge. Legacy values are
    retained but never silently certified by a newer sparse document.
    """
    trade_date = record["trade_date"]
    provenance = record.get("field_provenance") or {}
    update = {key: {"$literal": value} for key, value in record.items()
              if key not in set(INDICATOR_VALUE_FIELDS) | {"_id", "field_provenance", "data_quality"}}
    dividend_diagnostic = (record.get("data_quality") or {}).get("dividend_ttm")
    if isinstance(dividend_diagnostic, dict):
        update["data_quality.dividend_ttm"] = {"$literal": dividend_diagnostic}
    for field in INDICATOR_VALUE_FIELDS:
        value = record.get(field)
        if not finite_indicator(value):
            continue
        evidence = dict(provenance.get(field) or {})
        evidence.setdefault("data_source", record.get("data_source"))
        evidence.setdefault("as_of", trade_date)
        evidence.setdefault("method", "legacy_unverified")
        evidence.setdefault("basis", "unknown")
        evidence["date_verified"] = evidence.get("date_verified") is True and evidence["as_of"] == trade_date
        basis = {"pe_ttm": "ttm", "dividend_yield": "annual", "dividend_yield_ttm": "ttm"}.get(field)
        verified = evidence["date_verified"] and (not basis or evidence["basis"] == basis)
        use_incoming = True if verified else {"$not": [_trusted_expression(field, trade_date)]}
        update[field] = {"$cond": [use_incoming, {"$literal": value}, "$" + field]}
        update["field_provenance." + field] = {
            "$cond": [use_incoming, {"$literal": evidence}, "$field_provenance." + field]}

    incoming_status = (record.get("data_quality") or {}).get("field_status") or {}
    statuses = {field: _field_status(field, trade_date,
                  "unsupported" if incoming_status.get(field) == "unsupported" else "missing")
                for field in INDICATOR_COVERAGE_FIELDS}

    def keys_for(statuses):
        return {"$map": {"input": {"$filter": {
            "input": {"$objectToArray": "$data_quality.field_status"}, "as": "entry",
            "cond": {"$in": ["$$entry.v", statuses]}}},
            "as": "entry", "in": "$$entry.k"}}

    return [
        {"$set": update},
        {"$set": {"data_quality.field_status": statuses, "data_quality.merge_mode": "field_level"}},
        {"$set": {
            "data_quality.available_fields": keys_for(["available"]),
            "data_quality.missing_fields": keys_for(["missing", "unsupported"]),
            "data_quality.unverified_fields": keys_for(["unverified_date", "unverified_basis"]),
        }},
        {"$set": {"data_quality.status": {"$cond": [
            {"$eq": [{"$size": "$data_quality.available_fields"}, len(INDICATOR_COVERAGE_FIELDS)]},
            "complete", {"$cond": [{"$gt": [{"$size": "$data_quality.available_fields"}, 0]},
                                    "partial", "insufficient"]}]}}},
    ]


class DailyIndicatorsRepo(MongoBaseRepo):
    domain = "daily_indicators"
    range_sort_direction = -1


    async def upsert_many(self, records: List[Dict], market: str) -> int:
        if not records:
            return 0
        db = get_motor_db()
        coll = db[get_collection_name("daily_indicators", market)]
        ops = []
        for rec in records:
            try:
                filter_doc = build_filter("daily_indicators", rec)
            except KeyError:
                continue  # 缺少唯一键字段的记录跳过
            ops.append(UpdateOne(
                filter_doc,
                indicator_merge_pipeline(rec) if market == "CN" else {"$set": rec},
                upsert=True,
            ))
        if not ops:
            return 0
        return await batched_bulk_write(coll, ops)

    async def get_by_symbol_and_range(
        self, symbol: str, market: str, start_date: str, end_date: str
    ) -> List[Dict]:
        db = get_motor_db()
        coll = db[get_collection_name("daily_indicators", market)]
        cursor = coll.find(
            {"symbol": symbol, "trade_date": {"$gte": start_date, "$lte": end_date}},
            {"_id": 0},
        ).sort("trade_date", -1)
        return await cursor.to_list(length=None)
