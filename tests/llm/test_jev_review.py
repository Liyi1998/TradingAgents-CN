"""Real parser/filesystem tests; live provider test is explicitly opt-in (-m ai)."""

import copy
import json

import pytest

from app.llm.jev_review import (
    CONFIG_PATH, REPORT_KEY, REPORT_KEYS, JevReviewError, attach_review,
    build_review_state, evaluate, load_config, render_review, review_questions,
    review_reports, validate_response, input_guards,
)


def test_missing_config_is_disabled(tmp_path):
    config = load_config(tmp_path / "absent.json")
    assert not config.enabled


@pytest.mark.asyncio
async def test_disabled_does_not_change_state(tmp_path):
    state = {"reports": {"market_report": "Original report"}, "final_trade_decision": "HOLD"}
    before = copy.deepcopy(state)
    await attach_review(state, "300094", "2026-09-22", config_path=tmp_path / "absent.json")
    assert state == before


@pytest.mark.asyncio
async def test_missing_key_is_unavailable_not_success(tmp_path):
    path = tmp_path / "review.json"
    path.write_text(json.dumps({"enabled": True, "api_key": ""}))
    state = {"reports": {"market_report": "公开行情数据"}, "final_trade_decision": "WATCH"}
    before = copy.deepcopy(state)
    await attach_review(state, "300094", "2026-09-22", config_path=path)
    assert state["jev_review"]["status"] == "unavailable"
    assert state["final_trade_decision"] == before["final_trade_decision"]
    assert state["reports"]["market_report"] == before["reports"]["market_report"]
    assert "复核未完成" in state["reports"][REPORT_KEY]
    assert "answers" not in state["jev_review"]


@pytest.mark.asyncio
async def test_invalid_config_never_exposes_input(tmp_path):
    path = tmp_path / "review.json"
    path.write_text('{"api_key":"private-test-value", "enabled":"false"}')
    result = await review_reports({}, "300094", "2026-09-22", config_path=path)
    assert result["status"] == "unavailable"
    assert "private-test-value" not in json.dumps(result)
    assert "private-test-value" not in render_review(result)


def test_payload_allowlist_redaction_and_byte_budget():
    state = {
        "api_key": "credential-not-to-send",
        "messages": ["user-private-message"],
        "account_position": {"cost": 3.613, "shares": 20600},
        "reports": {key: "公开报告。" * 1000 for key in REPORT_KEYS},
        "structured_summary": {"analysis_summary": "公开概要", "investment_recommendation": "等待确认"},
    }
    state["reports"]["market_report"] += "\n持有20600股，成本3.613\nAPI Key: credential-not-to-send"
    state["reports"]["custom_private_report"] = "private-custom-note"
    state["reports"][REPORT_KEY] = "recursive-old-review"
    result = build_review_state(state, "300094", "2026-09-22")
    text = json.dumps(result, ensure_ascii=False)
    for private in ("credential-not-to-send", "20600", "3.613", "user-private-message", "private-custom-note", "recursive-old-review"):
        assert private not in text
    assert sum(len(value.encode("utf-8")) for value in result["report_excerpts"].values()) <= 77000
    assert "market_report" in result["truncated_sections"]
    assert "market_report" in result["redacted_sections"]


def test_strategy_conditions_survive_but_personal_facts_do_not():
    state = {"reports": {"final_trade_decision": "\n".join([
        "总仓位上限5%，当前新增上限0%；不得执行未授权策略。",
        "后续策略仓位不超过账户净资产5%，另获授权并补齐材料后才可复评。",
        "退出条件：有效跌破21.30元后停止执行，重新核验。",
        "当前持仓20600股，成本3.613；策略上限5%。",
        "账户净资产100000元；止损条件另行讨论。",
        "成本3.613；退出时重新评估。",
        "持有占比8%；未授权退出策略。",
        "账户净资产888888；授权条件另行讨论。",
        "账户状态已持仓；禁止新策略。",
        "现有仓位权重8%；另行授权退出。",
        "联系方式13812345678、user@example.com；授权后执行。",
        "Authorization: Bearer private-test-value",
    ])}}
    result = build_review_state(state, "300094", "2026-09-22")
    excerpt = result["report_excerpts"]["final_trade_decision"]
    for public in ("总仓位上限5%", "当前新增上限0%", "策略资金基数5%", "另获授权", "21.30元"):
        assert public in excerpt
    for private in ("20600", "3.613", "100000", "13812345678", "user@example.com", "private-test-value", "888888", "持有占比8%", "现有仓位", "账户"):
        assert private not in excerpt
    assert "final_trade_decision" in result["redacted_sections"]


def test_complete_final_plan_can_cover_shortened_earlier_plan():
    guards = input_guards({"sections": ["final_trade_decision", "trader_investment_plan", "summary_investment_recommendation"],
                           "truncated_sections": ["trader_investment_plan"], "redacted_sections": []})
    assert "missing_risk_conditions" not in guards


def _response_document():
    # Parser input, not a mocked client or simulated network response.
    return {
        "model": "jev-1.13.0",
        "answers": {
            key: {"type": "choice", "choice": "unknown", "confidence": 0.4,
                  "probabilities": {"issue": 0.2, "not_evident": 0.2, "unknown": 0.6}}
            for key in review_questions()
        },
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


def test_typed_response_and_render_uncertainty():
    result = validate_response(_response_document(), review_questions())
    result.update(status="completed", elapsed_seconds=0.2)
    rendered = render_review(result)
    assert "材料不足" in rendered and "需人工核对" in rendered
    assert "不是上涨概率" in rendered
    assert "规则：report-review-v1.3" in rendered


def test_input_guards_reject_high_confidence_for_incomplete_material():
    result = validate_response(_response_document(), review_questions())
    result.update(status="completed", elapsed_seconds=0.2, scope={
        "sections": ["final_trade_decision", "trader_investment_plan"],
        "truncated_sections": ["final_trade_decision"],
        "redacted_sections": ["trader_investment_plan"],
    })
    # Captured behavior from the real trial: the model disregarded the completeness instruction.
    result["answers"]["missing_risk_conditions"] = {
        "type": "choice", "choice": "not_evident", "confidence": 0.85,
        "probabilities": {"unknown": 0.09, "not_evident": 0.9, "issue": 0.01},
    }
    before = copy.deepcopy(result)
    assert set(input_guards(result["scope"])) == {"missing_risk_conditions", "decision_conflict"}
    rendered = render_review(result)
    row = next(line for line in rendered.splitlines() if line.startswith("| 最终计划"))
    assert "材料不足 | —" in row and "85.0%" not in row
    assert result == before  # Raw provider answers are retained unchanged for audit.


def test_complete_material_does_not_add_guards():
    assert input_guards({
        "sections": ["final_trade_decision", "trader_investment_plan", "summary_investment_recommendation"],
        "truncated_sections": [], "redacted_sections": [],
    }) == {}


@pytest.mark.parametrize("bad", [None, {}, {"answers": {}}, {"model": "not-jev", "answers": {}}])
def test_invalid_responses_are_rejected(bad):
    with pytest.raises(JevReviewError):
        validate_response(bad, review_questions())


@pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan"), float("inf"), True, "0.8"])
def test_invalid_confidence_rejected(bad):
    payload = _response_document()
    payload["answers"]["data_gaps"]["confidence"] = bad
    with pytest.raises(JevReviewError):
        validate_response(payload, review_questions())


def test_missing_answer_and_unexpected_distribution_rejected():
    payload = _response_document()
    del payload["answers"]["data_gaps"]
    with pytest.raises(JevReviewError):
        validate_response(payload, review_questions())
    payload = _response_document()
    payload["answers"]["data_gaps"]["probabilities"]["invented"] = 0
    with pytest.raises(JevReviewError):
        validate_response(payload, review_questions())
    payload = _response_document()
    payload["answers"]["data_gaps"]["choice"] = "issue"
    with pytest.raises(JevReviewError):
        validate_response(payload, review_questions())


@pytest.mark.ai
@pytest.mark.asyncio
async def test_live_typesafe_evaluation():
    config = load_config(CONFIG_PATH)
    if not config.enabled or not config.api_key.get_secret_value().strip():
        pytest.skip("Fill local config/jev_review.json to run the real API test")
    state = build_review_state({
        "reports": {"market_report": "300094：历史行情获取失败，暂不作技术判断。"},
        "final_trade_decision": "仅观察。若官方数据仍不可用，重新核验，不作交易决策。",
        "structured_summary": {"investment_recommendation": "仅观察，不作交易决策。"},
    }, "300094", "2026-09-22")
    result = await evaluate(config, state)
    eligible = set(review_questions()) - set(state["check_materials"])
    assert set(result["answers"]) == eligible
    assert result["model"].startswith("jev-")
