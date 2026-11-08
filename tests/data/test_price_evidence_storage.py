"""Real Mongo follow-up writes must not inherit another price observation's proof."""

from uuid import uuid4

import pandas as pd
import pytest

from app.data.core.interface import DataInterface
from app.data.processor.adjustment_contract import align_price_adjustments
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.repositories.adj_factors_repo import (
    AdjFactorsRepo,
    CN_ADJUSTMENT_EVIDENCE_FIELDS,
)
from app.data.storage.mongo.repositories.daily_quotes_repo import (
    DailyQuotesRepo,
    CN_QUOTE_EVIDENCE_FIELDS,
    trusted_raw_quote,
)


@pytest.fixture
async def price_symbol(real_mongo_db):
    symbol = "TESTPRICE" + uuid4().hex[:12]
    try:
        yield real_mongo_db, symbol
    finally:
        for domain in ("daily_quotes", "adj_factors"):
            for market in ("CN", "HK"):
                await real_mongo_db[get_collection_name(domain, market)].delete_many({"symbol": symbol})


def _quote(symbol, day="2026-10-08", **overrides):
    return {
        "symbol": symbol, "trade_date": day, "period": "daily", "market": "CN",
        "data_source": "akshare", "close": 10.0, "volume": 1000.0,
        "price_basis": "qfq", "adjustment_contract": "cn_adjusted_quotes_v1",
        "adjustment_as_of": "2026-10-08", "updated_at": "2026-10-08T08:00:00Z",
        "volume_unit": "shares", "amount_unit": "CNY", "unit_conversion": "declared_test_units",
        "field_provenance": {"close": {"verified": True}}, "data_quality": {"status": "complete"},
        **overrides,
    }


def _event(symbol, day="2026-09-01", series="snapshot-a", **overrides):
    return {
        "symbol": symbol, "trade_date": day, "market": "CN", "data_source": "akshare",
        "adj_factor": 0.5, "fore_adj_factor": 0.5, "back_adj_factor": 17.0,
        "raw_factor": 4.0, "adjustment_basis": "raw_price_multiplier",
        "adjustment_contract": "cn_price_multiplier_v1", "adjustment_method": "akshare_qfq_reciprocal",
        "adjustment_series_id": series, "record_type": "event", "coverage_end": "2026-10-08",
        "updated_at": "2026-10-08T08:00:00Z", "volume_unit": "shares", "amount_unit": "CNY",
        "unit_conversion": "old_units", "field_provenance": {"adj_factor": {"verified": True}},
        "data_quality": {"status": "complete"}, **overrides,
    }


async def test_cn_legacy_quote_update_revokes_all_absent_evidence_without_deleting_data(price_symbol):
    db, symbol = price_symbol
    repo = DailyQuotesRepo()
    await repo.upsert_many([_quote(symbol)], "CN")
    await repo.upsert_many([{"symbol": symbol, "trade_date": "2026-10-08", "period": "daily",
                            "close": 20.0}], "CN")
    row = await db.stock_daily_quotes.find_one({"symbol": symbol})
    assert row["close"] == 20.0 and row["volume"] == 1000.0
    assert all(row[field] is None for field in CN_QUOTE_EVIDENCE_FIELDS)
    adjusted = align_price_adjustments(pd.DataFrame([row]), pd.DataFrame(), as_of="2026-10-08")
    assert adjusted["close_adj"].isna().all()
    assert adjusted["_adjustment_status"].tolist() == ["unknown_quote_basis"]


async def test_raw_quote_replacement_cannot_keep_qfq_horizon_or_touch_other_dates(price_symbol):
    db, symbol = price_symbol
    repo = DailyQuotesRepo()
    previous = _quote(symbol, "2026-09-30")
    await repo.upsert_many([previous, _quote(symbol)], "CN")
    before = await db.stock_daily_quotes.find_one({"symbol": symbol, "trade_date": "2026-09-30"})
    await repo.upsert_many([{"symbol": symbol, "trade_date": "2026-10-08", "period": "daily",
                            "data_source": "jince", "close": 11.0, "price_basis": "raw",
                            "adjustment_as_of": "2026-09-30", "adjustment_contract": "cn_adjusted_quotes_v1"}], "CN")
    row = await db.stock_daily_quotes.find_one({"symbol": symbol, "trade_date": "2026-10-08"})
    assert row["price_basis"] == "raw" and row["data_source"] == "jince"
    assert row["adjustment_as_of"] is None and row["adjustment_contract"] is None
    assert row["volume_unit"] is None and row["amount_unit"] is None
    assert await db.stock_daily_quotes.count_documents({"symbol": symbol}) == 2
    assert await db.stock_daily_quotes.find_one({"_id": before["_id"]}) == before


async def test_verified_quote_evidence_is_preserved_only_when_supplied_by_new_observation(price_symbol):
    db, symbol = price_symbol
    repo = DailyQuotesRepo()
    await repo.upsert_many([_quote(symbol)], "CN")
    await repo.upsert_many([_quote(symbol, close=12.0, adjustment_as_of="2026-10-09")], "CN")
    result = await DataInterface.get_instance().screen("CN", "daily_quotes", filters={"symbol": symbol}, limit=0)
    row = result["items"][0]
    assert row["adjustment_as_of"] == "2026-10-09" and row["volume_unit"] == "shares"
    assert row["adjustment_contract"] == "cn_adjusted_quotes_v1"
    await repo.upsert_many([{"symbol": symbol, "trade_date": "2026-10-08", "period": "daily",
                            "data_source": "akshare", "close": 13.0, "price_basis": "qfq"}], "CN")
    row = await db.stock_daily_quotes.find_one({"symbol": symbol})
    assert row["price_basis"] == "qfq" and row["adjustment_contract"] is None
    assert row["adjustment_as_of"] is None


async def test_cn_legacy_factor_update_revokes_proof_and_absent_auxiliary_values(price_symbol):
    db, symbol = price_symbol
    repo = AdjFactorsRepo()
    await repo.upsert_many([_event(symbol)], "CN")
    await repo.upsert_many([{"symbol": symbol, "trade_date": "2026-09-01", "adj_factor": 7.0}], "CN")
    row = await db.stock_adj_factors.find_one({"symbol": symbol})
    assert row["adj_factor"] == 7.0
    assert all(row[field] is None for field in CN_ADJUSTMENT_EVIDENCE_FIELDS if field != "adj_factor")
    quotes = pd.DataFrame([_quote(symbol, price_basis="raw")])
    adjusted = align_price_adjustments(quotes, pd.DataFrame([row]), as_of="2026-10-08")
    assert adjusted["close_adj"].isna().all()


async def test_cn_new_factor_source_cannot_inherit_old_auxiliary_factor_or_unit_proof(price_symbol):
    db, symbol = price_symbol
    repo = AdjFactorsRepo()
    await repo.upsert_many([_event(symbol)], "CN")
    new = _event(symbol, series="snapshot-b", data_source="baostock", adjustment_method="baostock_fore_multiplier",
                 adj_factor=1.0, fore_adj_factor=1.0)
    for field in ("back_adj_factor", "raw_factor", "volume_unit", "amount_unit", "unit_conversion", "field_provenance", "data_quality"):
        new.pop(field)
    await repo.upsert_many([new], "CN")
    row = await db.stock_adj_factors.find_one({"symbol": symbol})
    assert row["data_source"] == "baostock" and row["adjustment_series_id"] == "snapshot-b"
    assert row["back_adj_factor"] is None and row["raw_factor"] is None and row["amount_unit"] is None


async def test_uncovered_old_event_rejects_new_snapshot_before_any_write_and_keeps_history(price_symbol):
    db, symbol = price_symbol
    repo = AdjFactorsRepo()
    await repo.upsert_many([_event(symbol, day) for day in ("2026-09-01", "2026-09-10", "2026-09-29")], "CN")
    before = await db.stock_adj_factors.find({"symbol": symbol}).sort("trade_date", 1).to_list(None)
    with pytest.raises(ValueError, match="uncovered existing event"):
        await repo.upsert_many([_event(symbol, day, "snapshot-b") for day in ("2026-09-01", "2026-09-29")], "CN")
    after = await db.stock_adj_factors.find({"symbol": symbol}).sort("trade_date", 1).to_list(None)
    assert after == before


async def test_complete_covered_event_update_preserves_out_of_range_history(price_symbol):
    db, symbol = price_symbol
    repo = AdjFactorsRepo()
    await repo.upsert_many([_event(symbol, day) for day in ("2025-01-01", "2026-09-01", "2026-09-29")], "CN")
    old = await db.stock_adj_factors.find_one({"symbol": symbol, "trade_date": "2025-01-01"})
    await repo.upsert_many([_event(symbol, day, "snapshot-b") for day in ("2026-09-01", "2026-09-29")], "CN")
    assert await db.stock_adj_factors.find_one({"_id": old["_id"]}) == old
    assert await db.stock_adj_factors.count_documents({"symbol": symbol}) == 3
    current = await repo.get_by_symbol_and_range(symbol, "CN", "2026-09-01", "2026-10-08")
    assert {row["adjustment_series_id"] for row in current} == {"snapshot-b"}


async def test_mixed_snapshot_batch_is_rejected_before_any_mongo_write(price_symbol):
    db, symbol = price_symbol
    with pytest.raises(ValueError, match="mixed adjustment snapshot"):
        await AdjFactorsRepo().upsert_many([
            _event(symbol, "2026-09-01"), _event(symbol, "2026-09-29", "snapshot-b")], "CN")
    assert await db.stock_adj_factors.count_documents({"symbol": symbol}) == 0


async def test_non_cn_sparse_writes_keep_existing_contract_behavior(price_symbol):
    db, symbol = price_symbol
    quote_repo, factor_repo = DailyQuotesRepo(), AdjFactorsRepo()
    await quote_repo.upsert_many([_quote(symbol, market="HK")], "HK")
    await factor_repo.upsert_many([_event(symbol, market="HK")], "HK")
    await quote_repo.upsert_many([{"symbol": symbol, "trade_date": "2026-10-08", "period": "daily", "close": 30.0}], "HK")
    await factor_repo.upsert_many([{"symbol": symbol, "trade_date": "2026-09-01", "adj_factor": 2.0}], "HK")
    quote = await db[get_collection_name("daily_quotes", "HK")].find_one({"symbol": symbol})
    factor = await db[get_collection_name("adj_factors", "HK")].find_one({"symbol": symbol})
    assert quote["close"] == 30.0 and quote["price_basis"] == "qfq"
    assert factor["adj_factor"] == 2.0 and factor["adjustment_series_id"] == "snapshot-a"


async def test_cn_range_keeps_predecessor_when_window_contains_no_event(price_symbol):
    db, symbol = price_symbol
    repo = AdjFactorsRepo()
    await repo.upsert_many([_event(symbol, "2026-09-01", adj_factor=1.0)], "CN")
    events = await repo.get_by_symbol_and_range(symbol, "CN", "2026-09-30", "2026-10-08")
    assert [event["trade_date"] for event in events] == ["2026-09-01"]
    quotes = pd.DataFrame([_quote(symbol, price_basis="raw")])
    aligned = align_price_adjustments(quotes, pd.DataFrame(events), as_of="2026-10-08")
    assert aligned["close_adj"].tolist() == [10.0]
    assert aligned["_adjustment_status"].tolist() == ["verified_event_series"]


async def test_cn_range_retains_nearest_unknown_anchor_instead_of_skipping_to_trusted_one(price_symbol):
    db, symbol = price_symbol
    repo = AdjFactorsRepo()
    await repo.upsert_many([_event(symbol, "2026-09-01", adj_factor=1.0)], "CN")
    await repo.upsert_many([{"symbol": symbol, "trade_date": "2026-09-20", "adj_factor": 3.0}], "CN")
    events = await repo.get_by_symbol_and_range(symbol, "CN", "2026-09-30", "2026-10-08")
    assert [event["trade_date"] for event in events] == ["2026-09-20"]
    aligned = align_price_adjustments(pd.DataFrame([_quote(symbol, price_basis="raw")]),
                                     pd.DataFrame(events), as_of="2026-10-08")
    assert aligned["close_adj"].isna().all()


async def test_hk_range_does_not_gain_cn_event_anchor_semantics(price_symbol):
    db, symbol = price_symbol
    repo = AdjFactorsRepo()
    await repo.upsert_many([_event(symbol, "2026-09-01", market="HK")], "HK")
    assert await repo.get_by_symbol_and_range(symbol, "HK", "2026-09-30", "2026-10-08") == []


RAW_CONVERSIONS = [
    ("akshare", "akshare:stock_zh_a_hist/eastmoney;volume:lot100->share*100;amount:CNY->CNY"),
    ("akshare", "akshare:stock_zh_a_daily/sina;volume:share->share;amount:CNY->CNY"),
    ("baostock", "baostock:identity"),
]


@pytest.mark.parametrize("source,conversion", RAW_CONVERSIONS)
@pytest.mark.parametrize("incoming", [
    {"data_source": "duckdb", "close": 999.0},
    {"data_source": "akshare", "close": 999.0, "price_basis": "raw", "volume_unit": "share", "amount_unit": "CNY"},
    {"data_source": "baostock", "close": 999.0, "price_basis": "qfq", "unit_conversion": "baostock:identity"},
])
async def test_unproven_overlap_write_keeps_entire_verified_raw_document(price_symbol, source, conversion, incoming):
    db, symbol = price_symbol
    repo = DailyQuotesRepo()
    original = _quote(symbol, price_basis="raw", volume_unit="share", data_source=source, unit_conversion=conversion)
    assert trusted_raw_quote(original)
    await repo.upsert_many([original], "CN")
    before = await db.stock_daily_quotes.find_one({"symbol": symbol})
    count = await repo.upsert_many([{"symbol": symbol, "trade_date": "2026-10-08", "period": "daily",
        "updated_at": "2026-10-09T09:00:00Z", **incoming}], "CN")
    after = await db.stock_daily_quotes.find_one({"symbol": symbol})
    assert count == 1 and after == before  # rejected values do not borrow the old proof/timestamp


@pytest.mark.parametrize("source,conversion", RAW_CONVERSIONS)
async def test_new_trusted_raw_observation_can_replace_an_existing_raw_bar(price_symbol, source, conversion):
    db, symbol = price_symbol
    repo = DailyQuotesRepo()
    first = _quote(symbol, price_basis="raw", volume_unit="share", data_source="baostock", unit_conversion="baostock:identity")
    await repo.upsert_many([first], "CN")
    next_row = _quote(symbol, price_basis="raw", volume_unit="share", data_source=source,
        unit_conversion=conversion, close=11.0, updated_at="2026-10-09T09:00:00Z")
    await repo.upsert_many([next_row], "CN")
    after = await db.stock_daily_quotes.find_one({"symbol": symbol})
    assert after["close"] == 11.0 and after["data_source"] == source
    assert after["unit_conversion"] == conversion and after["updated_at"] == next_row["updated_at"]
    assert after["adjustment_contract"] is None and after["adjustment_as_of"] is None


@pytest.mark.parametrize("source,conversion", RAW_CONVERSIONS)
@pytest.mark.parametrize("old_source", ["duckdb", "baostock"])
async def test_sparse_trusted_raw_observation_cannot_certify_old_numeric_fields(price_symbol, source, conversion, old_source):
    db, symbol = price_symbol
    repo = DailyQuotesRepo()
    old = {"symbol": symbol, "trade_date": "2026-10-08", "period": "daily", "data_source": old_source,
        "close": 12.0, "open": 11.0, "high": 13.0, "low": 10.0, "volume": 1000.0,
        "amount": 12000.0, "turnover_rate": 2.0}
    await db.stock_daily_quotes.insert_one(old)
    sparse = {"symbol": symbol, "trade_date": "2026-10-08", "period": "daily", "data_source": source,
        "close": 12.0, "price_basis": "raw", "volume_unit": "share", "amount_unit": "CNY", "unit_conversion": conversion}
    await repo.upsert_many([sparse], "CN")
    after = await db.stock_daily_quotes.find_one({"symbol": symbol})
    assert trusted_raw_quote(after) and after["close"] == 12.0
    assert all(after[field] is None for field in ("open", "high", "low", "volume", "amount", "turnover_rate"))
    assert after["_id"] == old["_id"]


async def test_overlap_protection_does_not_certify_a_new_unknown_key(price_symbol):
    db, symbol = price_symbol
    repo = DailyQuotesRepo()
    await repo.upsert_many([_quote(symbol, price_basis="raw", volume_unit="share",
        data_source="baostock", unit_conversion="baostock:identity")], "CN")
    unknown = {"symbol": symbol, "trade_date": "2026-09-30", "period": "daily", "close": 11.0,
        "data_source": "duckdb", "data_quality": {"note": "$literal source text"}}
    await repo.upsert_many([unknown], "CN")
    stored = await db.stock_daily_quotes.find_one({"symbol": symbol, "trade_date": "2026-09-30"})
    assert stored["close"] == 11.0 and stored["data_source"] == "duckdb"
    assert stored["price_basis"] is None and stored["volume_unit"] is None
    assert stored["data_quality"] == unknown["data_quality"]


@pytest.mark.parametrize("changes", [
    {"unit_conversion": "declared_test_units"}, {"volume_unit": "shares"}, {"amount_unit": None},
    {"data_source": "unknown"}, {"close": 0.0}, {"close": -1.0}, {"close": float("inf")},
    {"close": float("nan")}, {"close": True},
])
async def test_claimed_raw_but_unproven_existing_bar_does_not_block_replacement(price_symbol, changes):
    db, symbol = price_symbol
    old = _quote(symbol, **{"price_basis": "raw", "volume_unit": "share", "data_source": "baostock", "unit_conversion": "baostock:identity", **changes})
    assert not trusted_raw_quote(old)
    # Insert the adversarial legacy record directly; the real repository must
    # evaluate the stored contract inside the same atomic update, not pre-read.
    await db.stock_daily_quotes.insert_one(old)
    await DailyQuotesRepo().upsert_many([{"symbol": symbol, "trade_date": "2026-10-08", "period": "daily", "close": 20.0}], "CN")
    stored = await db.stock_daily_quotes.find_one({"symbol": symbol})
    assert stored["close"] == 20.0 and stored["price_basis"] is None
