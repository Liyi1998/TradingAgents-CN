"""龙虎榜仓储。"""

from typing import Dict, List

from app.data.storage.mongo.repositories.base import MongoBaseRepo


class DragonTigerRepo(MongoBaseRepo):
    """stock_dragon_tiger 集合仓储。"""

    domain = "dragon_tiger"

    async def get_by_symbol(
        self, symbol: str, market: str, limit: int = 50,
        start_date: str = None, end_date: str = None,
    ) -> List[Dict]:
        coll = self._coll(market)
        query = {"symbol": symbol}
        if start_date or end_date:
            query["trade_date"] = {}
            if start_date:
                query["trade_date"]["$gte"] = start_date
            if end_date:
                query["trade_date"]["$lte"] = end_date
        cursor = coll.find(
            query, {"_id": 0}
        ).sort("trade_date", -1).limit(limit)
        return await cursor.to_list(length=None)

    async def get_by_date(
        self, trade_date: str, market: str, limit: int = 100
    ) -> List[Dict]:
        coll = self._coll(market)
        cursor = coll.find(
            {"trade_date": trade_date}, {"_id": 0}
        ).sort("trade_date", -1).limit(limit)
        return await cursor.to_list(length=None)
