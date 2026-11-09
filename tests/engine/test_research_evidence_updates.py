from copy import deepcopy

import pytest

from app.engine.intraday_alerts import price_alerts
from app.engine.research_price_evidence import historical_price_claims, target_comparison
from app.engine.wyckoff_quotes import bind_assessment_quotes
from app.engine.wyckoff_tracking import wyckoff_board


def snapshot():
    return {"date": "2026-11-05", "status": "ready", "bars": [
        {"id": "a", "datetime": "2026-11-05 10:00:00", "low": 24.1, "high": 24.8, "close": 24.7},
        {"id": "b", "datetime": "2026-11-05 10:30:00", "low": 24.6, "high": 25.1, "close": 24.9},
        {"id": "c", "datetime": "2026-11-05 11:00:00", "low": 24.8, "high": 25.4, "close": 25.2},
    ]}


def level():
    return {"label": "第一目标", "kind": "target", "lower": 24.64, "upper": 24.64,
            "price_basis": "raw", "condition": "缩量滞涨时分批减仓", "source_date": "2026-11-04"}


def test_target_alert_keeps_first_evidence_and_never_backdates_afterclose_report():
    values = price_alerts(snapshot(), [level()], known_at="2026-11-04T18:00:00+08:00")
    assert [r["kind"] for r in values] == ["touch", "above_two_closes"]
    assert values[0]["evidence_at"] == "2026-11-05 10:00:00"
    assert values[1]["evidence_at"] == "2026-11-05 10:30:00"
    assert all(r["trade_authorized"] is False for r in values)
    assert price_alerts(snapshot(), [level()], known_at="2026-11-05T16:00:00+08:00") == []
    # During a bar's lifetime the new report was unavailable at the start.
    assert price_alerts(snapshot(), [level()], known_at="2026-11-05T10:15:00+08:00") == []
    again = price_alerts({**snapshot(), "checked_at": "later"}, [level()], known_at="2026-11-04T18:00:00+08:00")
    assert [r["id"] for r in values] == [r["id"] for r in again]


@pytest.mark.parametrize("change", [{"price_basis": "unspecified"}, {"expired": True}, {"valid_until": "2026-11-04"}, {"historical_claim_conflict": True}])
def test_unknown_basis_or_expired_points_cannot_raise_price_alert(change):
    assert not price_alerts(snapshot(), [{**level(), **change}], known_at="2026-11-04T18:00:00+08:00")


def test_missing_minutes_cannot_raise_breakout_alert():
    assert not price_alerts({**snapshot(), "status": "insufficient"}, [level()], known_at="2026-11-04T18:00:00+08:00")


def test_correct_price_wrong_date_remains_conflict_not_nearest_date_repaired():
    text = "| 阻力位3 | 29.18 元 | 强 | 2026-05-29 年度高点阻力位（上影线顶点） |"
    bars = [{"date": "2026-05-28", "high": 29.18}, {"date": "2026-05-29", "high": 28.39}]
    r = historical_price_claims(text, bars)[0]
    assert r["status"] == "mismatch" and r["actual"] == 28.39 and r["date"] == "2026-05-29"
    assert historical_price_claims(text, [])[0]["status"] == "missing_data"
    assert historical_price_claims(text.replace("05-29", "05-28"), bars)[0]["status"] == "matched"
    assert len(historical_price_claims(text+"\n"+text, bars)) == 1
    from app.engine.research_price_evidence import with_price_conflicts
    assert with_price_conflicts([{**level(), "lower": 29.18, "upper": 29.18}], text, bars)[0]["historical_claim_conflict"]
    new = {**level(), "lower": 29.18, "upper": 29.18}
    out = target_comparison([new], [level()], current_date="2026-11-05", previous_date="2026-11-04",
                            known_at="2026-11-05T17:00:00+08:00", content=text, bars=bars)
    assert out["targets"][0]["change"] == "added"
    assert out["targets"][0]["status"] == "historical_claim_conflict"
    assert out["previous_targets"][0]["lower"] == 24.64


def quote_value(quote):
    return {"source_quote": "趋势背景：上涨；当前阶段：拉升。", "confirmation_conditions": [],
            "invalidation_conditions": [], "events": [{"kind": "SOS", "date": "2026-11-05", "source_quote": quote}], "trading_range": None}


def test_event_quote_recovers_only_its_unique_original_labelled_line():
    quote = "2026-11-05，收盘上涨并放量"
    line = "5. **SOS（强势信号）**：" + quote
    assert bind_assessment_quotes(quote_value(quote), line)["events"][0]["source_quote"] == line
    for text in [line+"\n"+line, line+"，也可能是 UT", line+"；此前2026-11-04也观察到"]:
        assert bind_assessment_quotes(quote_value(quote), text)["events"][0]["source_quote"] == quote


def test_failed_new_technical_run_shows_new_report_text_separate_from_old_phase():
    text = "**威科夫方法校验：阶段结构未通过校验，不能作为已确认阶段。** 事件时间顺序不成立。\n\n# 技术报告\n\n## 威科夫\n\n趋势背景：上涨；当前阶段：拉升；状态：本轮证据支持。"
    baseline = {"analysis_id": "new", "as_of": "2026-11-05", "reports": {"market_report": text}}
    selected = {"_id": "v", "created_at": "2026-11-05T20:00:00+08:00", "modules": {},
                "errors": ["技术更新：模型上游服务暂时不可用"]}
    before = deepcopy(baseline)
    out = wyckoff_board(baseline, [], selected)
    assert out["current"] is None and not out["history"]
    assert out["status"] == "failed" and "上游" in out["error"]
    assert out["pending_judgment"]["as_of"] == "2026-11-05"
    assert out["pending_judgment"]["status"] == "unvalidated"
    assert "拉升" in out["pending_judgment"]["source_quote"]
    assert baseline == before

def test_actual_goertek_submission_exposes_method_error_before_spending_correction():
    import json
    from pathlib import Path
    from app.engine.wyckoff_tracking import checked_assessment
    x = json.loads((Path(__file__).parents[1] / "fixtures/research_evidence/002241_20261105.json").read_text())
    original = x["original"]
    with pytest.raises(ValueError, match=r"Spring.*2026-10-15.*2026-10-23"):
        checked_assessment(original["wyckoff"], original["content"], x["snapshot"], require_method=True)
    checks = historical_price_claims(original["content"], x["price_bars"])
    mismatches = [c for c in checks if c["price"] == 29.18]
    assert mismatches and all(c["status"] == "mismatch" and c["actual"] == 28.39 for c in mismatches)
