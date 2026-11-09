"""Local privacy preflight over real historical materials; no external calls."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from app.llm.jev_review import (
    FINAL_EXCERPT_BYTES, ORDINARY_EXCERPT_BYTES, REPORT_KEYS,
    _public_excerpt, build_review_state,
)


@pytest.fixture(scope="module")
def actual_reports():
    path = os.environ.get("REPORT_EVIDENCE_SNAPSHOT")
    if not path:
        pytest.skip("Requires the user-authorized private report snapshot")
    data = json.loads(Path(path).read_text())
    assert len(data["reports"]) == 3
    return data


@pytest.mark.parametrize("symbol", ["600997", "600977", "300124"])
def test_actual_public_plan_is_complete_but_not_a_review_pass(actual_reports, symbol):
    report = next(row for row in actual_reports["reports"] if row["stock_symbol"] == symbol)
    source = {"reports": report["reports"], "structured_summary": report["structured_summary"]}
    before = deepcopy(source)
    result = build_review_state(source, symbol, "2026-10-10")
    projection = result["public_strategy"]
    assert projection["sent_complete"] and not projection["unsafe_omission"]
    assert not projection["private_dependency"]
    assert projection["fields"]["exit"]["status"] == "source_keyword_evidence"
    assert projection["fields"]["total_limit"]["status"] == "source_keyword_evidence"
    assert "missing_risk_conditions" not in result["check_materials"]
    assert "decision_conflict" not in result["check_materials"]
    # Truncated news and noncomparable flow sources still cannot be certified.
    assert "subject_mismatch" in result["check_materials"]
    assert "flow_conflict" in result["check_materials"]
    text = json.dumps(result, ensure_ascii=False)
    assert "NONE" not in text and "账户状态" not in text
    for section, excerpt in result["report_excerpts"].items():
        budget = FINAL_EXCERPT_BYTES if section == "final_trade_decision" else (
            ORDINARY_EXCERPT_BYTES if section in REPORT_KEYS else 1000
        )
        assert len(excerpt.encode()) <= budget
    assert "answers" not in result  # No model verdict is fabricated locally.
    assert source == before


def test_delimited_state_omission_preserves_public_policy_not_identity():
    plan = (
        "账户状态：NONE，未持仓｜最终动作：观望（WATCH）｜当前新增及总仓位上限均为0%\n"
        "未来条件方案总仓位上限未明确。\n"
        "退出条件：跌破21.30元停止执行并重新核验。"
    )
    state = {"reports": {"final_trade_decision": plan},
             "structured_summary": {"investment_recommendation": "观望，不新增。"}}
    result = build_review_state(state, "300094", "2026-10-10")
    assert not {"missing_risk_conditions", "decision_conflict"} & result["check_materials"].keys()
    assert "NONE" not in json.dumps(result)
    assert "0%" in result["report_excerpts"]["final_trade_decision"]
    assert "未明确" in result["report_excerpts"]["final_trade_decision"]


@pytest.mark.parametrize("line", [
    "当前持仓20600股，成交量低于100000股后退出。",
    "未来按成本3.613元下方5%退出。",
    "账户状态：HELD；只有保持既有成本条件才可退出。",
    "账户状态：NONE，止损按实际持仓决定｜当前观望。",
    "当前实际持有比例8%；按未来规则退出。",
    "成交量100000股，我有2000股；跌破21.30元退出。",
    "账户状态：NONE｜当前持仓20600股，跌破21.30元退出。",
])
def test_mixed_private_values_never_become_public_future_variables(line):
    state = {"reports": {"final_trade_decision": "当前策略观望。\n" + line},
             "structured_summary": {"investment_recommendation": "观望。"}}
    result = build_review_state(state, "300094", "2026-10-10")
    assert "missing_risk_conditions" in result["check_materials"]
    assert "decision_conflict" in result["check_materials"]
    payload = json.dumps(result)
    for value in ("20600", "2000", "3.613", "HELD", "NONE"):
        assert value not in payload


def test_public_exchange_volume_and_corporate_cash_are_not_personal_balances():
    text = (
        "回调日成交量低于突破日51,682,301股；仅为未来研究条件。\n"
        "集团货币资金中存款占76.31%；报告期末现金余额不等于即时可用资金。"
    )
    safe, shortened, removed = _public_excerpt(text, 5000)
    assert "51,682,301股" in safe and "76.31%" in safe
    assert not shortened and removed == 0


def test_unscoped_private_cost_formula_remains_blocked():
    state = {"reports": {"final_trade_decision": "当前策略观望。\n止损按成本价下方5%执行。"}}
    result = build_review_state(state, "300094", "2026-10-10")
    assert "missing_risk_conditions" in result["check_materials"]


def test_clear_future_symbolic_protection_does_not_reveal_a_cost_value():
    safe, shortened, removed = _public_excerpt(
        "退出条件：未来持仓采用实际加权成本下方3%与原止损中更严格者；成本价未知不编造价位。", 5000,
    )
    assert "3%" in safe and "入场变量未知" in safe
    assert "实际加权成本" not in safe and "成本价" not in safe
    assert not shortened and removed == 0
