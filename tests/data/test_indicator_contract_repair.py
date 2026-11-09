"""Real parsers/adapters and Mongo merge semantics; no mocked source or storage."""

import asyncio
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import pytest

from app.data.core.interface import DataInterface
from app.data.schema.domains.daily_indicators import indicator_quality
from app.data.sources.base.exceptions import DataNotFoundError
from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
from app.data.sources.cn.akshare.api.daily_indicators import (
    _determine_period,
    attach_cash_dividend_yields,
    fetch_daily_indicators_batch,
    merge_indicator_history_frames,
    parse_indicator_date,
    quote_history_indicator_frame,
    snapshot_capture_date,
)
from app.data.sources.cn.akshare.api.daily_quotes import bind_daily_quote_source
from app.data.sources.cn.akshare.api.cash_dividends import (
    CashDividendHistory,
    derive_cash_dividend_yield,
    parse_cash_dividend_tables,
)
from app.data.sources.cn.tencent.adapter import TencentCNAdapter
from app.data.storage.mongo.repositories.daily_indicators_repo import DailyIndicatorsRepo


@pytest.mark.parametrize("value", ["2026-10-08", "20261008"])
def test_indicator_dates_accept_iso_and_legacy_compact(value):
    assert parse_indicator_date(value) == date(2026, 10, 8)


@pytest.mark.parametrize("value", ["2026-02-30", "2026/10/08", "2026-1-8", "20261008150000", "bad"])
def test_indicator_dates_reject_ambiguous_or_invalid_values(value):
    with pytest.raises(ValueError):
        parse_indicator_date(value)


def test_historical_window_uses_iso_dates_and_is_anchored_to_present():
    today = date(2026, 10, 8)
    assert _determine_period("2024-08-01", "2026-10-08", today=today) == "近五年"
    assert _determine_period("20240801", "20261008", today=today) == "近五年"
    # An archived short range cannot be served by a window relative to its end.
    assert _determine_period("2021-01-01", "2021-02-01", today=today) == "近十年"
    with pytest.raises(ValueError):
        _determine_period("2026-10-09", "2026-10-08", today=today)
    with pytest.raises(ValueError):
        _determine_period("invalid", "2026-10-08", today=today)


@pytest.mark.parametrize("requested,hour,is_open", [
    ("2026-09-30", 16, True), ("2026-10-08", 14, True), ("2026-10-08", 16, False),
])
def test_capture_time_cannot_establish_historical_or_unclosed_date(requested, hour, is_open):
    captured = datetime(2026, 10, 8, hour, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(ValueError):
        snapshot_capture_date(requested, captured, is_open)


async def test_historical_batch_request_rejected_before_source_or_database_io():
    with pytest.raises(DataNotFoundError, match="historical"):
        await fetch_daily_indicators_batch("1900-01-01")


def test_capture_snapshot_is_explicitly_unverified_and_dynamic_pe_is_not_ttm():
    captured = datetime(2026, 10, 8, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
    day = snapshot_capture_date("20261008", captured, True)
    doc = AKShareCNAdapter().adapt_daily_indicators(pd.DataFrame([{
        "symbol": "000001", "trade_date": day, "pe_dynamic": 10.0,
        "pb": 1.5, "volume_ratio": 0.0, "_mv_unit": "元",
        "_indicator_method": "eastmoney_spot_capture", "_date_verified": False,
    }]))[0].to_db_doc()
    assert "pe_ttm" not in doc and doc["pe_dynamic"] == 10.0
    assert doc["volume_ratio"] == 0.0  # Valid zero is not mistaken for absence.
    assert doc["data_quality"]["field_status"]["pb"] == "unverified_date"
    assert doc["data_quality"]["field_status"]["volume_ratio"] == "unverified_date"
    assert doc["data_quality"]["field_status"]["dividend_yield"] == "unsupported"


def test_history_merge_keeps_pb_only_actual_dates_and_does_not_relabel_them():
    merged = merge_indicator_history_frames({
        "total_mv": None, "pe_ttm": None,
        "pb": pd.DataFrame({"date": ["2026-09-29", "2026-09-30", "2026-09-30", "bad"],
                            "pb": [1.1, 1.2, 1.3, 9.9]}),
        "turnover_rate": pd.DataFrame({"date": ["2026-10-08"], "turnover_rate": [0.0]}),
    }, "000001", "2026-09-30", "2026-10-08")
    assert merged["date"].tolist() == ["2026-09-30", "2026-10-08"]
    docs = AKShareCNAdapter().adapt_daily_indicators(merged)
    assert docs[0].pb == 1.3 and docs[1].turnover_rate == 0.0
    assert docs[0].field_provenance["pb"]["date_verified"] is True
    assert docs[0].data_quality["field_status"]["pb"] == "available"
    assert docs[0].data_quality["field_status"]["pe_ttm"] == "missing"


def test_confirmed_history_pe_keeps_ttm_basis_and_yields_partial_coverage():
    frame = merge_indicator_history_frames({
        "pe_ttm": pd.DataFrame({"date": ["2026-10-08"], "pe_ttm": [12.0]}),
        "pb": pd.DataFrame({"date": ["2026-10-08"], "pb": [1.5]}),
    }, "000001", "2026-10-08", "2026-10-08")
    doc = AKShareCNAdapter().adapt_daily_indicators(frame)[0].to_db_doc()
    assert doc["pe_ttm"] == 12.0 and doc["field_provenance"]["pe_ttm"]["basis"] == "ttm"
    assert doc["data_quality"]["available_fields"] == ["pe_ttm", "pb"]
    assert doc["data_quality"]["status"] == "partial"
    assert doc["data_quality"]["field_status"]["volume_ratio"] == "unsupported"


def test_tencent_observed_date_is_used_but_opaque_pe_does_not_become_ttm():
    doc = TencentCNAdapter().adapt_daily_indicators(pd.DataFrame([{
        "symbol": "000001", "update_time": "20261008150000", "pb": 1.8,
        "pe_reported": 9.5, "volume_ratio": 5, "dividend_yield": 8,
    }]))[0].to_db_doc()
    assert doc["trade_date"] == "2026-10-08" and doc["pe_reported"] == 9.5
    assert "pe_ttm" not in doc and "volume_ratio" not in doc and "dividend_yield" not in doc
    assert doc["field_provenance"]["pb"]["date_verified"] is True
    assert doc["data_quality"]["field_status"]["pb"] == "available"
    assert doc["data_quality"]["field_status"]["volume_ratio"] == "unsupported"


def test_field_quality_does_not_certify_nonfinite_or_mislabeled_dividend():
    provenance = {
        "pe_ttm": {"date_verified": True, "basis": "ttm"},
        "dividend_yield": {"date_verified": True, "basis": "ttm"},
        "dividend_yield_ttm": {"date_verified": True, "basis": "ttm"},
    }
    quality = indicator_quality({"pe_ttm": float("inf"), "pb": True,
                                 "dividend_yield": 3.0, "dividend_yield_ttm": 3.0}, provenance)
    assert quality["field_status"]["pe_ttm"] == "missing"
    assert quality["field_status"]["pb"] == "missing"
    assert quality["field_status"]["dividend_yield"] == "unverified_basis"
    assert quality["field_status"]["dividend_yield_ttm"] == "available"


@pytest.fixture
async def indicator_symbol(real_mongo_db):
    symbol = "TESTI" + uuid4().hex[:12]
    try:
        yield real_mongo_db, symbol
    finally:
        await real_mongo_db.stock_daily_indicators.delete_many({"symbol": symbol})


def _provenance(source, field, *, date_verified=True, day="2026-10-08"):
    return {field: {"data_source": source, "as_of": day, "method": "contract_test",
                    "date_verified": date_verified, "basis": "ttm" if field == "pe_ttm" else "vendor_pb"}}


async def test_real_mongo_sparse_merge_preserves_verified_fields_and_their_sources(indicator_symbol):
    db, symbol = indicator_symbol
    repo = DailyIndicatorsRepo()
    key = {"symbol": symbol, "trade_date": "2026-10-08", "market": "CN"}
    await repo.upsert_many([{**key, "data_source": "akshare", "pe_ttm": 12.0, "pb": 1.5,
        "field_provenance": {**_provenance("akshare", "pe_ttm"), **_provenance("akshare", "pb")}}], "CN")
    await repo.upsert_many([{**key, "data_source": "tencent", "pb": 1.8, "pe_reported": 9.5,
        "field_provenance": _provenance("tencent", "pb")}], "CN")
    await repo.upsert_many([{**key, "data_source": "akshare", "pb": 99.0,
        "volume_ratio": 2.0, "pe_ttm": None,
        "field_provenance": {**_provenance("akshare", "pb", date_verified=False),
                             **_provenance("akshare", "volume_ratio", date_verified=False)}}], "CN")
    result = await DataInterface.get_instance().screen("CN", "daily_indicators", filters={"symbol": symbol}, limit=0)
    assert result["total"] == 1
    row = result["items"][0]
    assert row["pe_ttm"] == 12.0 and row["pb"] == 1.8 and row["volume_ratio"] == 2.0
    assert row["field_provenance"]["pe_ttm"]["data_source"] == "akshare"
    assert row["field_provenance"]["pb"]["data_source"] == "tencent"
    assert row["data_quality"]["field_status"]["pe_ttm"] == "available"
    assert row["data_quality"]["field_status"]["volume_ratio"] == "unverified_date"
    assert row["data_quality"]["status"] == "partial"
    await repo.upsert_many([{**key, "data_source": "tencent", "pb": float("nan"),
                            "field_provenance": _provenance("tencent", "pb")}], "CN")
    assert (await db.stock_daily_indicators.find_one({"symbol": symbol}))["pb"] == 1.8


async def test_real_mongo_legacy_value_and_wrong_asof_are_not_certified(indicator_symbol):
    db, symbol = indicator_symbol
    await db.stock_daily_indicators.insert_one({
        "symbol": symbol, "trade_date": "2026-10-08", "pe_ttm": 8.0, "data_source": "old"})
    await DailyIndicatorsRepo().upsert_many([{
        "symbol": symbol, "trade_date": "2026-10-08", "market": "CN", "data_source": "tencent",
        "pb": 1.5, "field_provenance": _provenance("tencent", "pb", day="2026-09-30"),
    }], "CN")
    row = await db.stock_daily_indicators.find_one({"symbol": symbol})
    assert row["pe_ttm"] == 8.0
    assert row["data_quality"]["field_status"]["pe_ttm"] == "unverified_date"
    assert row["data_quality"]["field_status"]["pb"] == "unverified_date"
    assert row["data_quality"]["available_fields"] == []


async def test_real_mongo_concurrent_independent_fields_do_not_erase_each_other(indicator_symbol):
    db, symbol = indicator_symbol
    key = {"symbol": symbol, "trade_date": "2026-10-08", "market": "CN"}
    await db.stock_daily_indicators.insert_one({**key, "data_source": "seed"})
    repo = DailyIndicatorsRepo()
    await asyncio.gather(
        repo.upsert_many([{**key, "data_source": "akshare", "pe_ttm": 12.0,
                          "field_provenance": _provenance("akshare", "pe_ttm")}], "CN"),
        repo.upsert_many([{**key, "data_source": "tencent", "pb": 1.8,
                          "field_provenance": _provenance("tencent", "pb")}], "CN"),
    )
    row = await db.stock_daily_indicators.find_one({"symbol": symbol})
    assert row["pe_ttm"] == 12.0 and row["pb"] == 1.8
    assert row["field_provenance"]["pe_ttm"]["data_source"] == "akshare"
    assert row["field_provenance"]["pb"]["data_source"] == "tencent"
    assert set(row["data_quality"]["available_fields"]) == {"pe_ttm", "pb"}


def _public_dividend_tables():
    # Public 000001 rows/header captured with ONE real GET on 2026-10-09.
    # These are deterministic parser inputs, not a substituted client/server.
    dividend_columns = pd.MultiIndex.from_tuples([
        ("分红", "公告日期", "公告日期"),
        ("分红", "分红方案(每10股)", "送股(股)"),
        ("分红", "分红方案(每10股)", "转增(股)"),
        ("分红", "分红方案(每10股)", "派息(税前)(元)"),
        ("分红", "进度", "进度"),
        ("分红", "除权除息日", "除权除息日"),
    ])
    rights_columns = pd.MultiIndex.from_tuples([
        ("配股", "公告日期"), ("配股", "配股方案(每10股配股股数)"), ("配股", "除权日"),
    ])
    return [pd.DataFrame([
        ["2026-09-17", 0, 0, 2.49, "实施", "2026-09-24"],
        ["2026-06-05", 0, 0, 3.60, "实施", "2026-06-12"],
        ["2025-09-30", 0, 0, 2.36, "实施", "2025-10-15"],
        ["2025-06-05", 0, 0, 3.62, "实施", "2025-06-12"],
    ], columns=dividend_columns), pd.DataFrame([
        ["2000-10-23", 3, "2000-11-06"],
    ], columns=rights_columns)]


def _cash_history():
    return parse_cash_dividend_tables(_public_dividend_tables())


def _cash_yield(history=None, **overrides):
    arguments = {"as_of": "2026-10-08", "close": 10.0,
                 "close_date": "2026-10-08", "close_basis": "unadjusted"}
    arguments.update(overrides)
    return derive_cash_dividend_yield(history or _cash_history(), **arguments)


def test_real_public_cash_headers_and_rows_derive_ex_date_not_announcement_window():
    result = _cash_yield()
    assert result["value"] == pytest.approx(8.45)
    assert result["provenance"]["cash_per_share"] == pytest.approx(0.845)
    assert result["provenance"]["event_count"] == 3
    assert result["provenance"]["window_start_exclusive"] == "2025-10-08"
    assert result["provenance"]["method"] == "derived_cash_dividend_ex_date_12m"
    assert result["provenance"]["cash_basis"] == "pre_tax"
    assert result["provenance"]["denominator_close"] == 10.0
    assert result["provenance"]["denominator_date"] == "2026-10-08"


def test_dividend_parser_rejects_changed_unit_and_missing_rights_table():
    tables = _public_dividend_tables()
    with pytest.raises(ValueError, match="schema or units"):
        parse_cash_dividend_tables(tables[:1])
    tables[0].columns = pd.MultiIndex.from_tuples([
        (a, b.replace("每10股", "每股"), c) for a, b, c in tables[0].columns
    ])
    with pytest.raises(ValueError, match="schema or units"):
        parse_cash_dividend_tables(tables)


@pytest.mark.parametrize("overrides", [
    {"close": None}, {"close": 0}, {"close": float("nan")}, {"close": float("inf")},
    {"close": True}, {"close_date": "2026-09-30"}, {"close_basis": "qfq"},
])
def test_dividend_yield_requires_finite_same_date_unadjusted_close(overrides):
    result = _cash_yield(**overrides)
    assert result["value"] is None and result["reason"] == "unverified_price"


@pytest.mark.parametrize("change,reason", [
    ({"bonus_per_ten": 1}, "share_basis_changed"),
    ({"transfer_per_ten": 1}, "share_basis_changed"),
    ({"cash_per_ten": None}, "incomplete_implemented_event"),
    ({"transfer_per_ten": "--"}, "incomplete_implemented_event"),
    ({"announce_date": "2026-10-09"}, "incomplete_implemented_event"),
    ({"ex_date": "--"}, "undated_implemented_event"),
    ({"status": "未知"}, "unknown_implementation_status"),
])
def test_dividend_history_fails_closed_on_unverified_events(change, reason):
    original = _cash_history()
    events = [dict(row) for row in original.dividends]
    events[0].update(change)
    result = _cash_yield(CashDividendHistory(events, original.rights, schema_verified=True))
    assert result["value"] is None and result["reason"] == reason


def test_dividend_history_requires_prior_window_boundary_and_stable_share_basis():
    history = _cash_history()
    assert _cash_yield(CashDividendHistory(history.dividends[:3], [], schema_verified=True))[
        "reason"] == "history_does_not_cover_window"
    assert _cash_yield(CashDividendHistory(history.dividends, [], schema_verified=False))[
        "reason"] == "unverified_history_schema"
    for ex_date, reason in [("2026-06-01", "share_basis_changed"), ("--", "undated_rights_event")]:
        result = _cash_yield(CashDividendHistory(history.dividends, [{"ex_date": ex_date}], schema_verified=True))
        assert result["value"] is None and result["reason"] == reason


def test_dividend_exact_duplicates_count_once_but_conflicts_are_not_summed():
    history = _cash_history()
    duplicate = dict(history.dividends[0])
    rows = [*history.dividends, duplicate]
    assert _cash_yield(CashDividendHistory(rows, [], schema_verified=True))["value"] == pytest.approx(8.45)
    duplicate["cash_per_ten"] = 4.0
    assert _cash_yield(CashDividendHistory(rows, [], schema_verified=True))[
        "reason"] == "conflicting_same_day_event"


def test_cash_window_excludes_cutoff_and_future_and_uses_calendar_year_for_leap_day():
    history = _cash_history()
    # On 2026-09-24 the dividend becoming ex-dividend that day is included.
    assert _cash_yield(as_of="2026-09-24", close_date="2026-09-24")["value"] == pytest.approx(8.45)
    assert _cash_yield(as_of="2026-09-23", close_date="2026-09-23")["value"] == pytest.approx(5.96)
    # By 2026-10-15 the 2025-10-15 payment is outside the left-open window.
    assert _cash_yield(as_of="2026-10-15", close_date="2026-10-15")["value"] == pytest.approx(6.09)
    older = CashDividendHistory([
        {"announce_date": "2022-01-01", "ex_date": "2022-02-01", "status": "实施",
         "cash_per_ten": 1, "bonus_per_ten": 0, "transfer_per_ten": 0},
    ], [], schema_verified=True)
    result = _cash_yield(older, as_of="2024-02-29", close_date="2024-02-29")
    assert result["value"] == 0 and result["provenance"]["window_start_exclusive"] == "2023-02-28"
    assert _cash_yield(history, as_of="0001-01-01", close_date="0001-01-01")["value"] is None


def test_dividend_enrichment_preserves_each_indicator_date_and_never_carries_price():
    merged = merge_indicator_history_frames({
        "pb": pd.DataFrame({"date": ["2026-09-30", "2026-10-08"], "pb": [1.1, 1.2]}),
        "turnover_rate": pd.DataFrame({"date": ["2026-10-08"], "turnover_rate": [0.5],
                                       "_unadjusted_close": [10.0], "_quote_method": ["eastmoney_daily_history"]}),
    }, "000001", "2026-09-30", "2026-10-08")
    enriched = attach_cash_dividend_yields(merged, _cash_history())
    docs = AKShareCNAdapter().adapt_daily_indicators(enriched)
    assert docs[0].trade_date == "2026-09-30" and docs[0].dividend_yield_ttm is None
    assert docs[0].data_quality["dividend_ttm"]["reason"] == "unverified_price"
    assert docs[1].trade_date == "2026-10-08" and docs[1].dividend_yield_ttm == pytest.approx(8.45)
    assert docs[1].field_provenance["dividend_yield_ttm"]["as_of"] == "2026-10-08"
    assert docs[1].dividend_yield is None  # Never alias a derived trailing yield as annual.
    unavailable = AKShareCNAdapter().adapt_daily_indicators(attach_cash_dividend_yields(merged, None))
    assert all(doc.dividend_yield_ttm is None for doc in unavailable)
    assert unavailable[1].data_quality["dividend_ttm"]["reason"] == "dividend_source_unavailable"


async def test_real_mongo_keeps_derived_dividend_provenance_during_sparse_merge(indicator_symbol):
    db, symbol = indicator_symbol
    frame = merge_indicator_history_frames({
        "turnover_rate": pd.DataFrame({"date": ["2026-10-08"], "turnover_rate": [0.5],
                                       "_unadjusted_close": [10.0], "_quote_method": ["eastmoney_daily_history"]}),
    }, symbol)
    doc = AKShareCNAdapter().adapt_daily_indicators(attach_cash_dividend_yields(frame, _cash_history()))[0].to_db_doc()
    repo = DailyIndicatorsRepo()
    await repo.upsert_many([doc], "CN")
    await repo.upsert_many([{"symbol": symbol, "market": "CN", "trade_date": "2026-10-08",
                            "data_source": "tencent", "pb": 1.8,
                            "field_provenance": _provenance("tencent", "pb")}], "CN")
    row = await db.stock_daily_indicators.find_one({"symbol": symbol})
    assert row["dividend_yield_ttm"] == pytest.approx(8.45)
    assert row["field_provenance"]["dividend_yield_ttm"]["cash_per_share"] == pytest.approx(0.845)
    assert row["data_quality"]["dividend_ttm"]["status"] == "available"
    assert row["data_quality"]["field_status"]["dividend_yield_ttm"] == "available"


@pytest.mark.parametrize("source_api,raw", [
    ("stock_zh_a_hist", {"日期": "2026-10-08", "收盘": 10.0}),
    ("stock_zh_a_daily", {"date": "2026-10-08", "close": 10.0}),
])
def test_raw_quote_route_keeps_dividend_close_when_turnover_is_missing(source_api, raw):
    frame = bind_daily_quote_source(pd.DataFrame([raw]), source_api=source_api)
    frame["symbol"] = "000001"
    frame["price_basis"] = "raw"
    quotes = quote_history_indicator_frame(frame)
    assert quotes["turnover_rate"].isna().all()
    assert quotes["_unadjusted_close"].tolist() == [10.0]
    merged = merge_indicator_history_frames({"turnover_rate": quotes}, "000001")
    doc = AKShareCNAdapter().adapt_daily_indicators(attach_cash_dividend_yields(merged, _cash_history()))[0]
    assert doc.dividend_yield_ttm == pytest.approx(8.45)
    expected = "sina_daily_history" if source_api == "stock_zh_a_daily" else "eastmoney_daily_history"
    assert doc.field_provenance["dividend_yield_ttm"]["denominator_source"] == expected


def test_sina_raw_quote_route_keeps_real_turnover_and_rejects_adjusted_dividend_close():
    frame = bind_daily_quote_source(pd.DataFrame([{
        "date": "2026-10-08", "close": 10.0, "turnover": 0.05,
    }]), source_api="stock_zh_a_daily").assign(symbol="000001", price_basis="raw")
    quotes = quote_history_indicator_frame(frame)
    merged = merge_indicator_history_frames({"turnover_rate": quotes}, "000001")
    doc = AKShareCNAdapter().adapt_daily_indicators(merged)[0]
    assert doc.turnover_rate == 5.0
    assert doc.field_provenance["turnover_rate"]["method"] == "sina_daily_history"
    assert doc.field_provenance["turnover_rate"]["date_verified"] is True
    adjusted = quote_history_indicator_frame(frame.assign(price_basis="qfq"))
    assert adjusted["_unadjusted_close"].isna().all()
    unknown = quote_history_indicator_frame(frame.drop(columns=["quote_api", "quote_source", "quote_units_verified"]))
    assert unknown["_unadjusted_close"].isna().all()


@pytest.mark.parametrize("bad", [True, False, np.bool_(True), np.bool_(False)])
def test_indicator_adapter_rejects_boolean_numbers_before_conversion_even_with_evidence(bad):
    evidence = _cash_yield()["provenance"]
    doc = AKShareCNAdapter().adapt_daily_indicators(pd.DataFrame([{
        "symbol": "000001", "date": "2026-10-08", "pe_ttm": bad, "pb": bad,
        "volume_ratio": bad, "total_mv": bad, "circ_mv": bad, "turnover_rate": bad,
        "dividend_yield_ttm": bad, "_dividend_ttm_provenance": evidence,
        "_indicator_method": "baidu_valuation_history", "_date_verified": True,
        "_quote_method": "sina_daily_history",
    }]))[0]
    for field in ("pe_ttm", "pb", "volume_ratio", "total_mv", "circ_mv", "turnover_rate", "dividend_yield_ttm"):
        assert getattr(doc, field) is None
    assert doc.data_quality["available_fields"] == []


async def test_real_mongo_boolean_adapter_input_cannot_overwrite_verified_numbers(indicator_symbol):
    db, symbol = indicator_symbol
    repo = DailyIndicatorsRepo()
    key = {"symbol": symbol, "trade_date": "2026-10-08", "market": "CN", "data_source": "akshare"}
    await repo.upsert_many([{**key, "pe_ttm": 12.0, "pb": 1.5,
                            "field_provenance": {**_provenance("akshare", "pe_ttm"), **_provenance("akshare", "pb")}}], "CN")
    bad = AKShareCNAdapter().adapt_daily_indicators(pd.DataFrame([{
        **key, "pe_ttm": True, "pb": np.bool_(False), "total_mv": True,
        "_indicator_method": "baidu_valuation_history", "_date_verified": True,
    }]))[0].to_db_doc()
    await repo.upsert_many([bad], "CN")
    row = await db.stock_daily_indicators.find_one({"symbol": symbol})
    assert row["pe_ttm"] == 12.0 and row["pb"] == 1.5
    assert row["data_quality"]["field_status"]["pe_ttm"] == "available"


@pytest.mark.parametrize("bad", [True, np.bool_(False)])
def test_tencent_indicator_adapter_also_rejects_boolean_numbers(bad):
    doc = TencentCNAdapter().adapt_daily_indicators(pd.DataFrame([{
        "symbol": "000001", "update_time": "20261008150000", "pb": bad,
        "pe_reported": bad, "total_mv": bad, "circ_mv": bad, "turnover_rate": bad,
    }]))[0]
    assert doc.pb is None and doc.pe_reported is None and doc.total_mv is None
    assert doc.circ_mv is None and doc.turnover_rate is None
    assert doc.field_provenance == {}
