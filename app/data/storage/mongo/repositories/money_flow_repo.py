"""资金流向仓储。"""

from app.data.storage.mongo.repositories.base import MongoBaseRepo

from typing import Dict, List, Optional

from pymongo import UpdateOne

from app.data.processor.validator import Validator
from app.data.schema.domains.money_flow import MONEY_FLOW_SNAPSHOT_FIELDS

from app.data.storage.mongo.client import get_motor_db
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.bulk_utils import batched_bulk_write
from app.data.storage.mongo.repositories.key_spec import build_filter


class MoneyFlowRepo(MongoBaseRepo):
    domain = "money_flow"
    range_sort_direction = -1

    """stock_money_flow 集合仓储。"""

    async def upsert_many(self, records: List[Dict], market: str) -> int:
        if not records:
            return 0
        db = get_motor_db()
        coll = db[get_collection_name("money_flow", market)]
        ops = []
        valid, _ = Validator().validate(records, "money_flow", market)
        for rec in valid:
            try:
                filter_doc = build_filter("money_flow", rec)
            except KeyError:
                continue  # 缺少唯一键字段的记录跳过
            # Same natural key is one source snapshot, never a cross-source merge.
            # Cover dict callers as well as schemas whose None fields are explicit.
            snapshot = {field: None for field in MONEY_FLOW_SNAPSHOT_FIELDS}
            snapshot.update(rec)
            ops.append(UpdateOne(
                filter_doc,
                {"$set": snapshot},
                upsert=True,
            ))
        if not ops:
            return 0
        return await batched_bulk_write(coll, ops)

    async def get_by_symbol_and_range(
        self, symbol: str, market: str, start_date: str, end_date: str,
    ) -> List[Dict]:
        db = get_motor_db()
        coll = db[get_collection_name("money_flow", market)]
        query = {"symbol": symbol, "trade_date": {"$gte": start_date, "$lte": end_date}}
        cursor = coll.find(query, {"_id": 0}).sort("trade_date", -1)
        return await cursor.to_list(length=None)

    async def get_latest_date(self, symbol: str, market: str) -> Optional[str]:
        db = get_motor_db()
        coll = db[get_collection_name("money_flow", market)]
        doc = await coll.find_one(
            {"symbol": symbol}, {"trade_date": 1}, sort=[("trade_date", -1)]
        )
        return doc["trade_date"] if doc else None
