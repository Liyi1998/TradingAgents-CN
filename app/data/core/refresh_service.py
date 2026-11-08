"""数据刷新服务 — 按需刷新指定股票数据。"""

import asyncio
import logging
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Optional, Tuple

from app.data.core.domain import DataDomain
from app.data.core.adjustment_refresh import (
    CN_ADJUSTMENT_HISTORY_START, assert_cn_adjustment_horizon, refresh_fetch_range,
    cn_adjustment_snapshot_horizon,
)
from app.data.core.result import RefreshResult, DomainRefreshResult
from app.data.core.registry.capability import CapabilityRegistry
from app.data.core.registry.priority import PriorityConfig
from app.data.schema.base.enums import RefreshStatus
from app.data.storage.redis.locks import DistributedLock
from app.data.storage.cache.memory_cache import TTLCache

if TYPE_CHECKING:
    from app.data.processor.fallback_router import FallbackRouter

logger = logging.getLogger(__name__)

_cooldown_cache = TTLCache(default_ttl=300)  # 5 分钟冷却
_SUPPORTED_ON_DEMAND_DOMAINS = {
    "basic_info",
    "trade_calendar",
    "daily_quotes",
    "index_quotes",
    "daily_indicators",
    "adj_factors",
    "corporate_actions",
    "financial_data",
    "news",
    "market_quotes",
    # 分析前预拉取扩展域
    "intraday_quotes",
    "money_flow",
    "margin_trading",
    "dragon_tiger",
    "block_trade",
}

# 按日期增量刷新的域：{domain: (date_field, default_lookback_days)}
_INCREMENTAL_DOMAINS = {
    "daily_quotes": ("trade_date", 60),
    "index_quotes": ("trade_date", 60),
    "daily_indicators": ("trade_date", 60),
    "adj_factors": ("trade_date", 60),
    "financial_data": ("report_period", 365),
    "intraday_quotes": ("datetime", 5),
    "money_flow": ("trade_date", 60),
    "margin_trading": ("trade_date", 60),
    "dragon_tiger": ("trade_date", 60),
    "block_trade": ("trade_date", 60),
    "corporate_actions": ("ex_date", 365),
    "news": ("publish_time", 7),
}


class DataRefreshService:
    """数据刷新服务 — 编排按需刷新流程。"""

    def __init__(self, capability_registry: CapabilityRegistry, priority_config: PriorityConfig):
        self._registry = capability_registry
        self._priority = priority_config
        self._router: Optional["FallbackRouter"] = None

    def _get_router(self):
        """获取或创建缓存的 FallbackRouter 实例（断路器和限流器状态跨调用保持）。

        使用进程级单例，与 sync_job / multi_source_basics_sync 共享状态。
        """
        if self._router is None:
            from app.data.processor.fallback_router import FallbackRouter
            self._router = FallbackRouter.get_instance()
        return self._router

    async def refresh(
        self,
        market: str,
        symbol: str,
        domains: Optional[List[str]] = None,
        force: bool = False,
        timeout: int = 30,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        coverage_filters: Optional[dict] = None,
    ) -> RefreshResult:
        """刷新指定股票的数据。

        Args:
            market: 市场 (CN/HK/US)
            symbol: 股票代码
            domains: 要刷新的域列表（None = 全部）
            force: 是否强制刷新（忽略冷却期）
            timeout: 超时秒数

        Returns:
            RefreshResult 刷新结果
        """
        result = RefreshResult(symbol=symbol, market=market)
        start_time = time.time()
        for value in (start_date, end_date):
            if value:
                datetime.strptime(value, "%Y-%m-%d")
        if start_date and end_date and start_date > end_date:
            raise ValueError("start_date必须不晚于end_date")

        if domains is None:
            domains = [d.value for d in DataDomain if d.value in _SUPPORTED_ON_DEMAND_DOMAINS and d.value != "index_quotes"]
        else:
            domains = list(domains)

        # 并行刷新各域
        tasks = []
        for domain in domains:
            tasks.append(self._refresh_domain(market, symbol, domain, force, timeout, start_date, end_date, coverage_filters))

        domain_results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, dr in enumerate(domain_results):
            if isinstance(dr, Exception):
                result.domains[domains[i]] = DomainRefreshResult(
                    domain=domains[i], status="failed", error=str(dr)
                )
            else:
                result.domains[domains[i]] = dr

        result.total_latency_ms = int((time.time() - start_time) * 1000)
        result.compute_status()
        return result

    async def _get_incremental_date_range(
        self, market: str, symbol: str, domain: str,
    ) -> Tuple[str, str, Optional[str]]:
        """查询 MongoDB 最新记录，计算增量拉取的日期范围。

        Returns:
            (start_date, end_date, latest_db_date) —
            start_date 为最新记录日期往前 7 天（重叠容错）；
            end_date 为最近交易日（非交易日回退，避免周末空请求）；
            latest_db_date 为库中已有最新日期（"%Y-%m-%d" 或 None），
            调用方可据此判断数据是否已是最新。
            若无历史数据则 start 回退到默认 lookback。
        """
        date_field, default_lookback = _INCREMENTAL_DOMAINS[domain]
        latest_db_date: Optional[str] = None

        try:
            from app.data.storage.mongo.client import get_motor_db
            from app.data.storage.mongo.collections import get_collection_name

            db = get_motor_db()
            coll_name = get_collection_name(domain, market)
            doc = await db[coll_name].find_one(
                {"symbol": symbol},
                {date_field: 1},
                sort=[(date_field, -1)],
            )

            if doc and doc.get(date_field):
                latest = doc[date_field]
                if len(str(latest)) >= 10:
                    latest_date = datetime.strptime(str(latest)[:10], "%Y-%m-%d")
                else:
                    latest_date = datetime.strptime(str(latest), "%Y%m%d")
                latest_db_date = latest_date.strftime("%Y-%m-%d")
                start = (latest_date - timedelta(days=7)).strftime("%Y-%m-%d")
            else:
                start = (datetime.now(timezone.utc) - timedelta(days=default_lookback)).strftime("%Y-%m-%d")
        except Exception as e:
            logger.debug(f"查询最新日期失败，使用默认范围: {e}")
            start = (datetime.now(timezone.utc) - timedelta(days=default_lookback)).strftime("%Y-%m-%d")

        # end_date 回退到最近交易日，避免周末/节假日请求空数据
        from app.data.core.market import get_latest_trade_day

        try:
            ltd = await get_latest_trade_day(market)
            if ltd is not None:
                end = ltd.strftime("%Y-%m-%d")
            else:
                end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        except Exception as e:
            logger.debug(f"查询最近交易日失败，降级为今天: {e}")
            end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if market == "CN" and domain == "adj_factors":
            start = CN_ADJUSTMENT_HISTORY_START
            # The last effective event is not the last checked corporate-action
            # horizon; a sparse event date cannot establish freshness.
            latest_db_date = None
        return start, end, latest_db_date

    async def _assert_cn_adjustment_horizon(self, symbol: str, end_date: str) -> None:
        await assert_cn_adjustment_horizon(symbol, end_date)

    async def _refresh_domain(
        self, market: str, symbol: str, domain: str, force: bool, timeout: int,
        requested_start: Optional[str] = None, requested_end: Optional[str] = None,
        coverage_filters: Optional[dict] = None,
    ) -> DomainRefreshResult:
        """刷新单个域。"""
        start = time.time()
        dr = DomainRefreshResult(domain=domain, status="pending")

        if domain not in _SUPPORTED_ON_DEMAND_DOMAINS:
            dr.status = "failed"
            dr.error = f"按需刷新暂不支持该域: {domain}"
            dr.latency_ms = int((time.time() - start) * 1000)
            return dr

        # 冷却期检查
        cooldown_key = f"cooldown:{market}:{symbol}:{domain}"
        if requested_start or requested_end:
            cooldown_key += f":{requested_start or ''}:{requested_end or ''}"
        if coverage_filters:
            cooldown_key += ":" + hashlib.sha256(json.dumps(coverage_filters, sort_keys=True, default=str).encode()).hexdigest()[:16]
        if not force and _cooldown_cache.get(cooldown_key):
            from copy import deepcopy
            return deepcopy(_cooldown_cache.get(cooldown_key))

        # 获取分布式锁
        lock = DistributedLock(f"lock:{market}:{domain}:{symbol}", ttl=timeout)
        acquired = await lock.acquire_with_wait(max_wait=5)
        if not acquired:
            # 另一刷新正在持有该 (market, domain, symbol) 锁——这是预期并发去重，
            # 不是失败。标记为 skipped 以避免被误判为 PARTIAL/FAILED。
            dr.status = RefreshStatus.SKIPPED.value
            dr.error = "另一刷新正在进行"
            dr.latency_ms = int((time.time() - start) * 1000)
            return dr

        try:
            router = self._get_router()

            # 增量域：只拉取最新记录之后的数据
            fetch_kwargs = {}
            end_date: Optional[str] = None
            if domain in _INCREMENTAL_DOMAINS:
                start_date, end_date, _ = await self._get_incremental_date_range(
                    market, symbol, domain
                )
                fetch_start, fetch_end = refresh_fetch_range(
                    market, domain, start_date, end_date, requested_start, requested_end)
                fetch_kwargs["start_date"] = fetch_start
                fetch_kwargs["end_date"] = fetch_end
                end_date = requested_end or end_date
                if market == "CN" and domain == "adj_factors":
                    await self._assert_cn_adjustment_horizon(symbol, end_date)
            elif domain in {"trade_calendar", "news"}:
                if requested_start:
                    fetch_kwargs["start_date"] = requested_start
                if requested_end:
                    fetch_kwargs["end_date"] = requested_end
            elif domain == "basic_info" and requested_end:
                fetch_kwargs["end_date"] = requested_end

            fetch_result = await asyncio.wait_for(
                router.fetch(market, domain, symbol, **fetch_kwargs,
                             coverage_filters=coverage_filters, timeout=timeout),
                timeout=timeout,
            )

            # 刷新状态如实反映源结果。缓存日期不能证明字段或窗口完整；
            # 源失败仍为 failed，消费方可以继续使用经覆盖校验的缓存。
            if not fetch_result.success:

                dr.status = "failed"
                dr.error = fetch_result.error or "所有数据源失败"
                dr.fallback_from = fetch_result.fallback_from
                dr.latency_ms = int((time.time() - start) * 1000)
                _cooldown_cache.set(cooldown_key, dr, ttl=60)
                return dr

            if not fetch_result.records:
                dr.status = "no_events" if domain in {"dragon_tiger", "block_trade"} else "empty"
                dr.source = fetch_result.source
                dr.latency_ms = int((time.time() - start) * 1000)
                _cooldown_cache.set(cooldown_key, dr, ttl=300)
                return dr

            if market == "CN" and domain == "adj_factors":
                returned_horizon = cn_adjustment_snapshot_horizon(fetch_result.records)
                if returned_horizon != end_date:
                    raise ValueError("CN adjustment source did not prove the requested horizon")
                # Recheck after source I/O; do not publish over a newer snapshot
                # that another writer completed while the request was running.
                await self._assert_cn_adjustment_horizon(symbol, returned_horizon)
            count = await self._write_to_mongo(fetch_result.records, domain, market)

            dr.status = "partial" if fetch_result.status == "partial" else "refreshed"
            dr.source = fetch_result.source
            dr.fallback_from = fetch_result.fallback_from
            dr.error = fetch_result.error
            dr.record_count = count
            dr.latency_ms = int((time.time() - start) * 1000)

            # 写 checkpoint，让 Dashboard 状态可见（手动单股刷新）。
            # 失败不阻断刷新主流程。
            try:
                from app.data.storage.mongo.repositories.metadata_repo import MetadataRepo

                await MetadataRepo().update_checkpoint(
                    market=market,
                    domain=domain,
                    source=fetch_result.source or "",
                    last_sync_date=end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    record_count=count,
                    status="success" if dr.status == "refreshed" and count else "partial",
                    duration_ms=dr.latency_ms,
                    scope="single",
                    trigger="manual",
                    symbol=symbol,
                )
            except Exception as cp_err:
                logger.debug(f"写 checkpoint 失败（不影响刷新结果）: {cp_err}")

            _cooldown_cache.set(cooldown_key, dr, ttl=300)
            return dr

        except asyncio.TimeoutError:
            dr.status = "timeout"
            dr.error = f"超时 ({timeout}s)"
            _cooldown_cache.set(cooldown_key, dr, ttl=60)
            dr.latency_ms = int((time.time() - start) * 1000)
            return dr
        except Exception as e:
            logger.warning(f"刷新 {market}/{symbol}/{domain} 失败: {e}")
            dr.status = "failed"
            dr.error = str(e)
            _cooldown_cache.set(cooldown_key, dr, ttl=60)
            dr.latency_ms = int((time.time() - start) * 1000)
            return dr

        finally:
            await lock.release()

    async def _write_to_mongo(self, records: list, domain: str, market: str) -> int:
        """写入 MongoDB。通过 DataInterface 单例获取 Reader，避免创建多余实例。"""
        try:
            from app.data.core.interface import DataInterface
            di = DataInterface.get_instance()
            repo = di.reader._get_repo(domain)
        except Exception as e:
            # 降级：直接创建（测试或特殊场景）
            logger.debug(f"通过 DataInterface 获取 Reader 失败，降级创建: {e}")
            from app.data.core.reader import Reader
            repo = Reader()._get_repo(domain)
        if repo:
            return await repo.upsert_many(records, market)
        return 0
