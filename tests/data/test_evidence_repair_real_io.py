"""Task-owned real Mongo/Redis contracts; opt-in, never simulated clients."""
import asyncio
import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("EVIDENCE_REPAIR_ACCEPTANCE") != "1",
    reason="Requires the dedicated evidence-repair Mongo/Redis containers")


@pytest.mark.asyncio
async def test_peer_refresh_observation_does_not_delete_or_steal_owner_lock(real_mongo_db):
    from app.core.config import settings
    import app.core.database as database
    from app.data.storage.redis.client import get_redis, reset_client
    from app.data.storage.redis.locks import DistributedLock
    from isolated_database import require_isolated_database
    require_isolated_database()
    assert settings.REDIS_PORT == 28024
    reset_client()
    await database.db_manager.init_redis()
    database.redis_client = database.db_manager.redis_client
    redis = get_redis()
    assert redis is not None and await redis.ping()
    key = "evidence_contract_peer_" + uuid4().hex
    holder, observer = DistributedLock(key, ttl=10), DistributedLock(key, ttl=10)
    try:
        assert await holder.acquire()
        assert not await observer.wait_until_released(.02)
        stored = await redis.get(key)
        if isinstance(stored, bytes): stored = stored.decode()
        assert stored == holder.owner_id
        async def finish():
            await asyncio.sleep(.15)
            await holder.release()
        task = asyncio.create_task(finish())
        assert await observer.wait_until_released(2)
        await task
        assert not await redis.exists(key)
    finally:
        stored = await redis.get(key)
        if isinstance(stored, bytes): stored = stored.decode()
        if stored == holder.owner_id: await holder.release()
        reset_client()


@pytest.mark.asyncio
async def test_facade_joins_actual_peer_refresh_instead_of_returning_immediate_empty(real_mongo_db):
    from app.core.config import settings
    import app.core.database as database
    from app.data.core.interface import DataInterface
    from app.data.storage.mongo.collections import get_collection_name
    from app.data.storage.redis.client import reset_client
    from app.data.storage.redis.locks import DistributedLock
    from app.utils.time_utils import now_utc
    from isolated_database import require_isolated_database
    require_isolated_database()
    reset_client()
    await database.db_manager.init_redis()
    database.redis_client = database.db_manager.redis_client
    DataInterface.reset_instance()
    symbol = "peer_" + uuid4().hex
    coll = real_mongo_db[get_collection_name("news", "CN")]
    holder = DistributedLock(f"lock:CN:news:{symbol}", ttl=15)
    assert await holder.acquire()
    async def finish_real_peer_write():
        await asyncio.sleep(7)
        stamp = now_utc().isoformat()
        await coll.insert_one({"symbol": symbol, "market": "CN", "title": "并发读取契约样本",
            "content": "真实隔离数据库写入，不请求任何新闻数据源。", "content_hash": uuid4().hex,
            "data_source": "contract_fixture", "publish_time": stamp, "updated_at": stamp})
        await holder.release()
    peer = asyncio.create_task(finish_real_peer_write())
    try:
        result = await DataInterface.get_instance().read_ready("CN", "news", symbol, timeout=12)
        await peer
        assert len(result["data"]) == 1
        assert result["quality"]["peer_refresh_status"] == "completed"
        assert result["quality"]["refresh_status"] == "coalesced"
    finally:
        if not peer.done():
            peer.cancel()
            try: await peer
            except asyncio.CancelledError: pass
            await holder.release()
        await coll.delete_many({"symbol": symbol})
        DataInterface.reset_instance()
        reset_client()
