"""市场快照仓储。"""

from typing import Dict, List, Optional

from pymongo import ReplaceOne

from app.data.storage.mongo.client import get_motor_db
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.bulk_utils import batched_bulk_write


class MarketQuotesRepo:

    async def upsert_many(self, records: List[Dict], market: str) -> int:
        if not records:
            return 0
        db = get_motor_db()
        coll = db[get_collection_name("market_quotes", market)]
        ops = []
        for rec in records:
            sym = rec.get("symbol")
            if not sym:
                continue
            filter_doc = {"symbol": sym}
            # A snapshot is indivisible: never combine an older price with a
            # newer $max timestamp, or retain another source's proof fields.
            ops.append(ReplaceOne(filter_doc, rec, upsert=True))
        if not ops:
            return 0
        return await batched_bulk_write(coll, ops)

    async def get_by_symbol(self, symbol: str, market: str) -> Optional[Dict]:
        db = get_motor_db()
        coll = db[get_collection_name("market_quotes", market)]
        return await coll.find_one({"symbol": symbol}, {"_id": 0})

    async def get_all(self, market: str, limit: int = 100) -> List[Dict]:
        db = get_motor_db()
        coll = db[get_collection_name("market_quotes", market)]
        cursor = coll.find({}, {"_id": 0}).limit(limit)
        return await cursor.to_list(length=None)
