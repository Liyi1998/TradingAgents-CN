"""Explicit medium/long financial windows retain all disclosed source rows."""

from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.data.core.reader import Reader, _financial_window_limit
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.repositories.financial_data_repo import FinancialDataRepo


def test_short_and_unspecified_requests_keep_legacy_limit():
    assert _financial_window_limit({}, None, None) == 20
    assert _financial_window_limit({}, "2021-01-01", "2026-01-01") == 20
    assert _financial_window_limit({"full_financial_window": False}, "2021-01-01", "2026-01-01") == 20
    assert _financial_window_limit({"full_financial_window": "true"}, "2021-01-01", "2026-01-01") == 20


@pytest.mark.parametrize("start,end", [
    ("2021-01-01", "2026-01-01"), ("2020-02-29", "2026-02-28"),
    ("2020-10-13", "2026-10-13"), ("2026-10-13", "2026-10-13"),
])
def test_complete_financial_reads_require_at_most_six_years(start, end):
    assert _financial_window_limit({"full_financial_window": True}, start, end) == 0


@pytest.mark.parametrize("start,end", [
    (None, "2026-01-01"), ("2021-01-01", None), ("", "2026-01-01"),
    ("2026-01-01", "2021-01-01"), ("2019-12-31", "2026-01-01"),
    ("2020-02-29", "2026-03-01"), ("2026-02-30", "2026-03-01"),
    ("not-a-date", "2026-01-01"), ("9995-01-01", "9999-01-01"),
])
def test_unbounded_or_invalid_complete_windows_are_rejected(start, end):
    with pytest.raises(ValueError, match="财务窗口"):
        _financial_window_limit({"full_financial_window": True}, start, end)


def _disclosed_record(symbol, period, source, statement_type="indicator", announced=None):
    announced = announced or (date.fromisoformat(period) + timedelta(days=30)).isoformat()
    return {
        "symbol": symbol, "market": "CN", "data_source": source,
        "report_period": period, "statement_type": statement_type,
        "financial_contract": "financial_disclosed_fields_v1",
        "announce_date": announced, "revenue": 1000.0,
        "currency": "CNY", "monetary_unit": "元",
        "field_provenance": {"revenue": {
            "data_source": source, "provider_channel": "published_statement",
            "report_period": period, "announce_date": announced,
            "method": "reported", "basis": "consolidated_ytd", "status": "available",
        }},
    }


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_multi_source_quarters_survive_reader_limit_and_disclosure_gate(real_mongo_db):
    symbol = f"HORIZON-{uuid4().hex}"
    collection = real_mongo_db[get_collection_name("financial_data", "CN")]
    periods = [f"{year}-{month_day}" for year in range(2021, 2026)
               for month_day in ("03-31", "06-30", "09-30", "12-31")]
    # The standard repository key is symbol/period/statement_type, not source.
    # Put each source in a distinct natural key, so both sources really survive
    # upsert and the read tests 40 stored documents across 20 actual quarters.
    records = [_disclosed_record(symbol, period, source, kind)
               for period in periods
               for source, kind in (("akshare", "indicator"), ("tushare", "income"))]
    # Neither future disclosure nor unknown disclosure becomes usable merely
    # because the result count is no longer capped at 20.
    records.extend([
        _disclosed_record(symbol, "2026-03-31", "akshare", announced="2026-05-01"),
        {**_disclosed_record(symbol, "2026-03-31", "tushare", "income"), "field_provenance": {}},
        _disclosed_record(symbol, "2020-12-31", "akshare"),
    ])
    try:
        await FinancialDataRepo().upsert_many(records, "CN")
        assert await collection.count_documents({"symbol": symbol}) == 43
        reader = Reader()
        default, _ = await reader.get_data("CN", "financial_data", symbol,
            start_date="2021-01-01", end_date="2026-04-15")
        assert len(default) == 20
        assert len({row["report_period"] for row in default}) == 10
        assert min(row["report_period"] for row in default) > periods[0]

        full, _ = await reader.get_data("CN", "financial_data", symbol,
            start_date="2021-01-01", end_date="2026-04-15",
            filters={"full_financial_window": True})
        assert len(full) == 40
        assert {row["report_period"] for row in full} == set(periods)
        assert {row["data_source"] for row in full} == {"akshare", "tushare"}
        assert {(row["report_period"], row["statement_type"]) for row in full} == {
            (period, kind) for period in periods for kind in ("indicator", "income")
        }
        assert all(row["revenue"] == 1000.0 and row["financial_as_of"] == "2026-04-15" for row in full)

        indicators, _ = await reader.get_data("CN", "financial_data", symbol,
            start_date="2021-01-01", end_date="2026-04-15",
            filters={"full_financial_window": True, "statement_type": "indicator"})
        assert len(indicators) == 20
        assert {row["report_period"] for row in indicators} == set(periods)
        assert {row["statement_type"] for row in indicators} == {"indicator"}

        with pytest.raises(ValueError, match="财务窗口"):
            await reader.get_data("CN", "financial_data", symbol,
                filters={"full_financial_window": True})
    finally:
        await collection.delete_many({"symbol": symbol})
