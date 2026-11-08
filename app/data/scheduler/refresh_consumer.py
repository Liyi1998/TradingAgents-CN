"""按需刷新队列消费器 — 打通 reader → Redis 队列 → DataRefreshService 闭环。

背景：Reader.get_data 发现 STALE 时把 (market, symbol, domain) 推入
Redis list（queue:refresh:<market>），但此前无任何消费者——用户点开
K 线图触发的刷新消息进队列后永远没人执行。

本模块作为后台协程常驻消费：逐条取出消息，调用 DataRefreshService
按需增量刷新该 symbol 的该域（30 秒超时、分布式锁去重、5 分钟冷却，
全部复用既有语义），实现「点开图表 → 下次访问即为新数据」的体验。
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_consumer_started = False
_consumer_start_requested = False


async def refresh_queue_consumer_loop():
    """消费按需刷新队列（单协程串行，避免突发并发打挂数据源配额）。

    每轮最多处理 MAX_MESSAGES_PER_TICK 条（限速保护），其余留待下轮。
    Redis 不可用时 RefreshQueue.pop_refresh 自动降级内存队列，无异常外泄。
    """
    global _consumer_started, _consumer_start_requested
    if _consumer_started:
        return
    _consumer_started = True

    from app.data.storage.redis.pubsub import RefreshQueue

    queue = RefreshQueue()
    logger.info("按需刷新队列消费器已启动")

    try:
        while True:
            processed = 0
            try:
                from app.core.config import settings
                markets = [value.strip().upper() for value in settings.DATA_FRESHNESS_PATROL_MARKETS.split(",")]
                for market in markets:
                    for _ in range(10):  # 每市场每轮至多 10 条，限速保护
                        msg = await queue.pop_refresh(market)
                        if not msg:
                            break
                        await _handle_refresh_message(market, msg)
                        processed += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("刷新队列消费异常（本轮跳过）: %s", e)
            # 无消息时休眠，避免空转烧 CPU
            await asyncio.sleep(5 if processed == 0 else 1)
    finally:
        _consumer_started = False
        _consumer_start_requested = False


async def _handle_refresh_message(market: str, msg: dict) -> None:
    """执行单条刷新消息（失败只记日志，不阻断消费循环）。"""
    symbol = msg.get("symbol")
    domain = msg.get("domain")
    if not symbol or not domain:
        return
    try:
        from app.data.core.interface import DataInterface

        di = DataInterface.get_instance()
        result = await di.refresh(market, symbol, domains=[domain], timeout=30,
            start_date=msg.get("start_date"), end_date=msg.get("end_date"))
        logger.debug("按需刷新结果 %s/%s/%s: %s", market, symbol, domain, result.status)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("按需刷新失败 %s/%s/%s: %s", market, symbol, domain, e)


def start_refresh_consumer():
    """注册消费协程到 task_registry（幂等：已启动则跳过）。"""
    global _consumer_start_requested
    if _consumer_started or _consumer_start_requested:
        return
    _consumer_start_requested = True
    from app.core.task_registry import task_registry

    coro = refresh_queue_consumer_loop()
    try:
        task_registry.register(coro, name="data_refresh_queue_consumer", critical=False)
    except Exception:
        coro.close()
        _consumer_start_requested = False
        raise
