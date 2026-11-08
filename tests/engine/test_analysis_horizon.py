"""Real prompt/contract paths for selectable periods, without model calls or mocks."""
from copy import deepcopy

import pytest

from app.engine.analysis_horizon import horizon_memory_name, render_horizon_contract
from app.engine.decision_semantics import normalize_decision, synchronize_summary
from app.engine.orchestrator.agents import _resolve_inject_args
from app.engine.orchestrator.pipeline import _require_valid_summary, _require_valid_text
from app.engine.orchestrator.state import export_legacy_state
from app.engine.orchestrator.workflow.submission import validate_submission
from app.engine.research_contract import RESEARCH_OUTPUT_RULES, append_research_contract
from app.engine.tools.datasources.registry import get_spec_by_id


def test_legacy_and_explicit_short_preserve_existing_prompt_bytes():
    original = "用户维护的角色职责。未来3天买入、未来7天卖出。"
    for value in (None, "short"):
        assert render_horizon_contract(value) == ""
        result = append_research_contract(original, value)
        from app.engine.wyckoff_method import METHOD_CONTRACT
        assert result == original + RESEARCH_OUTPUT_RULES + METHOD_CONTRACT
        assert append_research_contract(result, value) == result


@pytest.mark.parametrize("horizon,period", [("medium", "1–3个月"), ("long", "6–12个月")])
@pytest.mark.parametrize("role", ["market", "fundamentals", "short_term_capital", "financial_news", "social_media",
                                  "bull_researcher", "bear_researcher", "research_manager", "trader",
                                  "risky_analyst", "safe_analyst", "neutral_analyst", "risk_manager", "summary",
                                  "signal_processor", "custom-role"])
def test_every_role_uses_selected_period_after_legacy_role_requirements(horizon, period, role):
    original = "既有角色职责必须保留。未来3天买入、未来7天卖出。中线≤30个交易日后离场。"
    result = append_research_contract(original, horizon, agent_key=role)
    assert result.startswith(original)
    assert f"analysis_horizon={horizon}" in result
    assert period in result
    assert "不作为本次必做或验收要求" in result
    assert "不是承诺收益期限或到期强制卖出日期" in result
    assert "复查节点" in result
    assert "单项缺失仅限制依赖该项" in result
    assert "以及未来3/7个实际交易日的观察方案" not in result
    assert append_research_contract(result, horizon, agent_key=role) == result


def test_upgrading_already_composed_research_prompt_removes_short_research_contract():
    short = append_research_contract("用户角色")
    long = append_research_contract(short, "long")
    assert "以及未来3/7个实际交易日的观察方案" not in long
    assert long.count("【研究输出与账户执行分离】") == 1


@pytest.mark.parametrize("invalid", ["", "year", "LONG", 12, True])
def test_unknown_period_is_rejected_instead_of_silently_running_short(invalid):
    with pytest.raises(ValueError, match="analysis_horizon"):
        append_research_contract("role", invalid)


@pytest.mark.parametrize("horizon,start", [("short", "2026-08-29"), ("medium", "2026-04-16"), ("long", "2025-10-13")])
def test_real_registry_injection_uses_selected_period_without_changing_symbol_or_cutoff(horizon, start):
    context = {"ticker": "002241", "trade_date": "2026-10-13", "analysis_horizon": horizon}
    daily = _resolve_inject_args(get_spec_by_id("daily_quotes"), context)
    technical = _resolve_inject_args(get_spec_by_id("technical_analysis"), context)
    assert daily == {"symbol": "002241", "start_date": start, "end_date": "2026-10-13"}
    assert technical["analysis_horizon"] == horizon
    assert technical["analysis_date"] == "2026-10-13"
    assert context["analysis_horizon"] == horizon


def test_medium_and_long_memories_do_not_reuse_short_or_each_other():
    assert horizon_memory_name("bull_memory") == "bull_memory"
    assert horizon_memory_name("bull_memory", "short") == "bull_memory"
    assert {horizon_memory_name("bull_memory", value) for value in ("short", "medium", "long")} == {
        "bull_memory", "bull_memory_medium", "bull_memory_long"}


def test_long_report_retains_conditional_valuation_and_existing_hold_without_daily_schedule():
    content = "长线6–12个月研究：当前未持仓观望，已有持仓持有。基准情景估值区间18–22元，需现金流与盈利兑现。按季度财报复查，经营逻辑失效时提前评估退出。"
    conditional = [{"condition": "财报确认现金流改善并进入合理估值区间后重评", "valuation_range": "18–22元"}]
    submission = validate_submission("terminal", content, {
        "action": "持有", "target_price": None, "confidence_label": "中",
        "decision_semantics": {"account_position": "HELD", "unheld_action": "WATCH", "held_action": "HOLD",
                               "total_position_limit_pct": None, "conditional_strategy": conditional}})
    decision = normalize_decision({**submission, **submission["decision_semantics"]}, content, account_position="HELD")
    summary = validate_submission("summarizer", content, {
        "analysis_summary": content, "investment_recommendation": "每季度财报复查，条件失效则重新评估",
        "key_indicators": {"target_price": "6–12个月基准情景18–22元，非必达目标", "stop_loss": "经营逻辑失效后重评"},
        "model_confidence": None, "risk_assessment": {"level": "Medium", "score": None, "description": "盈利兑现风险"},
        "final_signal": "Hold"})
    summary["analysis_horizon"] = "long"
    result = synchronize_summary(summary, decision)
    _require_valid_text(content, "中长线报告", "交易员")
    _require_valid_summary(result)
    assert decision["held_action"] == "HOLD"
    assert decision["total_position_limit_pct"] is None
    assert result["key_indicators"] == summary["key_indicators"]
    assert result["analysis_horizon"] == "long"
    assert result["model_confidence"] is None
    assert result["decision_semantics"]["conditional_strategy"] == conditional


def test_export_keeps_frozen_horizon_and_does_not_depend_on_later_form_selection():
    state = {"analysis_horizon": "medium", "analysis_context": {"analysis_horizon": "medium"},
             "_plan_snapshot": {"analysis_horizon": "medium"}, "reports": {"trader_report": "中线研究"}}
    original = deepcopy(state)
    exported = export_legacy_state(state)
    assert exported["analysis_horizon"] == "medium"
    assert exported["_plan_snapshot"]["analysis_horizon"] == "medium"
    assert state == original
