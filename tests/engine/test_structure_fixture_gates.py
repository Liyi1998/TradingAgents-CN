"""Gate checks for the synthetic packets shown on the isolated research page."""

import os
from datetime import datetime

os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("JWT_SECRET", "isolated-test-jwt-not-a-credential")
os.environ.setdefault("CSRF_SECRET", "isolated-test-csrf-not-a-credential")

from app.engine.structure_auxiliary import CN, analyze
from app.services.structure_auxiliary import synthetic_fixture_packet
from tests.fixtures.structure_packets import scenario_packets, write_packets

NOW = datetime(2026, 11, 9, 7, 30, tzinfo=CN)


def _result(symbol: str):
    body = scenario_packets()[symbol]
    return analyze(
        symbol,
        body["rows"],
        NOW,
        calendar=body["calendar"],
        adjustments=body["adjustments"],
        daily=body["daily"],
        index_rows=body["index_rows"],
        index_symbol=body["index_symbol"],
    )


def test_complete_day_is_ready_and_has_no_lookahead():
    result = _result("900001")
    assert result["status"] == "ready"
    assert result["tpo"]["days"][0]["status"] == "ready"
    assert result["trade_authorized"] is False and result["vote"] is False
    assert any(event["kind"] == "sweep" for event in result["smc"]["events"])
    assert all(event["first_knowable_time"] <= result["as_of"] for event in result["smc"]["events"])
    assert next(gate["status"] for gate in result["gates"] if gate["name"] == "新鲜度") == "ready"


def test_missing_calendar_keeps_the_day_profile_partial():
    result = _result("900002")
    assert result["tpo"]["days"][0]["status"] == "ready"
    assert result["calendar_status"] == "unverified"
    assert result["status"] == "partial"
    assert next(gate["status"] for gate in result["gates"] if gate["name"] == "交易日历") == "cannot_judge"
    assert all(item["status"] != "confirmed" for item in result["overnight_gaps"])


def test_missing_bar_stale_conflict_and_missing_factor_stay_blocked():
    missing = _result("900003")
    assert missing["sessions"][0]["status"] == "incomplete"
    assert missing["tpo"]["days"][0]["status"] == "cannot_judge"
    assert next(gate["status"] for gate in missing["gates"] if gate["name"] == "新鲜度") == "ready"
    stale = _result("900004")
    assert next(gate["status"] for gate in stale["gates"] if gate["name"] == "新鲜度") == "stale"
    assert stale["status"] == "cannot_judge"
    conflicted = _result("900005")
    assert conflicted["sessions"][0]["status"] == "conflict"
    assert conflicted["tpo"]["days"][0]["status"] == "cannot_judge"
    assert next(gate["status"] for gate in conflicted["gates"] if gate["name"] == "来源") == "cannot_judge"
    unadjusted = _result("900006")
    assert unadjusted["tpo"]["days"][0]["status"] == "ready"
    assert unadjusted["adjustment"]["status"] == "unverified"
    assert unadjusted["status"] == "partial"
    assert next(gate["status"] for gate in unadjusted["gates"] if gate["name"] == "复权") == "cannot_judge"


def test_fixture_directory_never_falls_through_when_configured(tmp_path):
    assert synthetic_fixture_packet("900001", "") is None
    missing = synthetic_fixture_packet("900001", str(tmp_path))
    assert missing["evidence_origin"] == "synthetic_test"
    assert missing["rows"] == []
    write_packets(str(tmp_path))
    loaded = synthetic_fixture_packet("900001", str(tmp_path))
    assert loaded["evidence_origin"] == "synthetic_test"
    assert loaded["issues"][0] == "合成测试行情，不是交易所回报"
    assert len(loaded["rows"]) == 8
