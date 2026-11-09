"""Deterministic replay checks, including prefix invariance and rejected evidence."""
from copy import deepcopy
from datetime import datetime

import pytest

from app.engine.intraday_research import CN, ENDS, evaluate, prepare, reference_zones

DAY = "2026-11-05"


def native_rows():
    prices = [(10, 10.2, 9.9, 10.1), (10.1, 10.3, 9.8, 10.2), (10.2, 10.4, 10, 10.3),
              (10.3, 10.6, 10.1, 10.5), (10.5, 10.55, 10.2, 10.25), (10.25, 10.3, 10, 10.1),
              (10.1, 10.2, 9.9, 10), (10, 10.1, 9.7, 9.8)]
    return [dict(symbol="000503", datetime=f"{DAY} {m//60:02d}:{m%60:02d}:00", freq="30min",
        data_source="tencent", bar_time_kind="end", ohlc_kind="native_bar", price_basis="raw", volume_unit="share",
        open=o, high=h, low=l, close=c, volume=1000+i*100) for i, (m, (o, h, l, c)) in enumerate(zip(ENDS, prices))]


def snapshot(rows=None, stamp="15:01:00", session=None):
    return prepare("000503", native_rows() if rows is None else rows,
        session or {"date": DAY, "is_open": True}, datetime.fromisoformat(f"{DAY}T{stamp}").replace(tzinfo=CN))


@pytest.mark.parametrize("index", range(8))
def test_replay_prefix_has_no_future_bars_or_pivots(index):
    m = ENDS[index]
    stamp = f"{m//60:02d}:{m%60+1:02d}:00"
    prefix, future = snapshot(native_rows()[:index+1], stamp), snapshot(stamp=stamp)
    assert prefix == future
    assert len(prefix["bars"]) == index+1
    for zone in reference_zones(prefix):
        assert zone["observed_at"] <= prefix["as_of"]
        assert all(e in {b["id"] for b in prefix["bars"]} for e in zone["evidence_ids"])


@pytest.mark.parametrize("stamp,count", [("09:59:59", 0), ("10:00:59", 0), ("10:01:00", 1), ("12:59:59", 4), ("13:30:59", 4)])
def test_completion_and_lunch_boundaries(stamp, count):
    result = snapshot(stamp=stamp)
    assert len(result["bars"]) == count


@pytest.mark.parametrize("change", [
    {"price_basis": "qfq"}, {"volume_unit": "lot"}, {"bar_time_kind": "start"},
    {"symbol": "002241"}, {"volume": -1}, {"close": float("nan")},
    {"ohlc_kind": "last_price_sample"}, {"high": 1}, {"volume": True}, {"freq": "5min"},
])
def test_invalid_evidence_is_not_usable(change):
    rows = native_rows()
    rows[2].update(change)
    result = snapshot(rows)
    assert result["status"] == "insufficient"
    assert reference_zones(result) == []


def test_gaps_duplicates_conflicts_and_stale_last_bar():
    rows = native_rows()
    for bad in (rows[:-1], rows[:3]+rows[4:], rows+[{**rows[2], "close": 10.35}],
                rows+[{**rows[2], "data_source": "peer", "close": 10.35}]):
        assert snapshot(bad)["status"] == "insufficient"
    assert snapshot(rows+[rows[2]])["status"] == "ready"


def test_unverified_calendar_prevents_decision():
    for session in ({"date": DAY, "is_open": None}, {"date": DAY, "is_open": False}, {"date": "2026-11-04", "is_open": True}):
        assert snapshot(session=session)["status"] == "insufficient"


def test_touch_does_not_satisfy_complex_conditions_or_change_stops():
    levels = [{"kind": "exit", "label": "原止损", "lower": 9.75, "upper": 9.8, "price_basis": "raw",
               "valid_until": DAY, "condition": "放量跌破且行业风险恶化，核验可卖仓位"}]
    before = deepcopy(levels)
    result = evaluate(snapshot(), levels, [{"kind": "entry", "text": "企稳且公告无风险"}])
    assert result["conditions"][0]["state"] == "candidate"
    assert result["conditions"][1]["state"] == "unsupported"
    assert levels == before and result["trade_authorized"] is False
    assert result["sellable_position"] == "unknown"


def test_expiry_and_unknown_price_basis():
    level = {"label": "条件", "lower": 9.75, "upper": 9.8, "price_basis": "unspecified"}
    assert evaluate(snapshot(), [level], [])["conditions"][0]["state"] == "missing_data"
    assert evaluate(snapshot(), [{**level, "valid_until": "2026-11-04"}], [])["conditions"][0]["state"] == "expired"


@pytest.mark.parametrize("text,kind,state", [
    ("30分钟收盘价低于10.00元", "veto", "invalidated"),
    ("30分钟收盘价低于10.00元", "exit", "met"),
    ("30分钟收盘价高于10.00元", "entry", "not_met"),
    ("30分钟收盘价低于10.00元且公告无风险", "entry", "unsupported"),
    ("收盘价低于10.00元", "exit", "met"),
    ("30分钟收盘价高于0元", "entry", "unsupported"),
    ("30分钟收盘价跌破10.00元", "veto", "invalidated"),
    ("30分钟收盘价跌破10.20元", "veto", "not_met"),
])
def test_only_exact_price_clause_can_be_proven(text, kind, state):
    assert evaluate(snapshot(), [], [{"text": text, "kind": kind}])["conditions"][0]["state"] == state


def test_unsupported_amount_does_not_create_vwap():
    result = evaluate(snapshot(), [], [])
    assert "vwap" not in result
    assert result["volume_ratio_previous_two_bars"] == round(1700/1550, 3)
    assert result["wyckoff_candidate"]
