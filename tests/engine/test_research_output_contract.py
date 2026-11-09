"""Pure output-contract regressions; no model, database substitute or defaults."""
from copy import deepcopy

import pytest

from app.engine.decision_semantics import normalize_decision, synchronize_summary
from app.engine.research_contract import RESEARCH_OUTPUT_RULES, qualitative_confidence
from app.services.reports_service import _extract_structured_fields


def terminal():
    return normalize_decision({"unheld_action": "WATCH", "held_action": "REDUCE",
        "account_position": "HELD", "confidence": None, "risk_score": None,
        "target_price": None}, "当前不买。已有持仓减仓，未来条件未获授权。")


def summary(confidence=45):
    return {"model_confidence": confidence,
        "risk_assessment": {"level": "High", "score": 8, "description": "波动及基本面风险较高"},
        "key_indicators": {"entry_price": "条件触发后7.50", "target_price": "7.95；条件成立后8.04–8.25",
                           "stop_loss": "7.17为技术失效参考"}, "final_signal": "Buy"}


def test_terminal_nulls_do_not_erase_distinct_research_values_or_upgrade_actions():
    original = summary()
    result = synchronize_summary(original, terminal())
    assert original == summary()
    assert result["model_confidence"] == 45
    assert result["risk_assessment"]["level"] == "High"
    assert result["risk_assessment"]["score"] == 8
    assert result["key_indicators"] == original["key_indicators"]
    assert result["final_signal"] == "REDUCE"
    assert result["decision_semantics"]["unheld_action"] == "WATCH"
    assert result["decision_semantics"]["current_entry_limit_pct"] == 0
    assert result["assessment_metadata"]["price_kind"] == "research_reference_not_order"
    assert synchronize_summary(result, terminal()) == result


@pytest.mark.parametrize("value", [0, 1, 45, 50, 100])
def test_model_self_assessment_uses_the_declared_zero_to_hundred_scale(value):
    result = synchronize_summary(summary(value), terminal())
    assert result["model_confidence"] == value
    from app.services.analysis_service import _extract_result_risk_and_confidence
    assert _extract_result_risk_and_confidence(terminal(), result) == (value / 100, "高")


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), -1, 101, "N/A", None])
def test_invalid_or_absent_model_confidence_stays_unquantified(value):
    result = synchronize_summary(summary(value), terminal())
    assert result["model_confidence"] is None
    adapted = _extract_structured_fields({"decision": terminal(), "structured_summary": summary(value)})
    assert adapted["confidence_score"] is None


def test_report_read_adapter_preserves_summary_without_mutating_history():
    doc = {"decision": {"action": "持有", "confidence": .7, "risk_score": .5},
        "structured_summary": summary(),
        "reports": {"risk_manager_decision": "当前不买。已有持仓持有。"}}
    original = deepcopy(doc)
    result = _extract_structured_fields(doc)
    assert doc == original
    assert result["confidence_score"] == .45
    assert result["risk_level"] == "高"
    assert result["decision"]["confidence"] is None  # the former default .7 is unsupported
    assert result["structured_summary"]["key_indicators"]["target_price"] == summary()["key_indicators"]["target_price"]


@pytest.mark.parametrize("level", ["High", "高", "中高", "Unknown", "未知"])
def test_report_adapter_keeps_supported_chinese_and_english_risk_labels(level):
    values = summary()
    values["risk_assessment"]["level"] = level
    result = _extract_structured_fields({"decision": terminal(), "structured_summary": values})
    assert result["risk_level"] == {"High": "高", "Unknown": "未知"}.get(level, level)


def test_missing_research_fields_do_not_gain_prices_scores_or_default_risk():
    result = synchronize_summary({}, terminal())
    assert result["model_confidence"] is None
    assert result["risk_assessment"]["score"] is None
    assert result["risk_assessment"]["level"] == "Unknown"
    assert result["key_indicators"]["target_price"] == "N/A"


def test_malformed_legacy_optional_objects_do_not_break_whole_report():
    result = synchronize_summary({"risk_assessment": "unknown", "key_indicators": [],
                                  "assessment_metadata": "old"}, terminal())
    assert result["risk_assessment"]["level"] == "Unknown"
    assert result["model_confidence"] is None
    adapted = _extract_structured_fields({"structured_summary": {"risk_assessment": "unknown"},
                                         "decision": terminal()})
    assert adapted["risk_level"] == "未知"


def test_explicit_qualitative_confidence_is_not_mapped_to_a_probability():
    decision = normalize_decision({"action": "观望"}, "整体置信度：中低；因新闻样本覆盖不足。")
    result = synchronize_summary({}, decision)
    assert qualitative_confidence("整体置信度：中低；") == "中低"
    assert qualitative_confidence("| 决策置信度 | **中低** | 日线依据明确 |") == "中低"
    assert result["confidence_label"] == "中低"
    assert result["model_confidence"] is None
    assert qualitative_confidence("置信度不足，不能计算胜率") is None


def test_common_rules_keep_conditional_research_separate_from_execution():
    assert "单项缺失仅限制依赖该项" in RESEARCH_OUTPUT_RULES
    assert "不为每日编造" in RESEARCH_OUTPUT_RULES
    assert "不得猜测或放宽用户既有风控" in RESEARCH_OUTPUT_RULES


def test_shared_runtime_prompt_preserves_user_role_and_is_idempotent():
    from app.engine.research_contract import append_research_contract
    original = "市场：CN\n用户配置的角色定义，不改写数据库配置。"
    result = append_research_contract(original)
    from app.engine.wyckoff_method import METHOD_CONTRACT
    assert result == original + RESEARCH_OUTPUT_RULES + METHOD_CONTRACT
    assert append_research_contract(result) == result


def test_evidence_dependencies_follow_configured_domains_not_stock_or_role_names():
    from app.engine.tools.datasources.registry import get_specs_by_ids, expand_evidence_dependencies
    configured = get_specs_by_ids(["financial_data", "daily_indicators", "money_flow"])
    before = [spec.tool_id for spec in configured]
    expanded = expand_evidence_dependencies(configured, "CN")
    ids = [spec.tool_id for spec in expanded]
    assert {"official_announcements", "valuation_history", "capital_context"} <= set(ids)
    assert len(ids) == len(set(ids))
    assert [spec.tool_id for spec in configured] == before
    assert [spec.tool_id for spec in expand_evidence_dependencies(configured, "HK")] == before
