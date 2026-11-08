"""Replay actual report submissions, no LLM or simulated data clients."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from app.engine.decision_semantics import normalize_decision, synchronize_summary
from app.services.reports_service import _extract_structured_fields


@pytest.fixture(scope="module")
def actual_reports():
    path = os.environ.get("REPORT_EVIDENCE_SNAPSHOT")
    if not path:
        pytest.skip("Requires the user-authorized private report snapshot")
    data = json.loads(Path(path).read_text())
    assert len(data["reports"]) == 3
    return data


@pytest.mark.parametrize("symbol", ["600997", "600977", "300124"])
def test_actual_final_current_caps_and_conditional_plans_survive(actual_reports, symbol):
    report = next(row for row in actual_reports["reports"] if row["stock_symbol"] == symbol)
    event = [row for row in actual_reports["events"][report["task_id"]]
             if row["event_type"] == "tool_call" and row["agent_key"] == "risk_manager"
             and row["payload"]["tool"] == "submit_report"][-1]
    submission = event["payload"]["input"]
    raw = {**submission, **submission["decision_semantics"]}
    source = submission["content"]
    before = deepcopy(raw)
    decision = normalize_decision(raw, source)
    assert decision["total_position_limit_pct"] == 0
    assert decision["current_entry_limit_pct"] == 0
    assert decision["primary_action"] == "WATCH"
    assert decision["confidence"] is None  # Original model supplied a label only.
    assert decision["confidence_label"]
    for plan in submission["decision_semantics"]["conditional_strategy"]:
        assert plan in decision["conditional_strategy"]
    assert raw == before
    assert normalize_decision(decision, source) == decision
    summary = synchronize_summary(report["structured_summary"], decision)
    assert summary["decision_semantics"]["total_position_limit_pct"] == 0
    assert summary["key_indicators"] == report["structured_summary"]["key_indicators"]
    old = deepcopy(report)
    adapted = _extract_structured_fields(report)
    assert adapted["decision"]["total_position_limit_pct"] == 0
    assert report == old  # Read adaptation never migrates history.


def test_other_actor_and_future_unknown_do_not_cancel_current_zero():
    source = (
        "本账户NONE，当前观望。\n"
        "| 本账户当前总仓位上限 | 0% | 不适用于已有持仓账户 |\n"
        "当前新增及总仓位上限均为0%。\n"
        "未来条件方案总仓位上限未明确。\n"
        "已有持仓账户的总仓位上限未提供。"
    )
    none = normalize_decision({"account_position": "NONE", "unheld_action": "WATCH"}, source)
    held = normalize_decision({"account_position": "HELD", "held_action": "HOLD"}, source)
    assert none["total_position_limit_pct"] == 0
    assert held["total_position_limit_pct"] is None
    assert held["primary_action"] == "HOLD"


@pytest.mark.parametrize("unknown", ["当前总仓位上限未知", "本账户总仓位上限未提供"])
def test_current_unknown_or_conflict_remains_unknown(unknown):
    text = "当前总仓位上限0%。\n" + unknown
    assert normalize_decision({"account_position": "NONE", "total_position_limit_pct": 0}, text)["total_position_limit_pct"] is None
    conflict = normalize_decision({"account_position": "NONE"}, "当前总仓位上限0%。\n当前总仓位上限5%。")
    assert conflict["total_position_limit_pct"] is None


def test_future_or_denied_nonzero_cannot_become_current_authorization():
    decision = normalize_decision({"account_position": "NONE", "unheld_action": "WATCH"},
        "本账户NONE，当前观望。当前新增及总仓位上限均为0%。\n"
        "未来首笔仓位不超过30%；当前不批准新增仓位5%。")
    assert decision["total_position_limit_pct"] == decision["current_entry_limit_pct"] == 0
    assert any(item.get("limit_pct") == 30 for item in decision["conditional_strategy"])
    assert decision["primary_action"] == "WATCH"


def test_explicit_account_argument_and_public_current_policy_have_separate_scopes():
    text = (
        "账户状态：NONE。\n| 本账户当前总仓位上限 | 0% | 仅用于当前政策 |\n"
        "已有持仓账户的总仓位上限未提供。\n当前新增上限0%。"
    )
    selected = normalize_decision({}, text, account_position="NONE")
    assert selected["total_position_limit_pct"] == 0
    assert selected["position_evidence"]["current_entry_limit_pct"]["status"] == "explicit"
    public = normalize_decision({}, "当前策略总仓位上限0%。\n存量策略总仓位上限未提供。")
    assert public["total_position_limit_pct"] == 0
