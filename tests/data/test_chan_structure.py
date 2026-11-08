"""Native CZSC calculations and immutable snapshots in isolated real Mongo."""

from copy import deepcopy
from datetime import date, timedelta
import asyncio
import sys
from uuid import uuid4

import pytest

from app.engine.chan_structure import calculate, native_core, parts, prepare, preview, replay, _bar
from app.services.chan_structure import ChanStructureService, COLLECTION
from app.data.query.research_tracking import chan_context


def sample():
    # Explicit synthetic oscillations test the geometry, not investment accuracy.
    days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(120)]
    bars = []
    for i, day in enumerate(days):
        p = round(10 + (i % 20 if i % 40 < 20 else 20 - i % 20) * 0.3, 2)
        bars.append(
            dict(date=day, open=p, close=p + 0.1, high=p + 0.2, low=p - 0.2, data_source="synthetic_structure_sample")
        )
    return bars, dict(
        price_basis="raw", currency="CNY", calendar_complete=True, expected_sessions=days, next_session="2026-05-01"
    )


def test_incremental_confirmation_never_uses_later_bars_and_matches_native_batch():
    bars, context = sample()
    native = native_core()
    for n in (20, 25, 43, 60, 80, 100, 120):
        prefix = bars[:n]
        result = calculate("000503", prefix, context, prefix[-1]["date"])
        batch = parts(
            native.CZSC([_bar(native, "000503", b, i) for i, b in enumerate(prefix)], min_bi_len=6, max_bi_num=200)
        )
        for kind in ("fractals", "strokes", "zones"):
            assert [
                {k: v for k, v in x.items() if k not in {"confirmed_at", "updated_at_session"}} for x in result[kind]
            ] == batch[kind]
            for item in result[kind]:
                assert item["updated_at_session"] <= prefix[-1]["date"]
                if item["confirmed_at"]:
                    # A zone keeps its first three-stroke confirmation while
                    # subsequent strokes extend it; geometry has its own time.
                    if kind != "zones":
                        assert item["confirmed_at"] > (item.get("date") or item["end"])
                    assert item["confirmed_at"] <= item["updated_at_session"]
                    first = [b for b in prefix if b["date"] <= item["confirmed_at"]]
                    then = replay("000503", first)
                    assert any(x["id"] == item["id"] and x["status"] == "confirmed" for x in then[kind])
    assert "czsc" not in sys.modules


@pytest.mark.parametrize("case", ["qfq", "mixed", "duplicate", "future", "nan", "bad_ohlc", "calendar", "short"])
def test_invalid_inputs_are_not_drawn(case):
    bars, context = sample()
    as_of = bars[-1]["date"]
    if case == "qfq":
        context["price_basis"] = "qfq"
    if case == "mixed":
        bars[-1]["data_source"] = "other"
    if case == "duplicate":
        bars[-1]["date"] = bars[-2]["date"]
    if case == "future":
        as_of = bars[-2]["date"]
    if case == "nan":
        bars[-1]["high"] = float("nan")
    if case == "bad_ohlc":
        bars[-1]["low"] = 999
    if case == "calendar":
        context["calendar_complete"] = False
    if case == "short":
        bars = bars[-10:]
    with pytest.raises(ValueError):
        prepare("000503", bars, context, as_of)


def test_gap_is_not_bridged_and_stale_date_is_retained():
    bars, context = sample()
    missing = bars.pop(40)["date"]
    result = calculate("000503", bars, context, bars[-1]["date"])
    assert result["status"] == "partial" and result["window_start"] > missing
    result = calculate("000503", bars[:-1], context, bars[-1]["date"])
    assert result["status"] == "stale" and result["as_of"] == bars[-2]["date"]
    calendar = [dict(cal_date=b["date"], is_open=True) for b in bars]
    calendar.append(dict(cal_date=bars[0]["date"], is_open=False))
    assert not chan_context(bars, bars[-1]["date"], calendar)["calendar_complete"]


def test_intraday_preview_is_candidate_only_and_does_not_modify_closed_structure():
    bars, context = sample()
    closed = calculate("000503", bars, context, bars[-1]["date"])
    saved = deepcopy(closed)
    live = dict(
        date="2026-05-01",
        open=10.0,
        close=11.0,
        low=8.0,
        high=12.0,
        status="intraday",
        price_basis="raw",
        currency="CNY",
        observed_at="2026-05-01T10:00:00+08:00",
        data_source="live_sample",
    )
    result = preview("000503", bars, context, bars[-1]["date"], live, closed)
    assert result and result["data_source"] == "live_sample"
    assert all(
        x["status"] == "candidate" and x["confirmed_at"] is None
        for kind in ("fractals", "strokes", "zones")
        for x in result[kind]
    )
    assert closed == saved
    for patch in [dict(stale=True), dict(price_basis="qfq"), dict(date="2026-05-02")]:
        assert preview("000503", bars, context, bars[-1]["date"], {**live, **patch}, closed) is None


@pytest.mark.asyncio
async def test_content_addressed_snapshots_preserve_revisions_and_read_times(real_mongo_db):
    bars, context = sample()
    unique = "validation_" + uuid4().hex
    for b in bars:
        b["data_source"] = unique
    market = dict(bars=bars, chan_context=context, as_of=bars[-1]["date"])
    service = ChanStructureService(real_mongo_db)
    try:
        first, repeated = await asyncio.gather(service.build("000503", market), service.build("000503", market))
        assert first["persisted"] and first["snapshot_id"] == repeated["snapshot_id"]
        assert first["recorded_at"] == repeated["recorded_at"]
        old = await real_mongo_db[COLLECTION].find_one({"_id": first["snapshot_id"]})
        changed = deepcopy(market)
        changed["bars"][-1]["high"] += 0.4
        revised = await service.build("000503", changed)
        assert revised["snapshot_id"] != first["snapshot_id"] and revised["revision_reason"] == "input_revised"
        assert revised["supersedes_snapshot_id"] == first["snapshot_id"]
        assert await real_mongo_db[COLLECTION].find_one({"_id": first["snapshot_id"]}) == old
        assert not old["result"].get("preview")
        assert (await service.build("000503", {**market, "bars": []}))["status"] == "unavailable"
    finally:
        await real_mongo_db[COLLECTION].delete_many({"result.data_source": unique})
