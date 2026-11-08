"""DataInterface — 数据平台统一门面。消费层的唯一入口。"""

import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional

from app.data.core.errors import DataUnavailableError
from app.data.core.result import RefreshResult
from app.data.core.reader import Reader
from app.data.core.refresh_service import DataRefreshService
from app.data.core.registry.capability import CapabilityRegistry
from app.data.core.registry.priority import PriorityConfig
from app.data.core.screening import ScreeningGateway
from app.data.core.symbols import normalize_symbol, normalize_data_symbol
from app.data.storage.cache.memory_cache import TTLCache

logger = logging.getLogger(__name__)

_instance: Optional["DataInterface"] = None
_instance_lock = threading.Lock()

_VALID_MARKETS = ("CN", "HK", "US")


def _normalize_market(market: str) -> str:
    """市场标识入口归一（大小写不敏感），非法值 fail-fast。"""
    m = (market or "").strip().upper()
    if m not in _VALID_MARKETS:
        raise ValueError(f"未知市场类型: {market!r}（支持 CN/HK/US）")
    return m


class DataInterface:
    """数据平台统一接口（单例门面）。

    组合 Reader + RefreshService + CapabilityRegistry + PriorityConfig。
    消费层只通过此类访问数据。
    """

    async def mcp_news_is_fresh(self, symbol: str, max_age_hours: int = 24) -> bool:
        """Check freshness of normalized MCP news through the data boundary."""
        from app.data.sync.mcp_ingestion import is_news_fresh
        return await is_news_fresh(symbol, max_age_hours)

    async def persist_mcp_result(self, tool_name, tool_args, raw, context=None):
        """Normalize external MCP results before writing standard repositories."""
        from app.data.sync.mcp_ingestion import persist_mcp_result
        return await persist_mcp_result(tool_name, tool_args, raw, context)

    def __init__(self, sync_trigger_callback=None):
        self.reader = Reader()
        self._registry = CapabilityRegistry()
        self._priority = PriorityConfig()
        self.refresh_service = DataRefreshService(self._registry, self._priority)
        self._sync_trigger_callback = sync_trigger_callback
        self.screening = ScreeningGateway()

        from app.data.storage.mongo.repositories.metadata_repo import MetadataRepo

        self._metadata_repo = MetadataRepo()
        # Dashboard 统计 60s 共享缓存（stats 与 overview 各自键位、键含 market+domains）：
        # 数据本身分钟级更新，同步后 60s 内 dashboard 数字略旧是可接受的
        self._dashboard_cache = TTLCache(default_ttl=60)

    @classmethod
    def get_instance(cls) -> "DataInterface":
        """获取全局单例。"""
        global _instance
        if _instance is None:
            with _instance_lock:
                if _instance is None:
                    _instance = cls()
        return _instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（测试用）。"""
        global _instance
        with _instance_lock:
            _instance = None

    async def ensure_indexes(self) -> None:
        """启动期业务集合索引保障（应用层组装时调用一次）。

        业务集合索引的唯一事实源在 storage/mongo/index_definitions.py；
        core.database 不再 import 数据模块，由应用层经本契约入口触发。
        """
        from app.data.storage.mongo.client import get_motor_db
        from app.data.storage.mongo.index_definitions import ensure_all_market_indexes

        await ensure_all_market_indexes(get_motor_db())

    def reset_connections(self) -> None:
        """失效数据侧桥接缓存（应用层 shutdown 时在 close_db 前调用）。

        storage 层对 core 连接有懒缓存——core 关闭后不重置会让缓存指向
        已关闭的客户端（重连场景下读写旧实例）。
        """
        from app.data.storage.mongo import client as mongo_bridge
        from app.data.storage.redis import client as redis_bridge

        mongo_bridge.reset_client()
        redis_bridge.reset_client()
        logger.info("数据侧连接桥接缓存已重置")

    # ── 数据读取 ──

    async def read(
        self,
        market: str,
        domain: str,
        symbol: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        filters: Optional[Dict] = None,
        *,
        auto_refresh: bool = False,
        refresh_timeout: int = 30,
    ) -> Dict:
        """读取标准数据（消费方唯一读入口）。

        Args:
            market: 市场 (CN/HK/US，大小写不敏感)
            domain: 数据域 (basic_info/daily_quotes/...)，未注册域抛 ValueError
            symbol: 股票代码（可选，容忍多种写法：``000001.SZ`` / ``sz000001`` /
                ``1`` 均归一为存储口径；envelope 的 symbol 回显归一化结果）
            start_date: 起始日期（可选）
            end_date: 结束日期（可选）
            filters: 额外过滤条件（可选，如 list_status/statement_type 等）
            auto_refresh: 库空时自动触发该域按需刷新后重读一次（需 symbol，
                无 symbol 时该选项无效）。刷新自身失败不抛异常，落 refresh 摘要。
            refresh_timeout: auto_refresh 的刷新超时秒数

        Returns:
            envelope dict：{"data", "freshness", "market", "symbol", "domain",
            "auto_refreshed", "refresh"}。形状不变式：列表语义域 data 恒为
            List[Dict]（空为 ``[]``）；单例语义（basic_info + symbol）data 为
            Dict（空为 ``None``）。

        Raises:
            ValueError: domain 未注册或 market 非法（编程错误，fail-fast）
            DataUnavailableError: 存储故障（auto_refresh 补数后仍失败同样抛出）
        """
        market = _normalize_market(market)
        if not self.reader.supports_domain(domain):
            raise ValueError(f"未注册的数据域: {domain!r}")
        if symbol is not None:
            symbol = normalize_data_symbol(symbol, market, domain, filters)

        def normalized_date(value):
            text = str(value) if value is not None else None
            return f"{text[:4]}-{text[4:6]}-{text[6:]}" if text and len(text) == 8 and text.isdigit() else text
        start_date, end_date = normalized_date(start_date), normalized_date(end_date)
        from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
        context = ACTIVE_ANALYSIS_CONTEXT.get()
        if context and context.get("market") == market and domain != "trade_calendar":
            cutoff = context.get("intraday_end") if domain == "intraday_quotes" else context.get("as_of")
            if cutoff:
                end_date = min(end_date, cutoff) if end_date else cutoff
        from app.data.core.requirements import analysis_filters
        filters = analysis_filters(market, domain, symbol, start_date, end_date, filters, context)
        read_kwargs: Dict[str, Any] = dict(
            start_date=start_date, end_date=end_date, filters=filters
        )

        refreshed_summary: Optional[Dict] = None
        auto_refreshed = False
        freshness = "unknown"
        try:
            data, freshness = await self.reader.get_data(
                market, domain, symbol, **read_kwargs
            )
        except Exception as e:
            if not (auto_refresh and symbol):
                raise DataUnavailableError(market, domain, e) from e
            data = None

        # 库空/首读失败 → 按需补数后重读一次（无 symbol 无法按股补数，不触发）
        if auto_refresh and symbol and not data:
            auto_refreshed = True
            domain_result = None
            try:
                rr = await self.refresh(
                    market, symbol, domains=[domain],
                    force=True, timeout=refresh_timeout,
                    start_date=start_date, end_date=end_date, coverage_filters=filters,
                )
                domain_result = rr.domains.get(domain) if rr else None
            except Exception as e:  # noqa: BLE001 — 补数失败降级为摘要，不中断读取
                logger.warning(f"[read] {market}/{domain}/{symbol} 按需刷新失败: {e}")
            if domain_result is not None:
                refreshed_summary = {
                    "status": domain_result.status,
                    "source": domain_result.source,
                    "record_count": domain_result.record_count,
                    "error": domain_result.error,
                }
                try:
                    data, freshness = await self.reader.get_data(
                        market, domain, symbol, **read_kwargs
                    )
                except Exception as e:
                    raise DataUnavailableError(market, domain, e) from e
            else:
                refreshed_summary = {
                    "status": "skipped", "source": None,
                    "record_count": 0, "error": None,
                }

        from app.data.core.coverage import evaluate
        data, quality = evaluate(data, domain, start_date, end_date, filters)
        if market == "CN" and domain == "intraday_quotes" and data and symbol:
            from app.data.query.intraday_evidence import compare_intraday_daily
            days = sorted({str(row.get("datetime", ""))[:10] for row in data})
            # Compare closed sessions only; a partial session is not a missing day.
            closed = [day for day in days if not end_date or day < end_date[:10]
                      or len(end_date) == 10 or end_date[11:19] >= "15:00:00"]
            if closed:
                try:
                    daily = await self.read(market, "daily_quotes", symbol,
                        start_date=closed[0], end_date=closed[-1], filters={"price_basis": "raw"})
                    comparison = compare_intraday_daily(data, daily.get("data") or [], closed)
                except Exception as exc:
                    comparison = {"status": "partial", "reason": type(exc).__name__,
                                  "limitations": "日线交叉核验不可用，不据此删除已取得的分时OHLC；全日成交量/VWAP未核验。"}
                quality["daily_cross_check"] = comparison
                if comparison["status"] != "matched" and quality["status"] == "success":
                    quality.update(status="partial", reason="intraday_daily_volume_or_ohlc_unverified")
        if domain == "daily_indicators" and isinstance(data, list) and data:
            latest = max(data, key=lambda row: str(row.get("trade_date") or ""))
            optional = (latest.get("data_quality") or {}).get("field_status") or {}
            quality["optional_field_status"] = {field: status for field, status in optional.items()
                if field not in filters.get("required_fields", []) and status != "available"}
        quality["cache_freshness"] = getattr(freshness, "value", freshness)
        from app.data.core.analysis_context import record_read_trace
        record_read_trace({"market": market, "domain": domain, "symbol": symbol,
                           "start_date": start_date, "end_date": end_date, "filters": filters, "quality": quality})
        if context is not None:
            snapshots = context.setdefault("coverage_snapshots", {})
            key = f"{market}:{domain}:{symbol}:{start_date}:{end_date}"
            if key in snapshots or len(snapshots) < 100:
                snapshots[key] = {"market": market, "domain": domain, "symbol": symbol, "quality": quality}
        # 形状钳制：单例语义折叠为单 dict，列表语义空结果为 []（不再吐 None）
        if domain == "basic_info" and symbol:
            if isinstance(data, list):
                data = data[0] if data else None
        elif data is None:
            data = []

        result = {
            "data": data,
            "quality": quality,
            "freshness": freshness,
            "market": market,
            "symbol": symbol,
            "domain": domain,
            "auto_refreshed": auto_refreshed,
            "refresh": refreshed_summary,
        }
        if filters.get("contract") == "verified_same_basis_flow":
            from app.data.core.requirements import five_session_flow
            result["derived_context"] = five_session_flow(data or [], quality, filters.get("expected_dates") or [])
        return result

    async def intraday_research(self, symbol: str, now) -> Dict:
        """Bounded free 30-minute evidence; separate from daily research inputs."""
        if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
            raise ValueError("仅支持六位A股代码")
        from app.data.query.intraday_research import collect
        return await collect(self, symbol, now)

    async def structure_auxiliary_context(self, symbol: str, now) -> Dict:
        """One symbol's free 30-minute context. Does not refresh the whole market."""
        if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
            raise ValueError("仅支持六位A股代码")
        from app.data.query.structure_auxiliary import collect
        return await collect(self, symbol, now)

    async def analysis_context(self, market: str, as_of: str, sessions: int = 5) -> Dict:
        from app.data.core.analysis_context import build_context
        from app.core.async_utils import await_on_main_loop
        # AnalysisRuntime runs in a worker's asyncio.run loop, while Motor is
        # owned by the application loop. Calendar preparation needs the same
        # bridge as the other real data tools, including context propagation.
        return await await_on_main_loop(build_context(self, market, as_of, sessions))

    async def read_ready(self, market: str, domain: str, symbol: Optional[str] = None,
                         timeout: int = 30, **read_kwargs) -> Dict:
        """Bounded single-target completion; unavailable evidence never blocks peers.

        Return partial data plus structured diagnostics after real fallback. Do
        not refresh the whole market or turn a missing value into a zero.
        """
        market = _normalize_market(market)
        if symbol is not None:
            symbol = normalize_data_symbol(symbol, market, domain, read_kwargs.get("filters"))
        from datetime import datetime
        from app.data.core.market import get_market_timezone
        from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT, record_read_trace
        from app.data.core.requirements import analysis_filters
        import time
        began = time.monotonic()
        for key in ("start_date", "end_date"):
            value = str(read_kwargs.get(key) or "")
            if len(value) == 8 and value.isdigit():
                read_kwargs[key] = f"{value[:4]}-{value[4:6]}-{value[6:]}"
        context = ACTIVE_ANALYSIS_CONTEXT.get()
        if context and context.get("market") == market and domain != "trade_calendar":
            cutoff = context.get("intraday_end") if domain == "intraday_quotes" else context.get("as_of")
            if cutoff:
                read_kwargs["end_date"] = min(read_kwargs["end_date"], cutoff) if read_kwargs.get("end_date") else cutoff
        read_kwargs["filters"] = analysis_filters(
            market, domain, symbol, read_kwargs.get("start_date"), read_kwargs.get("end_date"),
            read_kwargs.get("filters"), ACTIVE_ANALYSIS_CONTEXT.get())
        result = {"data": [], "quality": {"status": "failed", "reason": "cache_read_failed"},
                  "market": market, "domain": domain, "symbol": symbol, "freshness": "unknown"}
        try:
            result = await self.read(market, domain, symbol, **read_kwargs)
            quality = result.get("quality") or {}
            today = datetime.now(get_market_timezone(market)).date().isoformat()
            end = read_kwargs.get("end_date")
            fresh = getattr(result.get("freshness"), "value", result.get("freshness")) == "fresh"
            if quality.get("status") in {"no_events", "not_applicable"} or (
                quality.get("status") == "success" and (fresh or (domain != "news" and end and end[:10] < today))
            ):
                return result
        except Exception as exc:
            logger.warning("按需缓存读取失败 %s/%s/%s: %s", market, domain, symbol, exc)
        if not symbol or symbol in {"__all__", "market"}:
            return result
        try:
            refreshed = await self.refresh(
                market, symbol, domains=[domain], timeout=timeout,
                start_date=str(read_kwargs["start_date"])[:10] if read_kwargs.get("start_date") else None,
                end_date=str(read_kwargs["end_date"])[:10] if read_kwargs.get("end_date") else None,
                coverage_filters=read_kwargs["filters"],
            )
            detail = refreshed.domains.get(domain)
            peer_finished = False
            if detail and detail.status == "skipped":
                from app.data.storage.redis.locks import DistributedLock
                peer_finished = await DistributedLock(f"lock:{market}:{domain}:{symbol}").wait_until_released(
                    max_wait=min(8, max(0, timeout - (time.monotonic() - began))))
            result = await self.read(market, domain, symbol, **read_kwargs)
            if detail:
                result["quality"].update(refresh_status=detail.status, refresh_source=detail.source,
                                          fallback_from=detail.fallback_from, refresh_error=detail.error)
                if detail.status == "skipped":
                    result["quality"]["peer_refresh_status"] = "completed" if peer_finished else "still_pending_or_unobservable"
                    if peer_finished and result["quality"].get("status") == "success" and getattr(result.get("freshness"), "value", result.get("freshness")) == "fresh":
                        result["quality"].update(refresh_status="coalesced", refresh_error=None)
                if not result.get("data") and detail.status == "no_events":
                    result["quality"].update(status="no_events", reason="source_confirmed_empty")
        except Exception as exc:
            logger.warning("有界免费补链失败 %s/%s/%s: %s", market, domain, symbol, exc)
            result["quality"].update(refresh_status="failed", refresh_error=type(exc).__name__)
        record_read_trace({"market": market, "domain": domain, "symbol": symbol,
                           "start_date": read_kwargs.get("start_date"), "end_date": read_kwargs.get("end_date"),
                           "filters": read_kwargs["filters"], "quality": result["quality"]})
        context = ACTIVE_ANALYSIS_CONTEXT.get()
        if context is not None:
            key = f"{market}:{domain}:{symbol}:{read_kwargs.get('start_date')}:{read_kwargs.get('end_date')}"
            if key in context.setdefault("coverage_snapshots", {}) or len(context["coverage_snapshots"]) < 100:
                context["coverage_snapshots"][key] = {"market": market, "domain": domain, "symbol": symbol, "quality": result["quality"]}
        return result

    async def price_reference(self, market: str, symbol: str, context: Dict) -> Dict:
        from app.core.async_utils import await_on_main_loop
        from app.data.query.price_reference import collect_price_reference
        return await await_on_main_loop(collect_price_reference(self, market, symbol, context))

    async def relative_volume_context(self, market: str, symbol: str, end_date: str, timeout: int = 8) -> Dict:
        """An explicitly different daily-volume statistic; no provider-field rewrite."""
        from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
        from app.data.core.requirements import relative_volume_5d
        context = ACTIVE_ANALYSIS_CONTEXT.get()
        if (not context or context.get("market") != market or context.get("calendar_status") != "success"
                or len(context.get("volume_reference_trade_dates") or context.get("recent_trade_dates") or []) < 6):
            context = await self.analysis_context(market, end_date, sessions=6)
        days = list(context.get("volume_reference_trade_dates") or context.get("recent_trade_dates") or [])[-6:]
        if market != "CN" or context.get("calendar_status") != "success" or len(days) != 6:
            return relative_volume_5d([], [])
        result = await self.read_ready(market, "daily_quotes", symbol, timeout=timeout, start_date=days[0], end_date=end_date,
            filters={"required_fields": ["volume", "close"], "expected_dates": days,
                     "contract": "verified_raw_daily_quotes", "calendar_verified": True})
        return relative_volume_5d(result.get("data") or [], days)

    async def valuation_history_context(self, market: str, symbol: str, end_date: str,
                                        sessions: int = 120, timeout: int = 12) -> Dict:
        """A bounded single-symbol long window, separate from the current quote."""
        from app.data.query.valuation_evidence import summarize_valuation_history
        if market != "CN" or not str(symbol).isdigit() or not 20 <= sessions <= 500:
            return {"status": "unavailable", "reason": "unsupported_history_request", "fields": {}}
        context = await self.analysis_context(market, end_date, sessions=sessions)
        dates = context.get("recent_trade_dates") or []
        if context.get("calendar_status") != "success" or len(dates) != sessions:
            return {"status": "unavailable", "reason": "actual_session_calendar_unverified", "fields": {},
                    "requested_sessions": sessions, "observed_session_count": len(dates)}
        read = await self.read_ready(market, "daily_indicators", symbol, timeout=timeout,
            start_date=dates[0], end_date=end_date,
            filters={"required_fields": ["pb", "pe_ttm"], "expected_dates": dates,
                     "contract": "verified_dated_indicators", "calendar_verified": True})
        result = summarize_valuation_history(read.get("data") or [], dates, sessions)
        result["quality"] = read.get("quality")
        result["status"] = "available" if all(item["status"] in {"available", "not_applicable_nonpositive"}
                                              for item in result["fields"].values()) else "partial"
        return result

    async def margin_eligibility(self, market: str, symbol: str, as_of: str) -> Dict:
        if market != "CN":
            return {"status": "unknown", "eligible": None, "reason": "market_eligibility_not_verified"}
        from app.data.sources.cn.shared.margin_series import fetch_margin_eligibility
        return await fetch_margin_eligibility(symbol, as_of)

    async def read_latest(
        self,
        market: str,
        domain: str,
        symbol: str,
        projection: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """读取指定股票在某域的最新一条记录（排序键按域自动推导）。"""
        market = _normalize_market(market)
        symbol = normalize_data_symbol(symbol, market, domain)
        return await self.reader.read_latest(market, domain, symbol, projection)

    async def technical_snapshot(self, market: str, symbol: str, end_date: str, refresh: bool = True,
                                 analysis_horizon: str = "short") -> Dict:
        """Read a bounded period-specific history and derive technical context."""
        from datetime import date, timedelta
        from app.core.analysis_horizon import get_horizon_profile
        from app.data.core.analysis_context import _calendar_days
        from app.data.query.technical_analysis import build_technical_snapshot, select_daily_bars, verified_price_group

        profile = get_horizon_profile(analysis_horizon)
        end = date.fromisoformat(end_date)
        start = (end - timedelta(days=profile.technical_lookback_days)).isoformat()
        records = await self.read(market, "daily_quotes", symbol, start_date=start, end_date=end_date, filters={"period": "daily"})
        calendar = await self.read(market, "trade_calendar", start_date=start, end_date=end_date)
        acquisition_warnings = []
        calendar_days = _calendar_days(calendar.get("data") or [])
        known_days = {day for day, opened in calendar_days.items() if opened is not None}
        requested_calendar_days = {
            (end - timedelta(days=i)).isoformat() for i in range(profile.technical_lookback_days + 1)}
        calendar_holes = profile.key != "short" and any(
            day not in known_days for day in requested_calendar_days)
        if refresh and (end_date not in known_days or calendar_holes):
            try:
                await self.refresh(market, "__calendar__", domains=["trade_calendar"], timeout=8, start_date=start, end_date=end_date)
                calendar = await self.read(market, "trade_calendar", start_date=start, end_date=end_date)
            except Exception as exc:
                acquisition_warnings.append(f"历史交易日历补取未完成（{type(exc).__name__}），仅使用已有日历证据。")
        priorities = await self._priority.get_priority(market, "daily_quotes")
        bars = select_daily_bars(records.get("data") or [], end_date, priorities)
        calendar_days = _calendar_days(calendar.get("data") or [])
        calendar_history_verified = all(calendar_days.get(day) is not None for day in requested_calendar_days)
        open_days = [day for day, opened in calendar_days.items() if opened is True and start <= day <= end_date]
        last = bars["trade_date"].max().strftime("%Y-%m-%d") if not bars.empty else ""
        first = bars["trade_date"].min().strftime("%Y-%m-%d") if not bars.empty else ""
        history_short = profile.key != "short" and (
            not calendar_history_verified or not first or (open_days and first > min(open_days)))
        if refresh and (len(bars) < 260 or (open_days and last < max(open_days))
                        or history_short
                        or (market == "CN" and not verified_price_group(bars, end_date))):
            try:
                await self.refresh(market, symbol, domains=["daily_quotes"], force=True, timeout=25,
                                   start_date=start, end_date=end_date)
                records = await self.read(market, "daily_quotes", symbol, start_date=start, end_date=end_date, filters={"period": "daily"})
            except Exception as exc:
                acquisition_warnings.append(f"历史行情补取未完成（{type(exc).__name__}），保留已验证的现有日线。")
        result = build_technical_snapshot(records.get("data") or [], calendar.get("data") or [], end_date, priorities,
                                          weekly_bars=profile.weekly_bars, monthly_bars=profile.monthly_bars)
        if acquisition_warnings:
            result.setdefault("warnings", []).extend(acquisition_warnings)
        if profile.key != "short":
            result["analysis_horizon"] = profile.key
            result["requested_history_start"] = start
            result["requested_history_end"] = end_date
            observed_start = result.get("history_start")
            result["history_window_status"] = (
                "available" if observed_start and open_days and observed_start <= min(open_days)
                and calendar_history_verified and result.get("calendar_complete") and not result.get("missing_open_dates")
                and result.get("data_cutoff") == max(open_days) else "partial")
            result["requested_calendar_verified"] = calendar_history_verified
            if result["history_window_status"] != "available":
                result.setdefault("warnings", []).append(
                    "所请求的中长线历史窗口尚未完整验证；可能涉及上市年限、停牌或源覆盖限制。保留已有证据，不把有限历史当作完整多年表现。")
        return result

    async def read_latest_batch(
        self,
        market: str,
        domain: str,
        symbols: List[str],
        projection: Optional[Dict] = None,
    ) -> Dict[str, Dict]:
        """批量读取多只股票各自的最新一条记录，返回 {symbol: doc}。"""
        market = _normalize_market(market)
        symbols = [normalize_data_symbol(s, market, domain) for s in symbols]
        return await self.reader.read_latest_batch(
            market, domain, symbols, projection
        )

    async def read_batch(
        self,
        market: str,
        domain: str,
        symbols: List[str],
        projection: Optional[Dict] = None,
        sort: Optional[List] = None,
        limit: int = 0,
    ) -> List[Dict]:
        """批量读取多只股票的记录（不折叠）。"""
        market = _normalize_market(market)
        symbols = [normalize_data_symbol(s, market, domain) for s in symbols]
        return await self.reader.read_batch(
            market, domain, symbols, projection, sort, limit
        )

    async def search_basic_info(
        self,
        market: str,
        query: str,
        fields: Optional[List[str]] = None,
        limit: int = 20,
    ) -> List[Dict]:
        """在 basic_info 中模糊搜索股票（symbol 前缀 / 名称包含）。"""
        market = _normalize_market(market)
        return await self.reader.search_basic_info(market, query, fields, limit)

    # ── 知识库检索（AI 数据面契约）──

    async def kb_search(
        self,
        market: str,
        symbol: str,
        query: str,
        *,
        as_of: Optional[str] = None,
        top_k: Optional[int] = None,
        types: Optional[List[str]] = None,
    ) -> List[Dict]:
        """新闻知识库向量检索 top-K 语料（AI 侧知识通道的唯一入口）。

        Args:
            market: 市场 (CN/HK/US)
            symbol: 股票代码（自动归一）
            query: 检索文本（关键词或自然语言）
            as_of: 截止日期，防前视硬约束——分析注入路径必须传分析日期
            top_k: 返回条数（默认 6）
            types: 语料类型过滤（news/announcement/…）

        Returns:
            语料 dict 列表；索引未就绪 / 未配置向量模型 / 无命中返回 []。
        """
        market = _normalize_market(market)
        symbol = normalize_symbol(symbol, market)
        from app.data.knowledge.retrieval import search

        return await asyncio.to_thread(
            search, market, symbol, query,
            as_of, top_k if top_k is not None else 6, types,
        )

    async def kb_record_citation(self, doc: Dict) -> bool:
        """写入一条引用校验记录（失败只记日志返回 False，不影响分析主链路）。"""
        from app.data.knowledge.stats import record_citation_stats

        return await asyncio.to_thread(record_citation_stats, doc)

    # ── 数据可用性（AI 可用性面契约）──

    async def get_availability(
        self, market: str, domains: Optional[List[str]] = None
    ) -> Dict[str, Dict]:
        """查询各域数据可用性（configured 与 has_data 正交）。

        - configured: 能力矩阵注册了源 → 可触发同步/refresh 补数
        - has_data: 库内有记录 → 可立即 read
        「有源无货」= 可补数；「无源有货」= 只能消费存量。

        Args:
            market: 市场 (CN/HK/US)
            domains: 待查域列表；None = 该市场全部注册域；含未注册域抛 ValueError

        Returns:
            {domain: {"configured", "has_data", "records", "last_updated", "sources"}}
        """
        market = _normalize_market(market)
        registered = set(self._registry.get_domains(market))
        if domains is None:
            targets = sorted(registered)
        else:
            unknown = [d for d in domains if d not in registered]
            if unknown:
                raise ValueError(f"未注册的数据域: {unknown}")
            targets = list(dict.fromkeys(domains))

        stats = await self.get_domain_stats(market, targets)
        availability: Dict[str, Dict] = {}
        for domain in targets:
            stat = stats.get(domain, {})
            records = stat.get("records", 0)
            availability[domain] = {
                "configured": True,
                "has_data": records > 0,
                "records": records,
                "last_updated": stat.get("last_updated"),
                "sources": self._registry.get_available_sources(
                    domain, market=market
                ) or [],
            }
        return availability

    # ── 筛选查询 ──

    async def screen(
        self,
        market: str,
        stage: str,
        filters: Optional[Dict] = None,
        projection: Optional[Dict] = None,
        sort: Optional[List] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> Dict:
        """单阶段筛选查询（委托 ScreeningQueryService，返回 {"items", "total"}）。"""
        from app.data.query.screening_query import ScreeningQueryService

        return await ScreeningQueryService().screen(
            market, stage,
            filters=filters,
            projection=projection,
            sort=sort,
            skip=skip,
            limit=limit,
        )

    # ── 数据刷新 ──

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
        coverage_filters: Optional[Dict] = None,
    ) -> RefreshResult:
        """按需刷新指定股票数据（symbol 容忍多种写法，自动归一）。"""
        market = _normalize_market(market)
        symbol = normalize_data_symbol(symbol, market, "index_quotes" if domains == ["index_quotes"] else "", coverage_filters)
        from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
        context = ACTIVE_ANALYSIS_CONTEXT.get()
        if context and context.get("market") == market and domains != ["trade_calendar"]:
            cutoff = context.get("as_of")
            if cutoff:
                end_date = min(end_date, cutoff) if end_date else cutoff
        return await self.refresh_service.refresh(
            market, symbol, domains, force, timeout, start_date=start_date, end_date=end_date,
            coverage_filters=coverage_filters,
        )

    # ── 同步管理 ──

    async def trigger_sync(
        self,
        market: str,
        domain: str,
        mode: Optional[str] = None,
        source: Optional[str] = None,
    ) -> str:
        """手动触发同步任务。优先走注入的回调，降级走调度引擎，最终降级走记录事件。

        mode/source 为调用方指定的覆盖值（incremental/full），
        仅在调度引擎分支生效；回调分支由回调方自行决定是否接收。
        """
        from datetime import datetime, timezone

        task_id = (
            f"sync_{market}_{domain}_{int(datetime.now(timezone.utc).timestamp())}"
        )

        # 优先使用注入的回调（由上层 worker 模块注册）
        if self._sync_trigger_callback:
            try:
                import inspect

                result = self._sync_trigger_callback(market, domain)
                # 回调可能是协程：需 await 才能真正执行
                if inspect.iscoroutine(result):
                    result = await result
                if result:
                    logger.info(f"通过回调触发同步: {result}")
                    return result
            except Exception as e:
                logger.warning(f"回调触发失败: {e}")

        # 降级：尝试调度引擎（后台执行，立即返回标识）。
        # SchedulerEngine 本就是数据模块组件（app/data/scheduler/engine），
        # 直接取其单例，不再绕道 app.worker（保持 data 不依赖上层）
        try:
            from app.data.scheduler.engine import SchedulerEngine

            engine = SchedulerEngine.get_instance()
            if engine:
                result = engine.run_job_now(market, domain, mode=mode, source=source)
                if result.get("status") in ("triggered", "already_running"):
                    logger.info(f"通过调度引擎触发同步: {result['task_id']}")
                    return result["task_id"]
        except Exception as e:
            logger.warning(f"调度引擎触发失败，降级到直接同步: {e}")

        # 降级：只记录事件（实际执行需等调度引擎可用）
        repo = self._metadata_repo
        await repo.insert_event(
            {
                "market": market,
                "event_type": "SYNC_START",
                "domain": domain,
                "task_id": task_id,
            }
        )
        logger.info(f"记录同步事件（降级模式）: {task_id}")
        return task_id

    async def get_running_sync_tasks(self, market: str) -> List[Dict]:
        """查询调度监控中当前运行中的同步任务（内存快照，进程重启后为空）。

        供 /sync/status 端点向前端暴露运行态：检查点只在任务收尾写入，
        没有这个快照前端无法感知「同步进行中」。
        """
        try:
            from app.data.scheduler.monitors import SchedulerMonitor

            monitor = SchedulerMonitor()
            if not getattr(monitor, "_running", False):
                return []
            return [
                r for r in monitor.get_running_tasks()
                if r.get("task_id", "").startswith(f"{market}:")
            ]
        except Exception as e:
            logger.debug(f"获取运行中同步任务失败: {e}")
            return []

    async def get_sync_status(
        self, market: str, domain: Optional[str] = None,
        trigger: Optional[str] = None,
    ) -> List[Dict]:
        """查询同步检查点列表。

        trigger: 仅返回该触发类型的检查点（manual/scheduled），None=不过滤。
        """
        return await self._metadata_repo.get_all_checkpoints(market, domain, trigger)

    async def get_sync_events(
        self, market: str, domain: Optional[str] = None, limit: int = 50
    ) -> List[Dict]:
        """查询同步事件。"""
        return await self._metadata_repo.get_events(market, domain, limit)

    # ── 数据源管理 ──

    async def get_source_health(self, market: str) -> List[Dict]:
        """获取数据源健康状态。优先从 MongoDB 读取，回退到内存监控。"""
        mongo_health = await self._metadata_repo.get_all_health(market)

        if mongo_health:
            # 过滤能力矩阵中已经移除的旧源，避免页面继续显示历史 Tushare
            # 健康记录，让用户误以为当前部署仍会调用收费源。
            allowed = {
                (source, domain)
                for domain in self._registry.get_domains(market)
                for source in self._registry.get_available_sources(domain, market)
            }
            return [
                item
                for item in mongo_health
                if (item.get("data_source") or item.get("source"), item.get("domain")) in allowed
            ]

        from app.data.monitoring.source_health import SourceHealthMonitor

        monitor = SourceHealthMonitor()
        return monitor.get_all_health(market)

    async def fetch_market_quotes(
        self,
        market: str,
        preferred_sources: Optional[List[str]] = None,
    ) -> Dict:
        """从外部免费源拉取全市场快照，不读取 MongoDB 缓存。"""
        from app.data.processor.fallback_router import FallbackRouter

        result = await FallbackRouter.get_instance().fetch(
            market,
            "market_quotes",
            "__all__",
            preferred_sources=preferred_sources,
        )
        return {
            "success": result.success,
            "data": result.records,
            "source": result.source,
            "error": result.error,
        }

    async def reset_source_circuit(self, market: str, source: str, domain: str) -> None:
        """运维重置指定源的熔断器（消费层路由的合法入口）。

        含三件事：重置 FallbackRouter 进程级单例里的熔断状态、同步内存
        health 条目（防 30s flush 用旧值覆盖）、把 Mongo health 快照置
        closed（进程刚重启内存无条目时，Mongo 是前端唯一可见状态）。
        """
        from app.data.processor.fallback_router import FallbackRouter

        router = FallbackRouter.get_instance()
        router._circuit.reset(source, domain=domain, market=market)

        from app.data.monitoring.source_health import SourceHealthMonitor

        SourceHealthMonitor().mark_circuit_closed(market, source, domain)

        await self._metadata_repo.upsert_health(
            market, source, domain, {"circuit_state": "closed"}
        )

    def get_capability_registry(self) -> CapabilityRegistry:
        """获取能力注册表。"""
        return self._registry

    # ── 域健康（数据可观测性三支柱：freshness / volume / 运行健康）──

    async def get_domain_health(self, market: str) -> Dict:
        """聚合 domain_stats + sync_checkpoints + source_health，返回域健康判定。

        判定逻辑在 app/data/core/health.py（纯逻辑可单测），本方法只做信号聚合：
        - 记录数 / 最新 updated_at：get_domain_stats
        - 最近同步时间 / 是否成功同步过：sync_checkpoints
        - 源运行状态：source_health（stale 快照过滤：>2h 且无内存热数据 → 无信号）
        - 覆盖率：domain 内 distinct symbol 数 / basic_info 股票总数（豁免域为 None）
        """
        from app.data.core.health import DomainHealthCalculator, HEALTH_SNAPSHOT_MAX_AGE_HOURS

        calculator = DomainHealthCalculator()
        domains = self._registry.get_domains(market)
        degraded_fields: List[str] = []

        domain_stats = await self.get_domain_stats(market, domains)

        checkpoints: List[Dict] = []
        try:
            checkpoints = await self._metadata_repo.get_all_checkpoints(market)
        except Exception as e:
            degraded_fields.append("sync_checkpoints")
            logger.warning(f"读取 {market} sync_checkpoints 失败: {e}")

        health_items: List[Dict] = []
        try:
            health_items = await self.get_source_health(market)
        except Exception as e:
            degraded_fields.append("source_health")
            logger.warning(f"读取 {market} source_health 失败: {e}")

        # stale Mongo 快照过滤：updated_at 超龄的条目视为无信号
        # （内存热数据无 updated_at 字段，天然保留）
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        fresh_health: List[Dict] = []
        for item in health_items:
            age = DomainHealthCalculator._age_hours(item.get("updated_at"), now)
            if age is not None and age > HEALTH_SNAPSHOT_MAX_AGE_HOURS:
                continue
            fresh_health.append(item)

        # 按 domain 分组源健康
        sources_by_domain: Dict[str, List[Dict]] = {}
        for item in fresh_health:
            sources_by_domain.setdefault(item.get("domain", ""), []).append(item)

        # checkpoint 按 domain 取最新一条
        latest_cp: Dict[str, Dict] = {}
        for cp in checkpoints:  # 已按 last_sync_time 降序
            latest_cp.setdefault(cp.get("domain", ""), cp)

        # 覆盖率：豁免域为 None，其余 = distinct symbols / basic_info 总数
        basic_info_total: Optional[int] = None
        symbol_counts: Dict[str, int] = {}
        try:
            from app.data.storage.mongo.client import get_motor_db
            from app.data.storage.mongo.collections import get_collection_name

            db = get_motor_db()
            basic_coll = db[get_collection_name("basic_info", market)]
            basic_info_total = await basic_coll.count_documents({})
            for domain in domains:
                if calculator._is_coverage_exempt(domain):
                    continue
                coll = db[get_collection_name(domain, market)]
                pipeline = [{"$group": {"_id": "$symbol"}}, {"$count": "n"}]
                agg = await coll.aggregate(pipeline).to_list(length=1)
                symbol_counts[domain] = agg[0]["n"] if agg else 0
        except Exception as e:
            degraded_fields.append("coverage")
            logger.warning(f"计算 {market} 覆盖率失败: {e}")

        domain_health = []
        for domain in domains:
            stats = domain_stats.get(domain, {})
            cp = latest_cp.get(domain)
            coverage = None
            if basic_info_total and symbol_counts.get(domain) is not None:
                coverage = round(symbol_counts[domain] / basic_info_total, 4)
            domain_health.append(calculator.evaluate(
                market=market,
                domain=domain,
                record_count=stats.get("records", 0),
                last_sync_time=(cp or {}).get("last_sync_time") or stats.get("last_updated"),
                checkpoint_success=bool(cp and cp.get("status") == "success"),
                sources=sources_by_domain.get(domain),
                coverage=coverage,
                monitoring_available=bool(fresh_health) or bool(sources_by_domain),
            ))

        summary = DomainHealthCalculator.summarize(domain_health)
        summary["degraded_fields"] = degraded_fields
        return {"domains": domain_health, "summary": summary}

    # ── 配置管理 ──

    async def get_config(self, market: str, domain: str) -> Optional[Dict]:
        """获取数据源优先级配置。"""
        return await self._metadata_repo.get_config(
            "data_source_priority", market, domain
        )

    async def update_config(
        self, market: str, domain: str, sources: List[str], updated_by: str = "user"
    ) -> bool:
        """更新数据源优先级。"""
        await self._metadata_repo.upsert_config(
            "data_source_priority",
            market,
            domain,
            {"sources": sources},
            updated_by,
        )
        self._priority.invalidate_cache(market, domain)
        logger.info(f"更新优先级: {market}/{domain} → {sources}")
        return True

    # ── Dashboard 统计 ──

    async def get_domain_stats(
        self, market: str, domains: List[str]
    ) -> Dict[str, Dict]:
        """获取各域统计信息（记录数 + 最后更新时间，60s 共享缓存）。"""
        cache_key = f"stats:{market}:{','.join(domains)}"
        cached = self._dashboard_cache.get(cache_key)
        if cached is not None:
            return cached

        from app.data.storage.mongo.client import get_motor_db
        from app.data.storage.mongo.collections import get_collection_name

        db = get_motor_db()
        stats: Dict[str, Dict] = {}
        for domain in domains:
            try:
                coll = db[get_collection_name(domain, market)]
                count = await coll.count_documents({})
                last_doc = await coll.find_one(
                    {},
                    {"updated_at": 1},
                    sort=[("updated_at", -1)],
                )
                stats[domain] = {
                    "records": count,
                    "last_updated": last_doc.get("updated_at") if last_doc else None,
                }
            except Exception as e:
                logger.debug(f"获取域统计失败: {domain}: {e}")
                stats[domain] = {"records": 0, "last_updated": None}
        self._dashboard_cache.set(cache_key, stats)
        return stats

    async def get_quotes_stats(self, market: str) -> Dict[str, int]:
        """获取日线行情集合统计（记录数 + 股票数）。

        用 aggregate $group 替代 distinct，避免大集合全表扫描。
        """
        from app.data.storage.mongo.client import get_motor_db
        from app.data.storage.mongo.collections import get_collection_name

        db = get_motor_db()
        coll = db[get_collection_name("daily_quotes", market)]
        total_records = await coll.count_documents({})
        pipeline = [{"$group": {"_id": "$symbol"}}, {"$count": "n"}]
        cursor = coll.aggregate(pipeline)
        agg = await cursor.to_list(length=1)
        total_symbols = agg[0]["n"] if agg else 0
        return {"total_records": total_records, "total_symbols": total_symbols}

    # ── 数据质量 ──

    async def get_quality_overview(
        self, market: str, domains: List[str]
    ) -> Dict[str, Dict]:
        """获取各域质量概览（记录数、完整率、最新日期，60s 共享缓存）。"""
        cache_key = f"overview:{market}:{','.join(domains)}"
        cached = self._dashboard_cache.get(cache_key)
        if cached is not None:
            return cached

        from app.data.storage.mongo.client import get_motor_db
        from app.data.storage.mongo.collections import get_collection_name

        db = get_motor_db()
        overview: Dict[str, Dict] = {}
        for domain in domains:
            try:
                coll = db[get_collection_name(domain, market)]
                total = await coll.count_documents({})
                missing_symbol = await coll.count_documents(
                    {"symbol": {"$exists": False}}
                )
                latest_doc = await coll.find_one(
                    {"trade_date": {"$exists": True}},
                    sort=[("trade_date", -1)],
                )
                latest_date = latest_doc.get("trade_date") if latest_doc else None
                overview[domain] = {
                    "total_records": total,
                    "missing_symbol": missing_symbol,
                    # 空集合 = 0% 完整（无数据不是"完整"，避免健康误报）
                    "completeness": round((total - missing_symbol) / total, 3)
                    if total > 0
                    else 0.0,
                    "empty": total == 0,
                    "latest_date": latest_date,
                }
            except Exception as e:
                overview[domain] = {"error": str(e)}
        self._dashboard_cache.set(cache_key, overview)
        return overview

    async def check_domain_quality(self, market: str, domain: str) -> Dict:
        """对指定域执行完整质量检查。"""
        from app.data.storage.mongo.client import get_motor_db
        from app.data.storage.mongo.collections import get_collection_name
        from datetime import datetime, timedelta, timezone

        _REQUIRED_FIELDS: Dict[str, List[str]] = {
            "daily_quotes": ["symbol", "trade_date", "close"],
            "daily_indicators": ["symbol", "trade_date"],
            "financial_data": ["symbol", "report_period"],
            "basic_info": ["symbol"],
        }
        _TIMESERIES_DOMAINS = ("daily_quotes", "daily_indicators", "adj_factors")

        db = get_motor_db()
        coll = db[get_collection_name(domain, market)]
        total = await coll.count_documents({})
        stats: Dict = {"total_records": total, "issues": []}

        if total == 0:
            stats["status"] = "empty"
            return stats

        required = _REQUIRED_FIELDS.get(domain, ["symbol"])
        for field in required:
            missing_count = await coll.count_documents(
                {
                    "$or": [{field: {"$exists": False}}, {field: None}, {field: ""}],
                }
            )
            if missing_count > 0:
                stats["issues"].append(
                    {
                        "type": "missing_field",
                        "field": field,
                        "count": missing_count,
                        "percentage": round(missing_count / total * 100, 2),
                    }
                )

        if domain in _TIMESERIES_DOMAINS:
            thirty_days_ago = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).strftime("%Y-%m-%d")
            try:
                pipeline = [
                    {"$match": {"trade_date": {"$gte": thirty_days_ago}}},
                    {"$group": {"_id": "$trade_date", "count": {"$sum": 1}}},
                    {"$sort": {"_id": 1}},
                ]
                cursor = coll.aggregate(pipeline)
                date_counts = await cursor.to_list(length=None)
                trading_days_covered = len(date_counts)
                try:
                    cal_coll = db[get_collection_name("trade_calendar", market)]
                    expected_days = await cal_coll.count_documents(
                        {
                            "is_open": 1,
                            "cal_date": {"$gte": thirty_days_ago},
                        }
                    )
                except Exception as e:
                    logger.debug(f"交易日历查询失败: {e}")
                    expected_days = None
                stats["date_continuity"] = {
                    "trading_days_covered": trading_days_covered,
                    "expected_trading_days": expected_days,
                    "coverage_rate": round(trading_days_covered / expected_days, 3)
                    if expected_days
                    else None,
                    "period": "last_30_days",
                }
            except Exception as e:
                stats["date_continuity"] = {"error": str(e)}

        if domain == "daily_quotes":
            try:
                bi_coll = db[get_collection_name("basic_info", market)]
                active_stocks = await bi_coll.count_documents({"list_status": "L"})
                if active_stocks > 0:
                    latest = await coll.find_one(sort=[("trade_date", -1)])
                    if latest:
                        latest_date = latest.get("trade_date", "")
                        covered = await coll.count_documents(
                            {"trade_date": latest_date}
                        )
                        stats["stock_coverage"] = {
                            "active_stocks": active_stocks,
                            "covered_stocks": covered,
                            "coverage_rate": round(covered / active_stocks, 3),
                            "latest_date": latest_date,
                        }
            except Exception as e:
                stats["stock_coverage"] = {"error": str(e)}

        stats["status"] = "ok" if not stats["issues"] else "warning"
        return stats
