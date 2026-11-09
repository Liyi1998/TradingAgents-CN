"""Deterministic source/projection/privacy tests; no provider calls or mocks."""
import copy
import json

import pytest

from app.llm.jev_review import (
    FINAL_EXCERPT_BYTES, REPORT_KEYS, JevReviewError, _public_excerpt, build_review_state,
    input_guards, review_questions, review_reports, validate_response,
)
from app.llm.jev_public_strategy import project_public_strategy


def _state(plan, summary="当前观望，不提前买入。"):
    return {"reports": {"final_trade_decision": plan},
            "structured_summary": {"investment_recommendation": summary}}


def _guards(review):
    return input_guards({**review, "sections": list(review["report_excerpts"])})


def test_only_independent_personal_facts_allow_risk_check():
    plan = "\n".join([
        "当前持仓20600股，成本3.613", "账户净资产100000元", "账户状态已持仓",
        "当前策略：观望，不提前买入。",
        "总仓位上限5%；当前新增仓位上限0%。",
        "后续策略仅在另获授权并补齐资料后才可执行；禁止提前执行。",
        "退出条件：跌破21.30元时停止执行并重新核验。",
    ])
    source = _state(plan)
    before = copy.deepcopy(source)
    review = build_review_state(source, "300094", "2026-10-08")
    guards = _guards(review)
    assert "missing_risk_conditions" not in guards
    assert "decision_conflict" not in guards
    assert "final_trade_decision" in review["redacted_sections"]
    projection = review["public_strategy"]
    assert projection["private_fact_lines_omitted"] == 3
    assert projection["sent_complete"]
    assert projection["fields"]["exit"]["status"] == "source_keyword_evidence"
    payload = json.dumps(review, ensure_ascii=False)
    for private in ("20600", "3.613", "100000", "账户状态", "已持仓"):
        assert private not in payload
    assert source == before


@pytest.mark.parametrize("line", [
    "当前持仓20600股；跌破21.30元退出。",
    "账户净资产100000元，只有确认后才可执行。",
    "根据实际持仓决定退出比例。",
    "止损按成本价下方5%执行。",
    "授权状态未知，退出条件另行讨论。",
    "沿用上述账户条件后才可复评。",
    "密钥 sk-private；满足条件后授权。",
    "退出策略详见风险管理报告。",
    "[中间内容省略，仅复核首尾片段]",
])
def test_mixed_private_dependencies_unknown_authorization_never_clear_gate(line):
    review = build_review_state(_state("当前策略：观望。\n" + line), "300094", "2026-10-08")
    guards = _guards(review)
    assert "missing_risk_conditions" in guards
    assert "decision_conflict" in guards
    assert "20600" not in json.dumps(review)


def test_full_source_absent_risk_is_inspectable_not_missing_material():
    review = build_review_state(_state("当前策略：买入。"), "300094", "2026-10-08")
    assert review["public_strategy"]["fields"]["exit"]["status"] == "complete_source_no_keyword_evidence"
    assert "missing_risk_conditions" not in _guards(review)
    assert "neither proves semantic presence nor semantic absence" in review_questions()["missing_risk_conditions"]["instructions"]


def test_summary_cannot_certify_missing_or_redacted_final_source():
    review = build_review_state(_state("当前持仓20600股；退出另行讨论。", "当前观望，跌破21.30元停止执行。"), "300094", "2026-10-08")
    assert "missing_risk_conditions" in _guards(review)
    state = {"structured_summary": {"investment_recommendation": "观望，跌破21.30元退出。", "decision_semantics": {"unheld_action": "WATCH", "exit_conditions": ["完整"]}}}
    review = build_review_state(state, "300094", "2026-10-08")
    assert review["public_strategy"] is None
    assert "missing_risk_conditions" in _guards(review)


def test_ambiguous_final_cap_not_replaced_by_summary_or_default():
    state = _state("当前策略观望。\n总仓位上限5%。\n总仓位上限10%。\n跌破21.30元退出。")
    state["decision"] = {"total_position_limit_pct": 5, "unheld_action": "BUY"}
    review = build_review_state(state, "300094", "2026-10-08")
    assert review["public_strategy"]["fields"]["total_limit"]["status"] == "unknown"
    assert "decision_conflict" in _guards(review)
    assert "missing_risk_conditions" not in _guards(review)


def test_projection_never_turns_default_watch_into_action_evidence():
    review = build_review_state(_state("最新行情数据待补齐。"), "300094", "2026-10-08")
    assert "missing_risk_conditions" in _guards(review)
    assert "decision_conflict" in _guards(review)


def test_projection_budget_overflow_keeps_guard_without_extra_payload():
    state = _state("当前策略：观望。\n" + "公开市场说明。" * 1800 + "\n跌破21.30元停止执行。\n当前持仓20600股")
    review = build_review_state(state, "300094", "2026-10-08")
    assert len(review["report_excerpts"]["final_trade_decision"].encode()) <= FINAL_EXCERPT_BYTES
    assert not review["public_strategy"]["sent_complete"]
    assert "missing_risk_conditions" in _guards(review)
    assert "20600" not in json.dumps(review)


def test_generalized_action_headings_are_not_actual_holdings():
    text = "账户持仓状态：已持仓\n- **未持仓动作**: 观望（WATCH）\n- **已持仓动作**: 减仓（REDUCE）"
    safe, truncated, removed = _public_excerpt(text, 5000)
    assert "账户" not in safe and "已持仓" not in safe and "未持仓" not in safe
    assert "新增策略动作：观望（WATCH）" in safe
    assert "存量策略动作：减仓（REDUCE）" in safe
    assert removed == 1 and not truncated


def test_ambiguous_holding_statement_not_generalized_without_branch_label():
    safe, _, removed = _public_excerpt("已持仓：持有\n未持仓：观望", 5000)
    assert safe == "" and removed == 2


@pytest.mark.parametrize("private", ["http://192.168.1.17:8080", "http://10.1.2.3", "http://172.20.3.1", "http://service.internal/a", "http://host.local:8080", "http://[::1]:8080"])
def test_internal_addresses_never_leave_in_strategy_lines(private):
    review = build_review_state(_state("当前策略观望。\n退出条件见 " + private), "300094", "2026-10-08")
    assert private not in json.dumps(review, ensure_ascii=False)
    assert "missing_risk_conditions" in _guards(review)


def test_public_projection_only_transmits_safe_source_evidence():
    projection = project_public_strategy("总仓位上限5%。\n当前持仓20600股，成本3.613\n跌破21.30元退出。", "final_trade_decision", _public_excerpt)
    assert projection["fields"]["total_limit"]["value_pct"] == 5
    assert projection["fields"]["total_limit"]["evidence"][0]["line"] == 1
    encoded = json.dumps(projection, ensure_ascii=False)
    assert "20600" not in encoded and "3.613" not in encoded


def test_all_transmitted_quoted_text_stays_in_original_allowlist_budgets():
    state = {"reports": {key: "公开材料。" * 1200 for key in REPORT_KEYS},
             "structured_summary": {"analysis_summary": "概要。" * 1000, "investment_recommendation": "当前观望。" * 1000}}
    state["reports"]["final_trade_decision"] = "当前策略观望。\n退出条件：跌破21.30元停止执行。"
    state["reports"]["private_custom"] = "do-not-send"
    review = build_review_state(state, "300094", "2026-10-08")
    assert set(review["report_excerpts"]) <= set(REPORT_KEYS) | {"summary_analysis_summary", "summary_investment_recommendation"}
    assert sum(len(value.encode()) for value in review["report_excerpts"].values()) <= 77000
    assert "do-not-send" not in json.dumps(review)


def test_preflight_question_selection_and_subset_response_validation():
    review = build_review_state(_state("当前策略观望。\n跌破21.30元退出。"), "300094", "2026-10-08")
    guards = _guards(review)
    assert "subject_mismatch" in guards and "flow_conflict" in guards
    questions = review_questions(set(review_questions()) - set(guards))
    assert set(questions) == {"data_gaps", "missing_risk_conditions", "decision_conflict"}
    document = {"model": "jev-1.13.0", "answers": {key: {"type": "choice", "choice": "unknown", "confidence": 0.5,
        "probabilities": {"unknown": 1.0, "issue": 0.0, "not_evident": 0.0}} for key in questions}}
    assert set(validate_response(document, questions)["answers"]) == set(questions)
    document["answers"].pop("missing_risk_conditions")
    with pytest.raises(JevReviewError, match="缺项"):
        validate_response(document, questions)


@pytest.mark.parametrize("reports, blocked", [
    ({"financial_news_report": "300094 最新行情价格10元。"}, "subject_mismatch"),
    ({"financial_news_report": "其他公司的新闻报道，业绩增长。"}, "subject_mismatch"),
    ({"financial_news_report": "300094：新闻摘要。" * 1000}, "subject_mismatch"),
    ({"short_term_capital_report": "东方财富和同花顺资金流方向相反。"}, "flow_conflict"),
    ({"short_term_capital_report": "2026-10-07 东方财富主力净流入100万元；2026-10-08 同花顺主力净流出100万元。"}, "flow_conflict"),
    ({"short_term_capital_report": "2026-10-07 东方财富主力净流入100万元；2026-10-07 同花顺大单净流出100万元。"}, "flow_conflict"),
    ({"short_term_capital_report": "2026-10-07 东方财富主力净流入100万元；2026-10-07 东方财富主力净流出100万元。"}, "flow_conflict"),
])
def test_module_presence_cannot_self_certify_identity_or_flow_comparability(reports, blocked):
    state = _state("当前策略观望。\n跌破21.30元退出。")
    state["reports"].update(reports)
    review = build_review_state(state, "300094", "2026-10-08")
    assert blocked in _guards(review)


def test_explicit_identity_and_same_period_metric_sources_are_inspectable_not_verified():
    state = _state("当前策略观望。\n跌破21.30元退出。")
    state["reports"].update({
        "financial_news_report": "300094：公司公告报道其经营情况，以下新闻需核对主体。",
        "short_term_capital_report": "2026-10-07 东方财富主力净流入100万元；2026-10-07 同花顺主力净流出100万元。",
    })
    review = build_review_state(state, "300094", "2026-10-08")
    assert "subject_mismatch" not in _guards(review)
    assert "flow_conflict" not in _guards(review)
    assert "answers" not in review  # Only the reviewer may judge the alleged issue.


@pytest.mark.asyncio
async def test_no_material_skips_external_call_even_without_key(tmp_path):
    path = tmp_path / "review.json"
    path.write_text(json.dumps({"enabled": True, "api_key": ""}))
    result = await review_reports({}, "300094", "2026-10-08", config_path=path)
    assert result["status"] == "skipped"
    assert result["external_call"] is False
    assert result["material_status"] == "insufficient_input"
    assert result["eligible_checks"] == []
    assert set(result["answers"]) == set(review_questions())
    assert all(answer["choice"] == "unknown" and answer["origin"] == "local_input_guard" for answer in result["answers"].values())
