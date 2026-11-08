"""Period-specific requests preserve evidence boundaries and legacy defaults."""
from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.core.analysis_horizon import get_horizon_profile, normalize_analysis_horizon
from app.engine.orchestrator.agents import _resolve_inject_args
from app.engine.tools.datasources.registry import get_spec_by_id
from app.data.query.valuation_evidence import summarize_valuation_history


@pytest.mark.parametrize("value", ["", "year", "LONG", 1, {}, []])
def test_invalid_horizon_is_not_silently_relabelled_short(value):
    with pytest.raises(ValueError):
        normalize_analysis_horizon(value)


@pytest.mark.parametrize("horizon,daily_days,financial_days,sessions", [
    (None, 45, 1460, 120), ("short", 45, 1460, 120),
    ("medium", 180, 1460, 250), ("long", 365, 1825, 500),
])
def test_registered_preloads_use_requested_period_and_observation_date(horizon, daily_days, financial_days, sessions):
    context = {"ticker": "000503", "trade_date": "2026-10-09", "analysis_horizon": horizon}
    for tool, days in (("daily_quotes", daily_days), ("financial_data", financial_days)):
        args = _resolve_inject_args(get_spec_by_id(tool), context)
        assert args["symbol"] == "000503"
        assert args["end_date"] == "2026-10-09"
        assert (date(2026, 10, 9) - date.fromisoformat(args["start_date"])).days == days
    history = _resolve_inject_args(get_spec_by_id("valuation_history"), context)
    assert history["sessions"] == sessions
    for tool in ("technical_analysis", "fundamentals"):
        assert _resolve_inject_args(get_spec_by_id(tool), context)["analysis_horizon"] == (horizon or "short")


def test_long_valuation_never_calls_a_short_sample_a_complete_history():
    # Input dates are explicit calendar observations; no weekday calendar is inferred.
    dates = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(500)]
    rows = [{"trade_date": day, "pb": 2.0, "pe_ttm": 10.0,
             "field_provenance": {field: {"as_of": day, "date_verified": True,
                 "data_source": "baostock", "method": "reported_daily", "basis": basis}
                 for field, basis in (("pb", "vendor_pb"), ("pe_ttm", "ttm"))}}
            for day in dates]
    complete = summarize_valuation_history(rows, dates, sessions=500)
    assert complete["fields"]["pb"]["status"] == "available"
    partial = summarize_valuation_history(rows[-120:], dates, sessions=500)
    assert partial["fields"]["pb"]["observation_count"] == 120
    assert partial["fields"]["pb"]["sample_percentile_pct"] is None
    assert partial["fields"]["pb"]["status"] == "insufficient_history"


@pytest.mark.asyncio
async def test_real_facade_retains_usable_quote_when_long_history_is_partial(real_mongo_db):
    from app.core.config import settings
    from app.data.core.interface import DataInterface
    from app.data.storage.mongo.collections import get_collection_name
    from isolated_database import require_isolated_database
    require_isolated_database()
    from isolated_database import require_isolated_database
    require_isolated_database()
    symbol = "horizon_" + uuid4().hex
    coll = real_mongo_db[get_collection_name("daily_quotes", "CN")]
    DataInterface.reset_instance()
    row = {"symbol": symbol, "market": "CN", "data_source": "baostock", "period": "daily",
        "trade_date": "2026-10-09", "open": 8.45, "high": 8.49, "low": 8.39, "close": 8.43,
        "volume": 8055400.0, "amount": 67987022.0, "price_basis": "raw", "volume_unit": "share",
        "amount_unit": "CNY", "unit_conversion": "baostock:native_share_CNY",
        "updated_at": "2026-10-09T15:30:00+08:00"}
    await coll.insert_one(row)
    calendar = real_mongo_db[get_collection_name("trade_calendar", "CN")]
    calendar_row = await calendar.insert_one({"exchange": "SSE", "market": "CN",
        "cal_date": "2026-10-09", "is_open": True, "data_source": "horizon_contract"})
    try:
        result = await DataInterface.get_instance().technical_snapshot(
            "CN", symbol, "2026-10-09", refresh=False, analysis_horizon="long")
        assert result["status"] == "success"
        assert result["recent_daily"][-1]["close"] == 8.43
        assert result["analysis_horizon"] == "long"
        assert result["history_window_status"] == "partial"
        assert result["requested_calendar_verified"] is False
        assert result["calendar_complete"] is True  # one observed day is not the requested five years
        assert result["indicators"]["ma250"] is None
        assert result["requested_history_start"] == (
            date(2026, 10, 9) - timedelta(days=get_horizon_profile("long").technical_lookback_days)).isoformat()
    finally:
        await coll.delete_many({"symbol": symbol})
        await calendar.delete_one({"_id": calendar_row.inserted_id})
        DataInterface.reset_instance()


@pytest.mark.parametrize("calendar", [
    [{"cal_date": "2026-10-09", "is_open": "unknown"}],
    [{"cal_date": "2026-10-09", "is_open": 1}, {"cal_date": "2026-10-09", "is_open": 0}],
])
def test_unknown_calendar_does_not_erase_an_observed_price(calendar):
    from app.data.query.technical_analysis import build_technical_snapshot
    row = {"trade_date": "2026-10-09", "open": 8.45, "high": 8.49, "low": 8.39,
           "close": 8.43, "volume": 8055400, "amount": 67987022, "data_source": "baostock",
           "price_basis": "raw", "period": "daily"}
    result = build_technical_snapshot([row], calendar, "2026-10-09", ["baostock"], trajectory_days=0)
    assert result["status"] == "success"
    assert result["calendar_complete"] is False
    assert result["invalid_closed_bars"] == []
    assert result["recent_daily"][0]["close"] == 8.43
