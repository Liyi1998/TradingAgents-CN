"""Deterministic regression coverage for position-aware verdict semantics.

These tests use representative excerpts from the nine 2026-09-01/02 reports.
They do not construct an LLM client or perform any external call.
"""

import pytest

from app.engine.decision_semantics import (
    ACTION_LABELS,
    format_decision_recommendation,
    merge_decision_key_points,
    normalize_decision,
    render_decision_markdown,
)
from app.services.reports_service import _extract_structured_fields


LEGACY_HOLD_CASES = [
    (
        "603232",
        "最终投资评级：持有。允许保留3%—4%基础仓；未持有者可在明确回踩确认后以0.5%—1%试仓。"
        "最大总仓位不超过5%，以14.30元为硬止损。",
        "HOLD",
    ),
    (
        "601766",
        "最终投资评级：持有。已有合理仓位继续持有；未持仓者不在6.31—6.33元追买，"
        "主要新增仓位等待回踩企稳或突破确认。跌破6.10元退出。",
        "HOLD",
    ),
    (
        "300194",
        "最终投资评级：持有。现价主动买入的风险收益比不足；技术结构尚未转空，因此维持持有。"
        "跌破3.72元止损。",
        "HOLD",
    ),
    (
        "600332",
        "最终评级：持有。已有低仓位继续持有；未持仓账户以突破确认优先，观望为主。"
        "跌破关键风险位应及时减仓。",
        "HOLD",
    ),
    (
        "002241",
        "最终投资评级：持有（偏谨慎）。尚未持仓：等待回踩确认或24.80元有效突破后再小仓介入；"
        "已有仓位谨慎持有。跌破22.30元退出。",
        "HOLD",
    ),
    (
        "002223",
        "最终投资评级：谨慎持有。未持仓者：观望，不在26.15元附近建立核心仓位。"
        "已有持仓账户继续持有，跌破25.55元减仓。",
        "HOLD",
    ),
    (
        "600875",
        "最终投资评级：持有（防守型）。现价高于中性估值，宜防守型持有并控制仓位；"
        "未持仓不追涨，失守22.70元退出。",
        "HOLD",
    ),
    (
        "300094",
        "最终评级：持有（偏谨慎）。当前上涨空间小于潜在回撤，不宜追涨，已有轻仓可谨慎持有。"
        "跌破3.02元清仓。",
        "HOLD",
    ),
    (
        "000503",
        "最终投资评级：持有（防御性持有，不新增追涨仓）。当前空仓维持观望并保持0%；"
        "已有仓位降至3%以内。只有放量站稳后仓位才可提高至最高5%；跌破7.02元全部退出。",
        "REDUCE",
    ),
]


@pytest.mark.parametrize(("symbol", "report_text", "held_action"), LEGACY_HOLD_CASES)
def test_nine_legacy_hold_reports_do_not_become_flat_account_hold(
    symbol: str,
    report_text: str,
    held_action: str,
):
    decision = normalize_decision(
        {"action": "持有", "reasoning": report_text}, report_text
    )

    assert decision["schema_version"] == 3, symbol
    assert decision["account_position"] == "UNKNOWN", symbol
    assert decision["research_view"] == "NEUTRAL", symbol
    assert decision["unheld_action"] == "WATCH", symbol
    assert decision["held_action"] == held_action, symbol
    assert decision["primary_action"] == "WATCH", symbol
    assert decision["action"] == "观望", symbol
    assert decision["legacy_action"] == "持有", symbol
    assert decision["derived_from_legacy"] is True, symbol


def test_account_position_selects_only_the_matching_branch():
    raw = {
        "research_view": "NEUTRAL",
        "unheld_action": "WATCH",
        "held_action": "REDUCE",
    }

    flat = normalize_decision(raw, account_position="NONE")
    held = normalize_decision(raw, account_position="HELD")

    assert flat["primary_action"] == "WATCH"
    assert flat["action"] == "观望"
    assert held["primary_action"] == "REDUCE"
    assert held["action"] == "减仓"


def test_action_codes_have_non_overlapping_meanings():
    assert ACTION_LABELS == {
        "BUY": "买入",
        "WATCH": "观望",
        "HOLD": "持有",
        "REDUCE": "减仓",
        "SELL": "卖出",
    }
    assert normalize_decision({"action": "买入"})["unheld_action"] == "BUY"
    assert normalize_decision({"action": "持有"})["unheld_action"] == "WATCH"
    assert normalize_decision({"action": "持有"})["held_action"] == "HOLD"
    assert normalize_decision({"action": "卖出"})["held_action"] == "SELL"


def test_position_limit_and_exit_conditions_are_extracted_without_llm():
    text = LEGACY_HOLD_CASES[-1][1]
    decision = normalize_decision({"action": "持有"}, text)

    assert decision["position_limit_pct"] == 3.0
    assert decision["current_entry_limit_pct"] == 0.0
    assert any(item["limit_pct"] == 5 for item in decision["conditional_strategy"])
    assert any("跌破7.02元全部退出" in item for item in decision["exit_conditions"])


def test_report_read_compatibility_does_not_need_database_migration():
    text = (
        "最终投资评级：持有。未持仓者观望，不在现价买入；"
        "已有持仓继续持有。总仓位不超过8%，跌破10元退出。"
    )
    doc = {
        "decision": {"action": "持有", "confidence": 0.7},
        "recommendation": "持有",
        "reports": {"risk_manager_decision": text},
        "key_points": ["操作建议：持有", "现金流改善"],
    }

    extracted = _extract_structured_fields(doc)

    assert doc["decision"] == {"action": "持有", "confidence": 0.7}
    assert extracted["decision"]["primary_action"] == "WATCH"
    assert "未持仓：观望（WATCH）" in extracted["recommendation"]
    assert "已持仓：持有（HOLD）" in extracted["recommendation"]
    assert "操作建议：持有" not in extracted["key_points"]


def test_account_context_alone_is_not_mislabeled_as_structured_decision():
    decision = normalize_decision(
        {"account_position": "NONE", "action": "持有"},
        "最终投资评级：持有。未持仓者观望，已有持仓继续持有。",
    )

    assert decision["derived_from_legacy"] is True
    assert decision["semantics_source"] == "LEGACY_DETERMINISTIC"


def test_string_key_points_are_not_split_into_characters():
    decision = normalize_decision({"action": "持有"})

    points = merge_decision_key_points("现金流改善", decision)

    assert points[-1] == "现金流改善"


def test_new_markdown_serializes_all_position_aware_fields():
    decision = normalize_decision(
        {
            "account_position": "UNKNOWN",
            "research_view": "NEUTRAL",
            "unheld_action": "WATCH",
            "held_action": "HOLD",
            "position_limit_pct": 5,
            "exit_conditions": ["跌破7.02元全部退出"],
            "confidence": 0.7,
            "risk_score": 0.8,
            "reasoning": "现价不追涨，已有轻仓可继续持有。",
        }
    )

    markdown = render_decision_markdown("000503", decision)
    recommendation = format_decision_recommendation(decision)

    assert "**账户持仓状态**: 未提供" in markdown
    assert "**未持仓动作**: 观望（WATCH）" in markdown
    assert "**已持仓动作**: 持有（HOLD）" in markdown
    assert "**仓位上限**: 5%" in markdown
    assert "跌破7.02元全部退出" in markdown
    assert recommendation.startswith("账户持仓：未提供；研究观点：中性")
