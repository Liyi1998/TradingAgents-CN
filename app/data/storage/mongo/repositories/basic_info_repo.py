"""股票基本信息仓储。"""

from typing import Dict, List, Optional
from datetime import date

from pymongo import UpdateOne

from app.data.storage.mongo.client import get_motor_db
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.bulk_utils import batched_bulk_write
from app.data.processor.industry_contract import INDUSTRY_CONTRACT, INDUSTRY_FIELDS, industry_label, industry_verified


def _valid_industry_text(field):
    text = {"$convert": {"input": "$" + field, "to": "string", "onError": "", "onNull": ""}}
    return {"$and": [
        {"$eq": [{"$type": "$" + field}, "string"]},
        {"$not": [{"$regexMatch": {"input": text,
            "regex": r"^\s*(?:[+-]?\d+(?:\.\d+)?|nan|inf|none|null|unknown|n/a|-)?\s*$", "options": "i"}}]},
    ]}


def cn_basic_update(record: Dict) -> List[Dict]:
    """Atomic sparse updates preserve field provenance, and retire invalid type labels."""
    fields = {key: {"$literal": value} for key, value in record.items()
              if key not in INDUSTRY_FIELDS and key != "_id"}
    valid_old = _valid_industry_text("industry")
    if industry_verified(record):
        # A slow older fetch cannot replace a newer dated classification.
        newer_old = {"$and": [valid_old,
            {"$eq": ["$industry_contract", INDUSTRY_CONTRACT]},
            {"$gt": ["$industry_as_of", record["industry_as_of"]]},
            {"$lte": ["$industry_as_of", date.today().isoformat()]},
            {"$ne": [{"$dateFromString": {"dateString": {"$convert": {"input": "$industry_as_of", "to": "string", "onError": "", "onNull": ""}},
                "format": "%Y-%m-%d", "onError": None, "onNull": None}}, None]},
            _valid_industry_text("industry_source"),
            _valid_industry_text("industry_classification"),
        ]}
        for key in INDUSTRY_FIELDS:
            fields[key] = {"$cond": [newer_old, "$" + key, {"$literal": record.get(key)}]}
    else:
        fields["industry"] = {"$cond": [valid_old, "$industry", {"$literal": industry_label(record.get("industry"))}]}
        for key in INDUSTRY_FIELDS[1:]:
            fields[key] = {"$cond": [valid_old, {"$ifNull": ["$" + key, None]}, None]}
    return [{"$set": fields}]


class BasicInfoRepo:
    """stock_basic_info 集合仓储。"""

    async def upsert_many(self, records: List[Dict], market: str) -> int:
        if not records:
            return 0
        db = get_motor_db()
        coll = db[get_collection_name("basic_info", market)]
        ops = []
        for rec in records:
            if "symbol" not in rec:
                continue
            ops.append(UpdateOne(
                {"symbol": rec["symbol"]},
                cn_basic_update(rec) if market == "CN" else {"$set": rec},
                upsert=True,
            ))
        if not ops:
            return 0
        return await batched_bulk_write(coll, ops)

    async def get_by_symbol(self, symbol: str, market: str) -> Optional[Dict]:
        db = get_motor_db()
        coll = db[get_collection_name("basic_info", market)]
        return await coll.find_one({"symbol": symbol}, {"_id": 0})

    async def get_all(self, market: str, limit: int = 0) -> List[Dict]:
        db = get_motor_db()
        coll = db[get_collection_name("basic_info", market)]
        cursor = coll.find({}, {"_id": 0})
        if limit > 0:
            cursor = cursor.limit(limit)
        return await cursor.to_list(length=None)

    async def get_active_symbols(self, market: str) -> List[Dict]:
        """获取活跃（未退市）股票的 symbol 列表。"""
        db = get_motor_db()
        coll = db[get_collection_name("basic_info", market)]
        cursor = coll.find(
            {"list_status": "L"},
            {"symbol": 1, "_id": 0},
        )
        return await cursor.to_list(length=None)

    async def count(self, market: str) -> int:
        db = get_motor_db()
        coll = db[get_collection_name("basic_info", market)]
        return await coll.count_documents({})
