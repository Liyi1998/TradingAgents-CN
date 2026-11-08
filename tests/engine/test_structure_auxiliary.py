"""Synthetic bars for the structure auxiliary. Not a backtest and not a return claim."""

from copy import deepcopy
from datetime import datetime, timedelta
from fractions import Fraction

import pytest

from app.engine.structure_auxiliary import (
    CN,
    SESSION_ENDS as ENDS,
    TPO_LABEL,
    analyze,
    expand_value_area,
    select_poc,
)

DAY = "2026-11-05"
NEXT = "2026-11-06"
SYMBOL = "000001"


def at(day, clock):
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=CN)


def bar(day, minute, prices, source="tencent"):
    opened, high, low, close = prices
    return {
        "symbol": SYMBOL,
        "datetime": f"{day} {minute // 60:02d}:{minute % 60:02d}:00",
        "freq": "30min",
        "open": opened,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000,
        "ohlc_kind": "native_bar",
        "price_basis": "raw",
        "volume_unit": "share",
        "bar_time_kind": "end",
        "data_source": source,
    }


def session(day, prices):
    return [bar(day, minute, price) for minute, price in zip(ENDS, prices)]


def flat(day, low=10.0, high=10.02):
    return session(day, [(low, high, low, low)] * 8)


def calendar(*days):
    return [{"cal_date": day, "is_open": True} for day in days]


def factors(*pairs):
    return [{"symbol": SYMBOL, "trade_date": day, "adj_factor": factor} for day, factor in pairs]


def run(rows, stamp="15:01:00", day=DAY, **kwargs):
    return analyze(
        SYMBOL,
        rows,
        at(day, stamp),
        calendar=kwargs.pop("calendar", calendar(day)),
        adjustments=kwargs.pop("adjustments", factors((day, 1))),
        **kwargs,
    )


def test_session_clock_matches_existing_intraday_contract():
    pytest.importorskip("pydantic")
    pytest.importorskip("bson")
    from app.engine.intraday_research import ENDS as intraday_ends

    assert ENDS == intraday_ends


def test_poc_tie_uses_midpoint_then_lower_bin():
    assert select_poc({0: 5, 1: 1, 2: 5}, 0, 2, 1) == 0
    assert select_poc({0: 4, 5: 4}, 0, 6, 1) == 5


def test_value_area_expands_heavier_side_and_takes_equal_counts_together():
    assert expand_value_area({0: 2, 1: 10, 2: 3}, 1, Fraction("0.70")) == (1, 2, 13)
    assert expand_value_area({0: 1, 1: 5, 2: 10, 3: 5, 4: 1}, 2, Fraction("0.70")) == (1, 3, 20)
    assert expand_value_area({3: 8}, 3, Fraction("0.70")) == (3, 3, 8)


def test_each_complete_bar_counts_a_bin_once_and_labels_the_approximation():
    result = run(flat(DAY))
    day = result["tpo"]["days"][0]
    assert result["tpo"]["label"] == TPO_LABEL
    assert day["status"] == "ready"
    assert day["total_count"] == 24
    assert day["poc"]["count"] == 8
    assert day["poc"]["lower"] == 10.01
    assert day["val"] == 10.0 and day["vah"] == 10.03
    assert day["covered_ratio"] >= 0.70
    assert "volume_profile" in day["not"]


def upward_then_reverse():
    # Higher low stays above the broken high so the reverse close is a CHoCH,
    # not an invalidation of the original break on the same bar.
    return [
        (10, 10.2, 9.8, 10),
        (10, 11, 10, 10.4),
        (10.2, 10.5, 10.1, 10.3),
        (11.0, 11.4, 10.8, 11.3),
        (11.3, 11.5, 11.25, 11.4),
        (11.4, 11.45, 11.15, 11.35),
        (11.3, 11.5, 11.3, 11.4),
        (11.3, 11.35, 11.0, 11.05),
    ]


def test_prefix_replay_has_no_future_knowledge():
    rows = session(DAY, upward_then_reverse())
    previous = None
    for index, minute in enumerate(ENDS):
        stamp = (datetime(2026, 11, 5, minute // 60, minute % 60, tzinfo=CN) + timedelta(seconds=60)).strftime(
            "%H:%M:%S"
        )
        full = run(rows, stamp, parameters={"swing_left": 1, "swing_right": 1})
        prefix = run(rows[: index + 1], stamp, parameters={"swing_left": 1, "swing_right": 1})
        assert full["shared_price"]["bar_ids"] == prefix["shared_price"]["bar_ids"]
        assert [event["id"] for event in full["smc"]["events"]] == [event["id"] for event in prefix["smc"]["events"]]
        visible = f"{DAY} {stamp}"
        for event in full["smc"]["events"]:
            assert event["first_knowable_time"] <= visible
            assert set(event["evidence_ids"]).issubset(full["shared_price"]["bar_ids"])
            if previous:
                prior = (
                    next(item for item in previous["smc"]["events"] if item["id"] == event["id"])
                    if any(item["id"] == event["id"] for item in previous["smc"]["events"])
                    else None
                )
                if prior:
                    assert prior["event_time"] == event["event_time"]
                    assert prior["first_knowable_time"] == event["first_knowable_time"]
        previous = full
    kinds = {event["kind"] for event in previous["smc"]["events"]}
    assert {"swing_high", "bos", "choch"}.issubset(kinds)
    choch = next(event for event in previous["smc"]["events"] if event["kind"] == "choch")
    assert choch["first_knowable_time"] == f"{DAY} 15:00:00"
    assert choch["direction"] == "bearish"
    early = run(rows[:7], "14:31:00", parameters={"swing_left": 1, "swing_right": 1})
    assert all(event["kind"] != "choch" for event in early["smc"]["events"])


def test_swing_is_not_knowable_on_its_own_bar():
    rows = flat(DAY)
    rows[1] = bar(DAY, ENDS[1], (10, 10.5, 9.9, 10.2))
    rows[2] = bar(DAY, ENDS[2], (10.1, 10.2, 9.9, 10.0))
    hidden = run(rows, "10:31:00", parameters={"swing_left": 1, "swing_right": 1})
    shown = run(rows, "11:01:00", parameters={"swing_left": 1, "swing_right": 1})
    assert all(event["kind"] != "swing_high" for event in hidden["smc"]["events"])
    swing = next(event for event in shown["smc"]["events"] if event["kind"] == "swing_high")
    assert swing["event_time"] == f"{DAY} 10:30:00"
    assert swing["first_knowable_time"] == f"{DAY} 11:00:00"


def test_fvg_waits_for_the_third_bar_and_skips_holes_and_overnight():
    prices = [(10, 10.05, 9.9, 10)] * 8
    prices[0] = (10, 10.0, 9.9, 9.95)
    prices[1] = (10, 10.4, 10.0, 10.3)
    prices[2] = (10.35, 10.6, 10.3, 10.5)
    rows = session(DAY, prices) + session(NEXT, [(10.8, 10.9, 10.7, 10.8)] * 8)
    early = run(rows, "10:31:00", calendar=calendar(DAY, NEXT), adjustments=factors((DAY, 1), (NEXT, 1)))
    assert all(event["kind"] != "fvg" for event in early["smc"]["events"])
    ready = run(rows, "15:01:00", day=NEXT, calendar=calendar(DAY, NEXT), adjustments=factors((DAY, 1), (NEXT, 1)))
    gaps = [event for event in ready["smc"]["events"] if event["kind"] == "fvg"]
    assert gaps and all(event["event_time"][:10] == event["first_knowable_time"][:10] for event in gaps)
    assert all(item["kind"] != "fvg" or item["first_knowable_time"] >= f"{DAY} 11:00:00" for item in gaps)
    assert all(item["status"] != "confirmed" or item["kind"] != "overnight_gap" for item in ready["smc"]["events"])
    holed = session(DAY, prices)
    del holed[1]
    isolated = run(holed, "15:01:00")
    assert isolated["sessions"][0]["status"] == "incomplete"
    assert all(event["kind"] != "fvg" for event in isolated["smc"]["events"])


def test_lunch_is_not_missing_and_adjustment_breakpoint_blocks_cross_day_events():
    complete = run(flat(DAY))
    assert complete["sessions"][0]["status"] == "complete"
    assert complete["sessions"][0]["missing"] == []
    midday = run(flat(DAY), "12:00:00")
    assert midday["sessions"][0]["status"] == "in_progress"
    assert 810 not in midday["sessions"][0]["missing"]
    rows = flat(DAY) + flat(NEXT, low=12, high=12.02)
    split = run(rows, "15:01:00", day=NEXT, calendar=calendar(DAY, NEXT), adjustments=factors((DAY, 1), (NEXT, 0.5)))
    assert split["adjustment"]["status"] == "breakpoint"
    assert NEXT in split["adjustment"]["breakpoints"]
    assert all(item["status"] != "confirmed" for item in split["overnight_gaps"])
    days = {}
    for row in rows:
        days[row["datetime"]] = row["datetime"][:10]
    # Evidence that would join the two factor regimes is not emitted as one event time span.
    for event in split["smc"]["events"]:
        assert event["event_time"][:10] == event["first_knowable_time"][:10]


def test_conflict_calendar_and_relative_gaps_stay_unjudged():
    rows = flat(DAY)
    rows.append({**rows[3], "open": 10.01, "high": 10.04, "low": 10.0, "close": 10.03, "data_source": "other"})
    conflicted = run(rows)
    assert conflicted["sessions"][0]["status"] == "conflict"
    assert conflicted["tpo"]["days"][0]["status"] == "cannot_judge"
    unverified = analyze(SYMBOL, flat(DAY) + flat(NEXT), at(NEXT, "15:01:00"), adjustments=factors((DAY, 1), (NEXT, 1)))
    assert unverified["calendar_status"] == "unverified"
    assert all(item["status"] == "cannot_judge" for item in unverified["overnight_gaps"])
    relative = run(
        flat(DAY),
        daily=[
            {"symbol": SYMBOL, "trade_date": "2026-11-04", "close": 10, "price_basis": "raw", "data_source": "tushare"},
            {"symbol": SYMBOL, "trade_date": DAY, "close": 9, "price_basis": "raw", "data_source": "tushare"},
        ],
    )
    assert relative["relative"]["index"]["status"] == "cannot_judge"
    assert relative["relative"]["sector"]["status"] == "cannot_judge"
    assert relative["relative"]["excess_close_to_close"] is None
    assert "异常" in relative["relative"]["attribution"]
    assert "stock_abnormal" not in relative["relative"]
    assert relative["performance_claim"] is False


def test_wyckoff_background_is_not_mutated_and_conflicts_are_kept():
    stored = {"phase": "markdown", "trend": "down", "status": "supported", "method_version": "wyckoff-method-v2"}
    original = deepcopy(stored)
    rows = session(DAY, upward_then_reverse())[:7]
    result = run(
        rows,
        "14:31:00",
        parameters={"swing_left": 1, "swing_right": 1},
        wyckoff=stored,
        held_action="HOLD",
        unheld_action="WATCH",
    )
    assert stored == original
    assert result["preserved"] == {
        "held_action": "HOLD",
        "unheld_action": "WATCH",
        "wyckoff_phase": "markdown",
        "wyckoff_status": "supported",
        "report_mutated": False,
        "decision_effect": "none",
    }
    assert result["wyckoff_background"]["mutated"] is False
    assert result["trade_authorized"] is False and result["vote"] is False
    assert result["smc"]["order_blocks"]["status"] == "deferred"
    assert result["smc"]["state"] == "up"
    assert any(item["left"] == "日线威科夫背景" and item["right"] == "30分钟SMC状态" for item in result["conflicts"])
    assert all(item["resolution"].startswith("保留冲突") for item in result["conflicts"])


def test_publication_delay_and_rejected_parameters():
    rows = flat(DAY)
    assert run(rows, "10:00:59")["as_of"] is None
    assert run(rows, "10:01:00")["sessions"][0]["bars"] == 1
    with pytest.raises(ValueError):
        run(rows, parameters={"bin_multiple": 0})


def test_settings_default_leaves_existing_switches_unchanged():
    pytest.importorskip("pydantic")
    pytest.importorskip("bson")
    from app.models.research_tracking import TrackSettings

    dumped = TrackSettings().model_dump()
    assert dumped["structure_auxiliary_enabled"] is False
    assert dumped["intraday_enabled"] is True
    legacy = TrackSettings.model_validate({"auto_enabled": True, "max_calls_per_day": 6, "intraday_enabled": True})
    assert legacy.structure_auxiliary_enabled is False
