"""Full CN event refresh boundaries; pure functions and real Mongo, no sources."""

from uuid import uuid4
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data.core.refresh_service import DataRefreshService, refresh_fetch_range
from app.data.core.registry.capability import CapabilityRegistry
from app.data.core.registry.priority import PriorityConfig
from app.data.core.adjustment_refresh import assert_cn_adjustment_horizon, cn_adjustment_snapshot_horizon
from app.data.scheduler.jobs.cn import CNAdjFactorsJob, CNDailyQuotesJob
from app.data.processor.adjustment_contract import (
    align_price_adjustments, normalize_adjustment_events,
)
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.repositories.adj_factors_repo import AdjFactorsRepo


def _service():
    return DataRefreshService(CapabilityRegistry(), PriorityConfig())


@pytest.mark.parametrize("requested_start", [None, "2026-02-22", "2026-09-30"])
def test_cn_event_refresh_never_slices_a_complete_snapshot_to_recent_events(requested_start):
    assert refresh_fetch_range("CN", "adj_factors", "2026-02-22", "2026-10-08",
                               requested_start, None) == ("1990-01-01", "2026-10-08")


def test_historical_end_is_not_silently_expanded_by_snapshot_start_policy():
    assert refresh_fetch_range("CN", "adj_factors", "2026-02-22", "2026-10-08",
                               "2026-09-01", "2026-09-30") == ("1990-01-01", "2026-09-30")


async def test_future_horizon_is_rejected_before_database_or_source_io():
    future = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=2)).isoformat()
    with pytest.raises(ValueError, match="future adjustment horizon"):
        await assert_cn_adjustment_horizon("000001", future)


async def test_scheduler_other_domain_preflight_preserves_its_dates_without_db_io():
    assert await CNDailyQuotesJob()._prepare_fetch_range("000001", "2026-09-01", "2026-09-30") == (
        "2026-09-01", "2026-09-30")


@pytest.mark.parametrize(("market", "domain"), [
    ("HK", "adj_factors"), ("US", "adj_factors"),
    ("CN", "daily_quotes"), ("CN", "financial_data"),
])
def test_other_markets_and_domains_keep_existing_requested_range_semantics(market, domain):
    assert refresh_fetch_range(market, domain, "2026-02-22", "2026-10-08",
                               "2026-09-01", "2026-09-30") == ("2026-09-01", "2026-09-30")


def _events(extra=False):
    rows = [
        {"date": "2026-01-01", "qfq_factor": 1.2},
        {"date": "2026-02-01", "qfq_factor": 1.1},
        {"date": "2026-03-01", "qfq_factor": 1.0},
    ]
    if extra:
        rows.append({"date": "2026-10-08", "qfq_factor": 0.9})
    return pd.DataFrame(rows)


def _documents(symbol, end, *, start="1990-01-01", extra=False):
    frame = normalize_adjustment_events(_events(extra), source="akshare", symbol="000001",
                                       start_date=start, end_date=end)
    frame["symbol"] = symbol
    return frame.to_dict("records")


def test_returned_snapshot_horizon_is_not_its_old_latest_event_date():
    records = _documents("000001", "2026-10-08")
    assert max(record["trade_date"] for record in records) == "2026-03-01"
    assert cn_adjustment_snapshot_horizon(records) == "2026-10-08"


@pytest.mark.parametrize("change", [
    {"coverage_end": "2026-09-30"}, {"adjustment_series_id": "other"},
    {"data_source": "baostock"}, {"record_type": None},
])
def test_inconsistent_returned_snapshot_cannot_advance_a_checkpoint(change):
    records = _documents("000001", "2026-10-08")
    records[0].update(change)
    with pytest.raises(ValueError, match="complete normalized snapshot"):
        cn_adjustment_snapshot_horizon(records)


def test_legacy_or_empty_result_has_no_publishable_snapshot_horizon():
    for records in ([], [{"symbol": "000001", "trade_date": "2026-10-08", "adj_factor": 1.0}]):
        with pytest.raises(ValueError):
            cn_adjustment_snapshot_horizon(records)


@pytest.mark.requires_db
@pytest.mark.parametrize("extra", [False, True])
async def test_real_mongo_full_refresh_updates_every_event_horizon_and_snapshot(real_mongo_db, extra):
    symbol = "TESTAJREF" + uuid4().hex
    coll = real_mongo_db[get_collection_name("adj_factors", "CN")]
    repo = AdjFactorsRepo()
    service = _service()
    try:
        await repo.upsert_many(_documents(symbol, "2026-09-30"), "CN")
        # Exercise actual incremental range calculation against the real DB.
        start, _, latest = await service._get_incremental_date_range("CN", symbol, "adj_factors")
        assert start == "1990-01-01" and latest is None
        fetch_start, fetch_end = refresh_fetch_range("CN", "adj_factors", start,
                                                    "2026-10-08", "2026-02-22")
        await service._assert_cn_adjustment_horizon(symbol, fetch_end)
        await repo.upsert_many(_documents(symbol, fetch_end, start=fetch_start, extra=extra), "CN")
        stored = await coll.find({"symbol": symbol}, {"_id": 0}).to_list(length=None)
        assert len(stored) == (4 if extra else 3)
        assert {record["coverage_end"] for record in stored} == {"2026-10-08"}
        assert len({record["adjustment_series_id"] for record in stored}) == 1
        quotes = pd.DataFrame([
            {"symbol": symbol, "trade_date": "2026-01-15", "close": 10.0, "price_basis": "raw"},
            {"symbol": symbol, "trade_date": "2026-10-08", "close": 10.0, "price_basis": "raw"},
        ])
        result = align_price_adjustments(quotes, pd.DataFrame(stored), as_of="2026-10-08")
        assert result["close_adj"].notna().all()
        assert set(result["_adjustment_status"]) == {"verified_event_series"}
    finally:
        await coll.delete_many({"symbol": symbol})


@pytest.mark.requires_db
async def test_real_mongo_historical_refresh_cannot_downgrade_newer_snapshot(real_mongo_db):
    symbol = "TESTAJREF" + uuid4().hex
    coll = real_mongo_db[get_collection_name("adj_factors", "CN")]
    try:
        await AdjFactorsRepo().upsert_many(_documents(symbol, "2026-10-08"), "CN")
        before = await coll.find({"symbol": symbol}).sort("trade_date", 1).to_list(length=None)
        with pytest.raises(ValueError, match="downgrade"):
            await _service()._assert_cn_adjustment_horizon(symbol, "2026-09-30")
        assert await coll.find({"symbol": symbol}).sort("trade_date", 1).to_list(length=None) == before
        await _service()._assert_cn_adjustment_horizon(symbol, "2026-10-08")
    finally:
        await coll.delete_many({"symbol": symbol})


@pytest.mark.requires_db
async def test_real_mongo_legacy_claim_is_not_a_proven_future_horizon(real_mongo_db):
    symbol = "TESTAJREF" + uuid4().hex
    coll = real_mongo_db[get_collection_name("adj_factors", "CN")]
    try:
        await coll.insert_one({"symbol": symbol, "trade_date": "2026-03-01", "adj_factor": 1.0,
                               "data_source": "akshare", "coverage_end": "9999-12-31"})
        await _service()._assert_cn_adjustment_horizon(symbol, "2026-10-08")
    finally:
        await coll.delete_many({"symbol": symbol})


@pytest.mark.requires_db
async def test_real_mongo_scheduler_uses_full_snapshot_and_same_horizon_guard(real_mongo_db):
    symbol = "TESTAJREF" + uuid4().hex
    coll = real_mongo_db[get_collection_name("adj_factors", "CN")]
    job = CNAdjFactorsJob()
    try:
        await AdjFactorsRepo().upsert_many(_documents(symbol, "2026-10-08"), "CN")
        assert await job._prepare_fetch_range(symbol, "2026-10-07", "2026-10-08") == (
            "1990-01-01", "2026-10-08")
        before = await coll.find({"symbol": symbol}).sort("trade_date", 1).to_list(length=None)
        with pytest.raises(ValueError, match="downgrade"):
            await job._prepare_fetch_range(symbol, "2026-09-29", "2026-09-30")
        assert await coll.find({"symbol": symbol}).sort("trade_date", 1).to_list(length=None) == before
    finally:
        await coll.delete_many({"symbol": symbol})
