"""MCP 原始结果快照仓储。"""

from typing import Any, Dict

from app.data.storage.mongo.client import get_motor_db


class MCPDataCacheRepo:
    """封装 ``mcp_data_cache`` 集合，避免 services 层直连数据库句柄。"""

    async def upsert_daily_snapshot(
        self,
        *,
        query_hash: str,
        cache_date: str,
        payload: Dict[str, Any],
    ) -> None:
        db = get_motor_db()
        await db["mcp_data_cache"].update_one(
            {"query_hash": query_hash, "cache_date": cache_date},
            {"$set": payload},
            upsert=True,
        )
