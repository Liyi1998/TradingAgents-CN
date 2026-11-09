"""Bounded public-provider acceptance against an isolated real DB. No LLM calls."""
import asyncio
import json
import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("FREE_ON_DEMAND_PROVIDER_ACCEPTANCE") != "1",
    reason="Explicit real public-network acceptance only",
)


@pytest.fixture
async def real_analysis_facade(real_mongo_db):
    import app.core.database as database
    from app.core.async_utils import set_main_loop
    from app.core.config import settings
    from app.data.core.interface import DataInterface
    from app.data.storage.redis.client import reset_client as reset_redis
    assert settings.MONGO_DB == "tradingagents_on_demand_validation_20261009"
    await database.db_manager.init_redis()
    database.redis_client = database.db_manager.redis_client
    database.redis_pool = database.db_manager.redis_pool
    reset_redis()
    assert await database.redis_client.ping()
    set_main_loop(asyncio.get_running_loop())
    DataInterface.reset_instance()
    try:
        yield DataInterface.get_instance()
    finally:
        set_main_loop(None)
        DataInterface.reset_instance()
        database.redis_client = None
        database.redis_pool = None
        reset_redis()


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["601918", "000001"])
async def test_empty_cache_requests_actual_free_flow_and_never_fabricates_a_sum(real_analysis_facade, symbol):
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.data.storage.mongo.client import get_motor_db
    from app.data.storage.mongo.collections import get_collection_name
    di = real_analysis_facade
    await get_motor_db()[get_collection_name("money_flow", "CN")].delete_many({"symbol": symbol})
    context = await di.analysis_context("CN", "2026-10-09")
    assert context["calendar_status"] == "success", "Actual exchange calendar must be verified"
    assert context["recent_trade_dates"] == ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"]
    token = ACTIVE_ANALYSIS_CONTEXT.set(context)
    began = time.monotonic()
    try:
        result = await di.read_ready("CN", "money_flow", symbol, timeout=26,
            start_date="2026-09-01", end_date="2026-10-09")
        quality = result["quality"]
        window = result.get("derived_context") or {}
        assert time.monotonic() - began < 36
        assert "refresh_status" in quality, "First acceptance run must actually exercise cold-cache refresh"
        if quality["status"] == "success":
            assert window["status"] == "available"
            assert window["flow_basis"] in {"eastmoney_order_size_v1", "sina_large_order_v1"}
            assert window["trade_dates"] == context["recent_trade_dates"]
            warm = await di.read_ready("CN", "money_flow", symbol, timeout=26,
                start_date="2026-09-01", end_date="2026-10-09")
            assert warm["quality"]["cache_freshness"] == "fresh"
            assert "refresh_status" not in warm["quality"], "Completed fresh caches must not repeatedly refresh"
            assert warm["derived_context"]["five_session_net_flow"] == window["five_session_net_flow"]
        else:
            assert window.get("five_session_net_flow") is None
            assert quality.get("missing_dates") or quality.get("missing_fields") or quality.get("contract_issues")
        print("PUBLIC_FLOW_ACCEPTANCE", json.dumps({"symbol": symbol, "quality": quality, "window": window}, ensure_ascii=False, default=str))
    finally:
        ACTIVE_ANALYSIS_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["601918", "000503"])
async def test_today_raw_quotes_and_indicators_attempt_actual_completion(real_analysis_facade, symbol):
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.data.storage.mongo.client import get_motor_db
    from app.data.storage.mongo.collections import get_collection_name
    di = real_analysis_facade
    for domain in ("daily_quotes", "daily_indicators"):
        await get_motor_db()[get_collection_name(domain, "CN")].delete_many({"symbol": symbol})
    context = await di.analysis_context("CN", "2026-10-09")
    token = ACTIVE_ANALYSIS_CONTEXT.set(context)
    try:
        quote = await di.read_ready("CN", "daily_quotes", symbol, timeout=26,
                                   start_date="2026-09-01", end_date="2026-10-09")
        indicators = await di.read_ready("CN", "daily_indicators", symbol, timeout=26,
                                        start_date="2026-09-01", end_date="2026-10-09")
        relative = await di.relative_volume_context("CN", symbol, "2026-10-09")
        assert relative["is_vendor_volume_ratio"] is False
        if relative["status"] == "available":
            assert len(relative["trade_dates"]) == 6 and relative["data_source"] in {"akshare", "baostock"}
        for result in (quote, indicators):
            assert result["quality"]["status"] in {"success", "partial", "failed", "conflict"}
            assert result["quality"].get("refresh_status")
        assert not any(row.get("volume_ratio") is not None for row in indicators.get("data") or [])
        if symbol == "000503":
            assert quote["quality"]["status"] == "success", "Incident stock's current price must genuinely be recovered"
            last = next(row for row in quote["data"] if row["trade_date"] == "2026-10-09")
            assert last["close"] == 8.43
        print("PUBLIC_PRICE_INDICATOR_ACCEPTANCE", json.dumps({"symbol": symbol, "quote_quality": quote["quality"],
            "indicator_quality": indicators["quality"], "relative_volume": relative}, ensure_ascii=False, default=str))
    finally:
        ACTIVE_ANALYSIS_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_worker_loop_preparation_matches_the_real_production_runtime(real_analysis_facade):
    from app.data.core.analysis_context import prompt_context
    # Actual worker asyncio.run -> application loop -> actual Motor reads. No
    # replacement client, function or event loop; this reproduced the incident.
    context = await asyncio.to_thread(lambda: asyncio.run(real_analysis_facade.analysis_context("CN", "2026-10-09")))
    assert context["calendar_status"] == "success"
    assert context["as_of"] == context["last_actual_trade_date"] == "2026-10-09"
    assert context["time_semantics"] == "analysis_date_observation_cutoff_not_request_start_freeze"
    prompt = prompt_context(context)
    assert "不是行情发生时间" in prompt and "晚于任务/角色启动几秒" in prompt
    print("WORKER_LOOP_CALENDAR_ACCEPTANCE", json.dumps({key: context[key] for key in
        ("as_of", "calendar_status", "recent_trade_dates", "observation_cutoff_local", "time_semantics")}, ensure_ascii=False))


@pytest.mark.asyncio
async def test_incident_event_tools_do_not_write_other_stocks_and_preserve_empty_semantics(real_analysis_facade):
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.data.storage.mongo.client import get_motor_db
    from app.data.storage.mongo.collections import get_collection_name
    from app.engine.data_preflight import ACTIVE_PREFLIGHT, PreflightSession, declare_role
    from app.engine.orchestrator.agents import build_tool_data
    from app.engine.tools.datasources.registry import get_specs_by_ids
    di = real_analysis_facade
    context = await di.analysis_context("CN", "2026-10-09")
    specs = get_specs_by_ids(["dragon_tiger", "block_trade"])
    other_counts = {}
    for domain in ("dragon_tiger", "block_trade"):
        coll = get_motor_db()[get_collection_name(domain, "CN")]
        await coll.delete_many({"symbol": "000503"})
        other_counts[domain] = await coll.count_documents({"symbol": {"$ne": "000503"}})
    token = ACTIVE_ANALYSIS_CONTEXT.set(context)
    session = PreflightSession()
    preflight_token = ACTIVE_PREFLIGHT.set(session)
    try:
        declare_role(context, "incident_event_tools", [spec.tool_id for spec in specs], specs)
        text = await build_tool_data("事件真实取数验收", specs, [], {"ticker": "000503", "trade_date": "2026-10-09"},
                                    role_key="incident_event_tools", analysis_context=context)
        assert text
        for domain, count in other_counts.items():
            assert await get_motor_db()[get_collection_name(domain, "CN")].count_documents({"symbol": {"$ne": "000503"}}) == count
        checks = context["role_data_checks"]["incident_event_tools"]["tools"]
        assert all(row["status"] in {"available", "no_events", "failed", "partial"} for row in checks)
        print("INCIDENT_EVENT_TOOL_ACCEPTANCE", json.dumps(checks, ensure_ascii=False, default=str))
    finally:
        await session.close()
        ACTIVE_PREFLIGHT.reset(preflight_token)
        ACTIVE_ANALYSIS_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_unavailable_historical_flow_does_not_stop_the_next_real_tool(real_analysis_facade):
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.engine.data_preflight import ACTIVE_PREFLIGHT, PreflightSession, declare_role
    from app.engine.orchestrator.agents import build_tool_data
    from app.engine.tools.datasources.registry import DatasourceSpec, get_specs_by_ids
    di = real_analysis_facade
    context = await di.analysis_context("CN", "1999-01-04")
    missing = get_specs_by_ids(["money_flow"])[0]
    actual_calendar = DatasourceSpec("real_calendar_after_missing", "真实交易日历", ["trade_calendar"], ["CN"],
        di.read, {"market": "CN", "domain": "trade_calendar", "start_date": "1998-12-01", "end_date": "1999-01-04"}, "Real facade read")
    specs = [missing, actual_calendar]
    session = PreflightSession()
    token = ACTIVE_ANALYSIS_CONTEXT.set(context)
    preflight_token = ACTIVE_PREFLIGHT.set(session)
    try:
        declare_role(context, "actual_missingness_acceptance", [spec.tool_id for spec in specs], specs)
        text = await build_tool_data("真实链路验收", specs, [], {"ticker": "601918", "trade_date": "1999-01-04"},
                                    role_key="actual_missingness_acceptance", analysis_context=context)
        checks = context["role_data_checks"]["actual_missingness_acceptance"]["tools"]
        assert checks[0]["status"] in {"failed", "partial", "empty"}
        assert checks[1]["status"] == "available"
        assert text and "真实交易日历" in text
        print("MISSING_ITEM_CONTINUED", json.dumps(checks, ensure_ascii=False, default=str))
    finally:
        await session.close()
        ACTIVE_PREFLIGHT.reset(preflight_token)
        ACTIVE_ANALYSIS_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_overview_refreshes_all_declared_indices_without_losing_completed_peers(real_analysis_facade):
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT, ACTIVE_READ_TRACE
    from app.engine.tools.datasources.china_market import get_china_market_overview
    context = await real_analysis_facade.analysis_context("CN", "2026-10-09")
    trace = []
    token = ACTIVE_ANALYSIS_CONTEXT.set(context)
    trace_token = ACTIVE_READ_TRACE.set(trace)
    began = time.monotonic()
    try:
        result = await asyncio.to_thread(get_china_market_overview, "2026-10-09", True, False)
        assert time.monotonic() - began < 30
        assert all(name in result for name in ("上证指数", "深证成指", "创业板指"))
        last_checks = {row["symbol"]: row for row in trace if row.get("domain") == "market_quotes"}
        assert set(last_checks) == {"000001.SH", "399001.SZ", "399006.SZ"}
        assert any(row["quality"]["status"] == "success" for row in last_checks.values()), "Actual index evidence must be obtained"
        print("OVERVIEW_REAL_INDEX_ACCEPTANCE", json.dumps(last_checks, ensure_ascii=False, default=str))
    finally:
        ACTIVE_READ_TRACE.reset(trace_token)
        ACTIVE_ANALYSIS_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_composite_fundamentals_really_reads_company_price_and_finance(real_analysis_facade):
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT, ACTIVE_READ_TRACE
    from app.engine.tools.datasources.fundamentals import get_stock_fundamentals
    context = await real_analysis_facade.analysis_context("CN", "2026-10-09")
    trace = []
    token = ACTIVE_ANALYSIS_CONTEXT.set(context)
    trace_token = ACTIVE_READ_TRACE.set(trace)
    began = time.monotonic()
    try:
        result = await asyncio.to_thread(get_stock_fundamentals, "000503", "2026-10-09")
        assert time.monotonic() - began < 55
        assert all(title in result for title in ("公司身份", "当期行情", "A股基本面财务数据"))
        assert "8.43" in result, "The incident stock's verified current close must actually reach the composite evidence"
        last_checks = {row["domain"]: row for row in trace if row.get("symbol") == "000503"}
        assert set(last_checks) == {"basic_info", "daily_quotes", "financial_data"}
        assert last_checks["daily_quotes"]["quality"]["status"] == "success"
        assert "请先同步" not in result
        print("COMPOSITE_REAL_DOMAIN_ACCEPTANCE", json.dumps(last_checks, ensure_ascii=False, default=str))
    finally:
        ACTIVE_READ_TRACE.reset(trace_token)
        ACTIVE_ANALYSIS_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_incident_quote_reaches_registered_analyst_tool_evidence(real_analysis_facade):
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.engine.data_preflight import ACTIVE_PREFLIGHT, PreflightSession, declare_role
    from app.engine.orchestrator.agents import build_tool_data
    from app.engine.tools.datasources.registry import get_specs_by_ids
    context = await real_analysis_facade.analysis_context("CN", "2026-10-09")
    specs = get_specs_by_ids(["daily_quotes"])
    assert len(specs) == 1
    session = PreflightSession()
    token = ACTIVE_ANALYSIS_CONTEXT.set(context)
    preflight_token = ACTIVE_PREFLIGHT.set(session)
    try:
        declare_role(context, "incident_price_material", [spec.tool_id for spec in specs], specs)
        text = await build_tool_data("现价材料真实验收", specs, [], {"ticker": "000503", "trade_date": "2026-10-09"},
                                    role_key="incident_price_material", analysis_context=context)
        checks = context["role_data_checks"]["incident_price_material"]["tools"]
        assert checks[0]["status"] == "available"
        assert "2026-10-09" in text and "8.43" in text
        coverage = checks[0]["coverage"]
        assert any(row["status"] == "success" and row["symbol"] == "000503" for row in coverage)
        assert any(row["domain"] == "daily_quotes" and row["symbol"] == "000503"
                   and row["quality"].get("data_cutoff") == "2026-10-09"
                   for row in context["coverage_snapshots"].values())
        print("INCIDENT_PRICE_MATERIAL_ACCEPTANCE", json.dumps(checks, ensure_ascii=False, default=str))
    finally:
        await session.close()
        ACTIVE_PREFLIGHT.reset(preflight_token)
        ACTIVE_ANALYSIS_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_real_unavailable_calendar_keeps_date_and_allows_independent_read(real_analysis_facade):
    # Before this exchange's public history: an actual unavailable calendar, not
    # a fake provider, patched facade or a made-up weekday calendar.
    context = await real_analysis_facade.analysis_context("CN", "1970-01-05")
    assert context["as_of"] == context["analysis_date"] == "1970-01-05"
    assert context["calendar_status"] == "failed"
    assert context["recent_trade_dates"] == [] and context["intraday_end"] is None
    assert context["limitations"]
    next_read = await real_analysis_facade.read("CN", "trade_calendar", start_date="2026-09-28", end_date="2026-10-09")
    assert next_read["data"] and next_read["quality"]["status"] == "success"
    print("UNAVAILABLE_CALENDAR_CONTINUED", json.dumps(context, ensure_ascii=False, default=str))
