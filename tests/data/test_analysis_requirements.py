"""Pure contract tests and real isolated Mongo paths; no provider replacement."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.data.core.coverage import evaluate
from app.data.core.requirements import analysis_filters, five_session_flow, relative_volume_5d
from app.engine.data_preflight import declare_role, record_tool, render_role_gaps, summarize_result


DAYS = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"]
CONTEXT = {"market": "CN", "as_of": "2026-10-09", "calendar_status": "success", "recent_trade_dates": DAYS}


def flow(day, source="sina", **changes):
    row = {"symbol": "601918", "market": "CN", "data_source": source,
           "trade_date": day, "main_net_inflow": 100.0, "amount_unit": "CNY",
           "flow_date_verified": True, "flow_date_basis": "source_row_date",
           "flow_basis": "sina_large_order_v1" if source == "sina" else "eastmoney_order_size_v1"}
    row.update(changes)
    return row


def filters(domain="money_flow", **changes):
    result = analysis_filters("CN", domain, "601918", "2026-09-01", "2026-10-09", {}, CONTEXT)
    result.update(changes)
    return result


@pytest.mark.parametrize("symbol", ["601918", "000001", "300750", "920001"])
def test_contract_is_per_requested_symbol_not_a_stock_allowlist(symbol):
    actual = analysis_filters("CN", "money_flow", symbol, None, "2026-10-09", {}, CONTEXT)
    assert actual["expected_dates"] == DAYS
    assert "2026-10-01" not in actual["expected_dates"]


def test_no_analysis_context_does_not_change_legacy_or_market_reads():
    assert analysis_filters("CN", "money_flow", "601918", None, None, {}, None) == {}
    assert analysis_filters("CN", "money_flow", "__all__", None, None, {}, CONTEXT) == {}
    assert analysis_filters("HK", "money_flow", "00700", None, None, {}, CONTEXT) == {}


@pytest.mark.parametrize("reverse", [False, True])
def test_cache_freshness_business_date_is_independent_of_repository_sort(reverse):
    from app.data.core.reader import Reader
    rows = [{"trade_date": day} for day in DAYS]
    if reverse:
        rows.reverse()
    assert Reader.latest_business_date(rows) == DAYS[-1]
    assert Reader.latest_business_date(rows + [{"trade_date": True}, {"trade_date": "unknown"}]) == DAYS[-1]


def test_complete_single_definition_computes_five_actual_sessions():
    rows = [flow(day) for day in DAYS]
    original = deepcopy(rows)
    selected, quality = evaluate(rows, "money_flow", "2026-09-01", "2026-10-09", filters())
    summary = five_session_flow(selected, quality, DAYS)
    assert rows == original
    assert quality["status"] == "success"
    assert summary["five_session_net_flow"] == 500
    assert summary["flow_basis"] == "sina_large_order_v1"
    assert summary["is_eastmoney_main_flow"] is False


def test_same_named_field_does_not_merge_different_flow_definitions():
    rows = [flow(day, "akshare") for day in DAYS[:3]] + [flow(day) for day in DAYS[3:]]
    selected, quality = evaluate(rows, "money_flow", None, "2026-10-09", filters())
    assert quality["status"] == "partial"
    assert quality["missing_dates"] == DAYS[3:]
    assert {row["data_source"] for row in selected} == {"akshare"}
    assert five_session_flow(selected, quality, DAYS)["five_session_net_flow"] is None


@pytest.mark.parametrize("change", [{"main_net_inflow": None}, {"main_net_inflow": True},
    {"main_net_inflow": float("nan")}, {"main_net_inflow": float("inf")},
    {"flow_date_verified": False}, {"flow_date_basis": "capture_date"},
    {"amount_unit": "unknown"}, {"flow_basis": "unknown"}])
def test_invalid_daily_observation_is_not_a_completed_window(change):
    rows = [flow(day) for day in DAYS]
    rows[-1].update(change)
    selected, quality = evaluate(rows, "money_flow", None, "2026-10-09", filters())
    assert quality["status"] != "success"
    assert five_session_flow(selected, quality, DAYS)["five_session_net_flow"] is None


def test_conflicting_duplicate_day_is_quarantined_not_counted_twice():
    rows = [flow(day) for day in DAYS] + [flow(DAYS[-1], main_net_inflow=-100)]
    _, quality = evaluate(rows, "money_flow", None, "2026-10-09", filters())
    assert quality["missing_dates"] == [DAYS[-1]]


def test_old_and_future_rows_do_not_hide_a_missing_actual_session():
    rows = [flow(day) for day in DAYS[:-1]] + [flow("2026-09-25"), flow("2026-10-12")]
    _, quality = evaluate(rows, "money_flow", None, "2026-10-09", filters())
    assert quality["missing_dates"] == [DAYS[-1]]
    assert quality["future_rows_excluded"] == 1


def test_calendar_hole_cannot_be_certified_by_some_returned_rows():
    _, quality = evaluate([flow(day) for day in DAYS], "money_flow", None, "2026-10-09",
                          filters(calendar_verified=False))
    assert quality["status"] == "partial"
    assert "actual_session_calendar_unverified" in quality["contract_issues"]


def indicator(**changes):
    row = {"symbol": "601918", "market": "CN", "data_source": "akshare", "trade_date": DAYS[-1],
           "pe_ttm": 10.0, "pb": 1.5, "field_provenance": {
               key: {"data_source": "akshare", "as_of": DAYS[-1], "date_verified": True,
                     "method": "baidu_valuation_history", "basis": "ttm" if key == "pe_ttm" else "vendor_pb"}
               for key in ("pe_ttm", "pb")}}
    row.update(changes)
    return row


def test_current_indicator_fields_need_their_own_dated_provenance():
    row = indicator()
    _, good = evaluate([row], "daily_indicators", None, DAYS[-1], filters("daily_indicators"))
    assert good["status"] == "success"
    row["field_provenance"]["pe_ttm"]["basis"] = "dynamic"
    selected, bad = evaluate([row], "daily_indicators", None, DAYS[-1], filters("daily_indicators"))
    assert bad["status"] == "partial"
    assert selected[0]["pe_ttm"] is None and selected[0]["pb"] == 1.5


def test_previous_indicator_date_is_not_today_even_with_a_current_document_label():
    row = indicator()
    row["field_provenance"]["pb"]["as_of"] = DAYS[-2]
    _, bad = evaluate([row], "daily_indicators", None, DAYS[-1], filters("daily_indicators"))
    assert bad["missing_fields"] == ["pb"]


def quote(day, volume, **changes):
    row = {"symbol": "601918", "market": "CN", "data_source": "baostock", "trade_date": day,
           "price_basis": "raw", "unit_conversion": "baostock:identity", "volume_unit": "share",
           "amount_unit": "CNY", "close": 10.0, "volume": volume}
    row.update(changes)
    return row


def test_derived_relative_volume_is_explicitly_not_vendor_volume_ratio():
    days = ["2026-09-25", *DAYS]
    rows = [quote(day, 100 if index < 5 else 250) for index, day in enumerate(days)]
    original = deepcopy(rows)
    result = relative_volume_5d(rows, days)
    assert result["value"] == 2.5
    assert result["is_vendor_volume_ratio"] is False
    assert "volume_ratio" not in result
    assert rows == original


@pytest.mark.parametrize("change", [{"volume": True}, {"volume": float("inf")},
    {"volume_unit": "lot"}, {"price_basis": "unknown"}, {"unit_conversion": None},
    {"data_source": "akshare"}])
def test_relative_volume_requires_six_same_source_verified_share_observations(change):
    days = ["2026-09-25", *DAYS]
    rows = [quote(day, 100) for day in days]
    rows[-1].update(change)
    assert relative_volume_5d(rows, days)["value"] is None


def test_missingness_is_reported_without_a_pipeline_exception():
    context = {}
    declare_role(context, "custom_role", ["missing_tool"], [])
    record_tool(context, "custom_role", "missing_tool", "failed", "all_free_sources_failed",
                coverage=({"domain": "money_flow", "quality": {"status": "partial", "missing_dates": DAYS[-1:]}},))
    report = render_role_gaps(context, "custom_role")
    assert "未中止整体分析" in report
    assert "all_free_sources_failed" in report and DAYS[-1] in report


def test_confirmed_empty_event_result_is_not_a_network_failure():
    read = {"domain": "dragon_tiger", "market": "CN", "symbol": "601918",
            "start_date": DAYS[-1], "end_date": DAYS[-1], "quality": {"status": "no_events"}}
    status, reason = summarize_result({"status": "no_data", "data": "No returned events"},
        ["dragon_tiger"], CONTEXT, coverage=(read,), args={"ts_code": "601918", "trade_date": "20261009"})
    assert status == "no_events" and "source_confirmed" in reason
    assert summarize_result({"status": "no_data", "data": "No returned events"},
        ["dragon_tiger"], CONTEXT, args={"ts_code": "601918"})[0] == "empty"


@pytest.mark.asyncio
async def test_real_cache_partial_fields_remain_partial_not_zero(real_mongo_db):
    from app.core.config import settings
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.data.core.interface import DataInterface
    from app.data.storage.mongo.collections import get_collection_name
    from isolated_database import require_isolated_database
    require_isolated_database()
    symbol = "unit_" + uuid4().hex
    # Valid numeric identities are needed by repository validation. Use a
    # synthetic collection symbol only for direct deterministic read isolation.
    coll = real_mongo_db[get_collection_name("money_flow", "CN")]
    rows = [{**flow(day), "symbol": symbol} for day in DAYS[:-1]]
    await coll.insert_many(rows)
    DataInterface.reset_instance()
    token = ACTIVE_ANALYSIS_CONTEXT.set(CONTEXT.copy())
    try:
        result = await DataInterface.get_instance().read("CN", "money_flow", symbol,
            start_date="2026-09-01", end_date=DAYS[-1], filters=filters())
        assert result["quality"]["missing_dates"] == DAYS[-1:]
        assert result["derived_context"]["five_session_net_flow"] is None
        assert await coll.count_documents({"symbol": symbol}) == 4
    finally:
        await coll.delete_many({"symbol": symbol})
        ACTIVE_ANALYSIS_CONTEXT.reset(token)
        DataInterface.reset_instance()


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["dragon_tiger", "block_trade"])
async def test_real_event_range_is_filtered_before_limit_not_after(real_mongo_db, domain):
    from app.core.config import settings
    from app.data.core.interface import DataInterface
    from app.data.storage.mongo.collections import get_collection_name
    from isolated_database import require_isolated_database
    require_isolated_database()
    symbol = "unit_" + uuid4().hex
    coll = real_mongo_db[get_collection_name(domain, "CN")]
    await coll.insert_many([{"symbol": symbol, "trade_date": day} for day in [*DAYS, "2026-10-12"]])
    DataInterface.reset_instance()
    try:
        result = await DataInterface.get_instance().read("CN", domain, symbol,
            start_date="2026-09-30", end_date="2026-09-30", filters={"limit": 1})
        assert len(result["data"]) == 1 and result["data"][0]["trade_date"] == "2026-09-30"
    finally:
        await coll.delete_many({"symbol": symbol})
        DataInterface.reset_instance()
