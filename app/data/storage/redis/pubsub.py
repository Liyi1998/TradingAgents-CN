"""异步刷新队列 — Redis list 实现。"""

import json
import logging
import hashlib
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 内存降级队列
_memory_queues: Dict[str, list] = {}
_memory_dedupe: Dict[str, float] = {}
# SET + bounded queue write must be atomic; keep the claim through processing.
_ENQUEUE = """
if redis.call('LLEN', KEYS[1]) >= 1000 then return 0 end
if not redis.call('SET', KEYS[2], '1', 'NX', 'EX', 300) then return 0 end
redis.call('RPUSH', KEYS[1], ARGV[1])
return 1
"""


class RefreshQueue:
    """异步刷新消息队列。"""

    async def publish_refresh(self, market: str, symbol: str, domain: str,
                              start_date: str = None, end_date: str = None) -> None:
        """Bounded, range-scoped enqueue; duplicate claims expire after processing cooldown."""
        message = json.dumps({"market": market, "symbol": symbol, "domain": domain,
                              "start_date": start_date, "end_date": end_date}, sort_keys=True)
        queue_key = f"queue:refresh:{market}"
        dedupe = "queue:refresh:claim:" + hashlib.sha256(message.encode()).hexdigest()
        try:
            from app.data.storage.redis.client import get_redis
            redis = get_redis()
            if redis:
                await redis.eval(_ENQUEUE, 2, queue_key, dedupe, message)
                return
        except Exception:
            logger.debug("Redis enqueue unavailable; bounded memory fallback", exc_info=True)
        now = time.monotonic()
        for key in [key for key, deadline in _memory_dedupe.items() if deadline <= now]:
            _memory_dedupe.pop(key, None)
        queue = _memory_queues.setdefault(queue_key, [])
        if _memory_dedupe.get(dedupe, 0) > now or len(queue) >= 1000:
            return
        _memory_dedupe[dedupe] = now + 300
        queue.append(message)

    async def pop_refresh(self, market: str) -> Optional[Dict]:
        """从队列弹出一个刷新任务。"""
        queue_key = f"queue:refresh:{market}"

        try:
            redis = None
            try:
                # 函数内延迟导入：仅在需要时解析 Redis 客户端，避免模块加载期耦合。
                from app.data.storage.redis.client import get_redis
                redis = get_redis()
            except Exception as e:
                logger.debug(f"获取 Redis 连接失败: {e}")

            if redis:
                msg = await redis.lpop(queue_key)
                if msg:
                    return json.loads(msg)
                return None
        except Exception as e:
            logger.debug(f"Redis 队列读取失败: {e}")

        # 内存降级
        if queue_key in _memory_queues and _memory_queues[queue_key]:
            msg = _memory_queues[queue_key].pop(0)
            return json.loads(msg)
        return None
