"""通用同步任务基类 — 编排: 读检查点 → FallbackRouter.fetch → 写仓储 → 更新检查点 → 写 sync_event。"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.data.core.domain import DataDomain, MARKET_DATA_DOMAINS
from app.data.core.adjustment_refresh import (
    assert_cn_adjustment_horizon, refresh_fetch_range, cn_adjustment_snapshot_horizon,
)
from app.data.scheduler.checkpoint import CheckpointManager
from app.data.storage.mongo.repositories.metadata_repo import MetadataRepo

logger = logging.getLogger(__name__)
_SUPPORTED_SYNC_DOMAINS = {
    "basic_info",
    "trade_calendar",
    "daily_quotes",
    "index_quotes",
    "daily_indicators",
    "adj_factors",
    "corporate_actions",
    "financial_data",
    "market_quotes",
    "news",
    "intraday_quotes",
    "money_flow",
    "margin_trading",
    "dragon_tiger",
    "block_trade",
    "tushare_universe",
    "connect_status",
    "southbound_holding",
    "pre_post_market",
}

# 冷启动时，事件型域不应从 1970 年请求到现在。AKShare 的龙虎榜和大宗
# 交易接口按日期区间返回全市场数据；首次“增量同步”只缓存近期窗口即可。
_INITIAL_LOOKBACK_DAYS = {
    "dragon_tiger": 60,
    "block_trade": 60,
}


def _resolve_sync_concurrency(market: str) -> int:
    """按市场读取并发上限（带兜底）。"""
    mapping = {
        "CN": "CN_SYNC_CONCURRENCY",
        "HK": "HK_SYNC_CONCURRENCY",
        "US": "US_SYNC_CONCURRENCY",
    }
    attr = mapping.get(market.upper(), "DATA_SYNC_CONCURRENCY")
    value = getattr(settings, attr, None)
    if isinstance(value, int) and value >= 1:
        return value
    return settings.DATA_SYNC_CONCURRENCY


class BaseSyncJob(ABC):
    """同步任务基类，三市场各域共用。"""

    def __init__(self, market: str, domain: str):
        self.market = market
        self.domain = domain
        self.sync_mode = "incremental"
        self.preferred_source = None
        self.dependencies = []
        self.force_sync = False
        self._checkpoint = CheckpointManager()
        self._metadata = MetadataRepo()

    async def execute(self) -> dict:
        """执行同步任务。"""
        start = time.time()
        event = {
            "event_type": "SYNC_START",
            "market": self.market,
            "domain": self.domain,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._metadata.insert_event(event)

        try:
            if self.domain not in _SUPPORTED_SYNC_DOMAINS:
                raise NotImplementedError(
                    f"同步任务暂未打通该域: {self.market}/{self.domain}"
                )

            # 非交易日跳过行情类数据（force_sync 时跳过检查）
            if not self.force_sync and DataDomain(self.domain) in MARKET_DATA_DOMAINS:
                if not await self._is_trading_day():
                    logger.info(f"非交易日，跳过 {self.market}/{self.domain}")
                    return {"status": "skipped", "reason": "non_trading_day"}

            # 读取检查点
            if self.sync_mode == "full":
                start_date = "1970-01-01"
            else:
                checkpoint = await self._checkpoint.get_checkpoint(
                    self.market, self.domain, "scheduled"
                )
                if checkpoint:
                    start_date = checkpoint
                elif self.domain in _INITIAL_LOOKBACK_DAYS:
                    from app.data.core.market import get_market_timezone

                    market_tz = get_market_timezone(self.market)
                    start_date = (
                        datetime.now(market_tz)
                        - timedelta(days=_INITIAL_LOOKBACK_DAYS[self.domain])
                    ).strftime("%Y-%m-%d")
                else:
                    start_date = "1970-01-01"

            checkpoint_date = None
            outcomes = []

            # A 股日线的全市场同步走金策智算 DuckDB 分页桥接。DuckDB 是历史
            # 真相来源，MongoDB 仅保留近期缓存；禁止退化成 5000+ 只股票逐股
            # 请求 AKShare，避免冷启动产生数千次公网调用。
            if (
                self.market == "CN"
                and self.domain == DataDomain.DAILY_QUOTES.value
                and self.preferred_source == "duckdb"
            ):
                bulk_result = await self._sync_cn_duckdb_daily_quotes(start_date)
                outcomes.append({"status": bulk_result.get("status", "success")})
                total_count = bulk_result["count"]
                last_source = bulk_result["source"]
                checkpoint_date = bulk_result["checkpoint_date"]
                symbols = []
            else:
                total_count = 0
                last_source = None

            from app.data.processor.fallback_router import FallbackRouter
            router = FallbackRouter.get_instance()
            needs_symbols = await self._resolve_symbol_mode(router)
            # 获取符号列表（basic_info/trade_calendar 不需要逐符号）
            if checkpoint_date is None:
                symbols = (
                    await self._get_symbols()
                    if needs_symbols
                    else ["__all__"]
                )

            if needs_symbols and len(symbols) > settings.DATA_SYNC_MAX_SYMBOLS_PER_RUN:
                await self._metadata.insert_event({"event_type": "SYNC_SKIPPED", "market": self.market,
                    "domain": self.domain, "reason": "non_batch_fanout_exceeds_budget",
                    "symbol_count": len(symbols), "budget": settings.DATA_SYNC_MAX_SYMBOLS_PER_RUN})
                return {"status": "skipped", "reason": "non_batch_fanout_exceeds_budget",
                        "symbol_count": len(symbols), "budget": settings.DATA_SYNC_MAX_SYMBOLS_PER_RUN}
            # 使用进程级单例：与 refresh_service / multi_source_basics_sync
            # 共享 circuit_breaker + rate_limiter 状态
            from app.data.processor.fallback_router import FallbackRouter

            router = FallbackRouter.get_instance()

            # 关键：从串行 for 循环改为 Semaphore + gather 并发
            # - 单 symbol fetch 内部的 RateLimiter / CircuitBreaker 已是 asyncio.Lock 保护的并发安全状态
            # - Semaphore 不释放到 RateLimiter sleep 期间，保证 RateLimiter 窗口不被并发破坏
            # - return_exceptions=True 保证单个 symbol 失败不影响整体
            if checkpoint_date is not None:
                pass
            elif len(symbols) <= 1:
                # 单 symbol 或 __all__：保持原串行路径，避免额外调度开销
                for symbol in symbols:
                    result = await self._fetch_and_write(router, symbol, start_date)
                    outcomes.append(result)
                    total_count += result.get("count", 0)
                    last_source = result.get("data_source")
            else:
                concurrency = _resolve_sync_concurrency(self.market)
                semaphore = asyncio.Semaphore(concurrency)
                logger.info(
                    f"并发同步 {self.market}/{self.domain}: symbols={len(symbols)} concurrency={concurrency}"
                )

                async def _limited_fetch(sym: str) -> dict:
                    async with semaphore:
                        return await self._fetch_and_write(router, sym, start_date)

                results = await asyncio.gather(
                    *[_limited_fetch(sym) for sym in symbols],
                    return_exceptions=True,
                )
                for sym, res in zip(symbols, results):
                    if isinstance(res, Exception):
                        logger.warning(
                            f"同步失败 {self.market}/{self.domain}/{sym}: {res}"
                        )
                        outcomes.append({"status": "failed", "error": type(res).__name__})
                        continue
                    outcomes.append(res)
                    total_count += res.get("count", 0)
                    src = res.get("data_source")
                    if src:
                        last_source = src

            # 更新检查点：用市场本地日期，避免跨时区边界日期错位
            from app.data.core.market import get_market_timezone

            market_tz = get_market_timezone(self.market)
            failed = [row for row in outcomes if row.get("status") not in {"success", "no_events"}]
            coverage_dates = [row["coverage_date"] for row in outcomes if row.get("coverage_date")]
            sync_status = "partial" if failed and total_count else "failed" if failed or (not total_count and self.domain not in {"dragon_tiger", "block_trade"}) else "success"
            covered = checkpoint_date or (min(coverage_dates) if coverage_dates else None)
            if sync_status == "success" and covered:
                await self._checkpoint.update_checkpoint(
                    self.market, self.domain, "scheduled", covered, total_count
                )
            # A failed/empty run never advances last successful coverage.
            elapsed = int((time.time() - start) * 1000)
            await self._metadata.insert_event(
                {
                    "event_type": "SYNC_SUCCESS" if sync_status == "success" else "SYNC_PARTIAL" if sync_status == "partial" else "SYNC_FAILED",
                    "market": self.market,
                    "domain": self.domain,
                    "data_source": last_source,
                    "record_count": total_count,
                    "latency_ms": elapsed,
                }
            )

            return {"status": sync_status, "count": total_count, "coverage_date": covered, "failed_units": len(failed), "latency_ms": elapsed}

        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            logger.error(f"同步失败 {self.market}/{self.domain}: {e}", exc_info=True)
            await self._metadata.insert_event(
                {
                    "event_type": "SYNC_FAILED",
                    "market": self.market,
                    "domain": self.domain,
                    "error": str(e),
                    "latency_ms": elapsed,
                }
            )
            return {"status": "failed", "error": str(e)}

    async def _sync_cn_duckdb_daily_quotes(self, start_date: str) -> dict:
        """分页缓存金策智算 DuckDB 的近期 A 股日线。"""
        from datetime import timedelta

        from app.data.processor.validator import Validator
        from app.data.sources.cn.duckdb.adapter import JinceDuckDBCNAdapter
        from app.data.sources.cn.duckdb.provider import JinceDuckDBCNProvider

        provider = JinceDuckDBCNProvider()
        from app.data.storage.mongo.repositories.daily_quotes_repo import DailyQuotesRepo

        await DailyQuotesRepo().ensure_indexes(self.market)
        coverage = await provider.get_coverage()
        daily_table = next(
            (
                item
                for item in coverage.get("tables", [])
                if item.get("interval") == "D" and item.get("status") == "success"
            ),
            None,
        )
        latest_text = str(
            (daily_table or {}).get("max_date")
            or coverage.get("latest_trade_date")
            or ""
        )[:10]
        earliest_text = str((daily_table or {}).get("min_date") or "")[:10]
        if not latest_text:
            raise RuntimeError("金策智算 DuckDB 未返回日线最新日期")

        latest = datetime.strptime(latest_text, "%Y-%m-%d")
        if self.sync_mode == "full" or not start_date or start_date <= "1970-01-02":
            effective_start = latest - timedelta(
                days=settings.JINCE_INITIAL_LOOKBACK_DAYS
            )
        else:
            try:
                effective_start = datetime.strptime(start_date[:10], "%Y-%m-%d")
            except ValueError:
                effective_start = latest - timedelta(
                    days=settings.JINCE_INITIAL_LOOKBACK_DAYS
                )
            effective_start -= timedelta(
                days=settings.JINCE_INCREMENTAL_OVERLAP_DAYS
            )
        if earliest_text:
            earliest = datetime.strptime(earliest_text, "%Y-%m-%d")
            effective_start = max(effective_start, earliest)
        effective_start_text = effective_start.strftime("%Y-%m-%d")

        logger.info(
            "DuckDB 日线分页同步: %s~%s batch=%d",
            effective_start_text,
            latest_text,
            settings.JINCE_DAILY_QUOTES_BATCH_SIZE,
        )
        adapter = JinceDuckDBCNAdapter(provider)
        validator = Validator()
        total_count = 0
        page_count = 0
        rejected_count = 0
        async for frame in provider.iter_daily_quotes(
            effective_start_text,
            latest_text,
            batch_size=settings.JINCE_DAILY_QUOTES_BATCH_SIZE,
        ):
            schemas = adapter.adapt_daily_quotes(frame)
            records = [item.to_db_doc() for item in schemas]
            valid, errors = validator.validate(records, self.domain, self.market)
            if errors:
                rejected_count += len(errors)
                logger.warning(
                    "DuckDB 日线第 %d 页校验剔除 %d/%d 条",
                    page_count + 1,
                    len(records) - len(valid),
                    len(records),
                )
            if valid:
                total_count += await self._write_records(valid)
            page_count += 1
            logger.info(
                "DuckDB 日线分页写入: page=%d records=%d cumulative=%d",
                page_count,
                len(valid),
                total_count,
            )

        return {
            "count": total_count,
            "source": "duckdb",
            "checkpoint_date": latest_text,
            "status": "partial" if rejected_count else "success",
        }

    async def _fetch_and_write(self, router, symbol: str, start_date: str) -> dict:
        """通过 FallbackRouter 获取数据并写入。"""

        preferred_sources = [self.preferred_source] if self.preferred_source else None
        from app.data.core.market import get_latest_trade_day, get_market_timezone
        expected = await get_latest_trade_day(self.market)
        end_date = expected.isoformat() if expected else datetime.now(get_market_timezone(self.market)).strftime("%Y-%m-%d")
        start_date, end_date = await self._prepare_fetch_range(symbol, start_date, end_date)
        result = await router.fetch(
            self.market,
            self.domain,
            symbol,
            start_date,
            end_date,
            preferred_sources=preferred_sources,
        )

        if result.success and result.records:
            adjustment_coverage = None
            if self.market == "CN" and self.domain == "adj_factors":
                adjustment_coverage = cn_adjustment_snapshot_horizon(result.records)
                if adjustment_coverage != end_date:
                    raise ValueError("CN adjustment source did not prove the requested horizon")
                await assert_cn_adjustment_horizon(symbol, adjustment_coverage)
            count = await self._write_records(result.records)
            from app.data.core.coverage import DATE_FIELDS
            date_field = DATE_FIELDS.get(self.domain, "cal_date")
            dates = [str(row.get(date_field))[:10] for row in result.records if row.get(date_field)]
            if self.market == "CN" and self.domain == "adj_factors":
                covered = adjustment_coverage
            elif self.domain == "trade_calendar":
                # Calendar coverage ahead of today is metadata, not fetched prices.
                covered = min(max(dates), end_date) if dates else None
            else:
                covered = max(dates) if dates else end_date if self.domain == "basic_info" else None
            return {"status": "partial" if result.status == "partial" else "success", "count": count, "data_source": result.source, "coverage_date": covered}
        normal_empty = result.success and self.domain in {"dragon_tiger", "block_trade"}
        return {"status": "no_events" if normal_empty else "failed", "count": 0, "data_source": result.source,
                "error": result.error or "no_records", "coverage_date": end_date if normal_empty else None}

    async def _prepare_fetch_range(self, symbol: str, start_date: str, end_date: str) -> tuple:
        """Share complete CN event ranges/non-regression gates with manual refresh."""
        start_date, end_date = refresh_fetch_range(self.market, self.domain, start_date, end_date)
        if self.market == "CN" and self.domain == "adj_factors":
            await assert_cn_adjustment_horizon(symbol, end_date)
        return start_date, end_date

    async def _write_records(self, records: list) -> int:
        """写入仓储。"""
        from app.data.core.reader import Reader

        reader = Reader()
        repo = reader._get_repo(self.domain)
        if repo:
            return await repo.upsert_many(records, self.market)
        return 0

    async def _is_trading_day(self) -> bool:
        """查询交易日历。"""
        from app.data.core.market import is_trading_day

        return await is_trading_day(self.market)

    async def _resolve_symbol_mode(self, router) -> bool:
        """Select batch only from actual available provider method overrides."""
        batch_methods = {"daily_quotes": "get_daily_quotes_batch",
                         "daily_indicators": "get_daily_indicators_batch",
                         "financial_data": "get_financial_data_batch",
                         "money_flow": "get_money_flow_batch"}
        method = batch_methods.get(self.domain)
        if not method:
            return self._needs_symbol_list()
        from app.data.sources.base.provider import BaseProvider
        priorities = await router._priority.get_priority(self.market, self.domain)
        sources = [self.preferred_source] if self.preferred_source else router._registry.get_ordered_sources(
            self.market, self.domain, user_priority=priorities)
        for source in sources:
            provider, _ = await router._get_provider_adapter(self.market, source)
            if provider and provider.is_available() and getattr(type(provider), method) is not getattr(BaseProvider, method):
                return False
        return True

    def _needs_symbol_list(self) -> bool:
        """是否需要逐符号同步。

        basic_info 和 trade_calendar 是全量同步。
        daily_quotes / daily_indicators / financial_data / money_flow /
        adj_factors / margin_trading 使用批量模式（按日期/报告期参数一次
        获取全市场——逐 symbol 对全市场在限流下不可行，fallback_router 的
        _fetch_*_batch 负责选择批量分支）。
        market_quotes 使用批量快照模式。
        dragon_tiger / block_trade 按日期全量获取。
        news 使用市场级抓取（全市场财经快讯，不依赖逐 symbol）。
        """
        if self.domain in {
            DataDomain.DAILY_QUOTES.value, DataDomain.DAILY_INDICATORS.value,
            DataDomain.FINANCIAL_DATA.value, DataDomain.MONEY_FLOW.value,
        }:
            # 上游批量日线仅实现 CN Tushare；本地 CN DuckDB 另有分页路径。
            # 其他市场/显式免费源保留原逐股路径，不能全部派发 __all__ 后跳过。
            # Legacy synchronous hint only; run() resolves actual provider methods.
            batch_source = self.preferred_source == "tushare" or (self.domain == DataDomain.DAILY_QUOTES.value and self.preferred_source is None)
            duckdb_daily = self.domain == DataDomain.DAILY_QUOTES.value and self.preferred_source == "duckdb"
            return self.market != "CN" or not (batch_source or duckdb_daily)
        return self.domain not in (
            DataDomain.BASIC_INFO.value,
            DataDomain.TRADE_CALENDAR.value,
            DataDomain.MARKET_QUOTES.value,
            DataDomain.DAILY_QUOTES.value,
            DataDomain.DAILY_INDICATORS.value,
            DataDomain.FINANCIAL_DATA.value,
            DataDomain.MONEY_FLOW.value,
            DataDomain.ADJ_FACTORS.value,
            DataDomain.MARGIN_TRADING.value,
            DataDomain.DRAGON_TIGER.value,
            DataDomain.BLOCK_TRADE.value,
            DataDomain.NEWS.value,
        )

    async def _get_symbols(self) -> list:
        """获取当前市场活跃股票列表。"""
        from app.data.storage.mongo.repositories.basic_info_repo import BasicInfoRepo

        repo = BasicInfoRepo()
        stocks = await repo.get_active_symbols(self.market)
        symbols = [s["symbol"] for s in stocks]
        if not symbols:
            # 降级：list_status 字段可能缺失，改为获取所有记录
            from app.data.storage.mongo.client import get_motor_db
            from app.data.storage.mongo.collections import get_collection_name

            db = get_motor_db()
            coll = db[get_collection_name("basic_info", self.market)]
            cursor = coll.find({}, {"symbol": 1, "_id": 0})
            all_stocks = await cursor.to_list(length=None)
            symbols = [s["symbol"] for s in all_stocks if s.get("symbol")]
            logger.warning(f"list_status 字段缺失，降级获取全部股票: {len(symbols)} 只")
        return symbols

    @abstractmethod
    def get_cron(self) -> str:
        """返回 cron 表达式。"""
        ...

    @abstractmethod
    def get_timezone(self) -> str:
        """返回时区。"""
        ...
