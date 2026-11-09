"""Public-condition replay: semantics, units, session boundaries and partial proof."""
from copy import deepcopy

import pytest

from app.engine.intraday_conditions import evaluate_condition
from app.engine.intraday_research import evaluate
from tests.engine.test_intraday_research import DAY, native_rows, snapshot


@pytest.mark.parametrize("text,state", [
    ("日线收盘价跌破10元或盘中击穿9.5元硬性止损底线", "met"),
    ("日线收盘价高于10元且当日成交量超过1万股", "not_met"),
    ("当日成交量不低于1.08万股", "met"),
    ("当日成交量超过1.08万股", "not_met"),
    ("30分钟成交量不低于1700股", "met"),
    ("当日累计成交量超过1万股", "met"),
    ("30分钟K线收阴线", "met"),
    ("30分钟K线收阳线", "not_met"),
    ("30分钟成交量超过前两根均量的1.1倍", "not_met"),
    ("30分钟成交量不低于前两根均量的1倍", "met"),
    ("股价触及10.5元", "met"),
    ("盘中触及10.45至10.5元阻力带", "met"),
    ("盘中价格跌破9.6元", "not_met"),
    ("股价未跌破10元", "unsupported"),
    ("日线成交量低于9000万元", "unsupported"),
    ("股价触及-3元", "unsupported"),
    ("当日主力净流出低于-500万元", "unsupported"),
    ("2026-11-04收盘价高于10元", "unsupported"),
    ("30分钟收盘价高于9元或低于8元且公告无风险", "unsupported"),
    ("当日成交额超过1亿元", "missing_data"),
    ("单日主力资金净流入超过2000万元", "missing_data"),
])
def test_complete_rules_and_rejected_ambiguity(text, state):
    assert evaluate_condition(text, snapshot())["state"] == state


@pytest.mark.parametrize("text", ["当日成交量不超过2万股", "日线收盘价高于10元", "当日成交额超过1亿元", "主力净流入超过100万元"])
def test_daily_rules_wait_for_close_even_if_intraday_threshold_met(text):
    assert evaluate_condition(text, snapshot(stamp="11:31:00"))["state"] == "pending_close"


def test_missing_vs_session_waiting_and_no_zero_imputation():
    assert evaluate_condition("30分钟收盘价高于10元", snapshot(stamp="09:59:00"))["state"] == "pending_session"
    assert evaluate_condition("30分钟收盘价高于10元", snapshot(rows=[]))["state"] == "missing_data"
    assert evaluate_condition("主力净流入低于100万元", snapshot())["state"] == "missing_data"


def test_supplements_require_same_day_verified_values_and_correct_direction():
    s = snapshot()
    s["supplementary"] = {"main_flow": {"date": DAY, "verified": True, "value": -4e6, "source": "akshare"},
                          "day_amount": {"date": DAY, "verified": True, "value": 2e8, "source": "tencent"}}
    assert evaluate_condition("主力净流出不超过500万元", s)["state"] == "met"
    assert evaluate_condition("主力净流入超过100万元", s)["state"] == "not_met"
    assert evaluate_condition("当日成交额超过1亿元", s)["state"] == "met"
    for change in ({"date": "2026-11-04"}, {"verified": False}, {"value": float("nan")}, {"value": True}):
        bad = deepcopy(s)
        bad["supplementary"]["main_flow"].update(change)
        assert evaluate_condition("主力净流出不超过500万元", bad)["state"] == "missing_data"


@pytest.mark.parametrize("text", [
    "日K线收盘价站稳MA10阻力位7.14元之上",
    "日线成交量缩减至9000万股以下且30分钟K线收企稳阳线",
    "股价盘中回踩22.68-23.20元支撑区间获得承接",
    "上市公司公告重大诉讼、监管立案调查或大额资产减值等重大黑天鹅利空",
    "深交所披露的11-04两融数据显示融资买入占成交额比例低于12%且融资净卖出未超5000万元",
])
def test_actual_compound_prose_never_becomes_full_approval(text):
    result = evaluate_condition(text, snapshot())
    assert result["state"] == "unsupported"
    assert result["unsupported"]


def test_partial_numeric_proof_does_not_prove_stability_or_shrinkage():
    result = evaluate_condition("日线成交量缩减至9000万股以下且30分钟K线收企稳阳线", snapshot())
    partials = [c for c in result["checks"] if c.get("partial")]
    assert any(c["state"] == "met" and "成交量上限" in c["reason"] for c in partials)
    assert any("企稳缺少" in c["reason"] for c in partials)
    assert result["state"] == "unsupported"


def test_dated_anchor_and_pullback_only_prove_numeric_subconditions():
    for text in ["股价跌破2026-11-04日内回踩低点13.31元，弱反抽结构衰竭", "股价盘中回踩9.7-9.8元支撑区间获得承接"]:
        result = evaluate_condition(text, snapshot())
        assert result["state"] == "unsupported"
        assert any(c.get("partial") and c["state"] == "met" for c in result["checks"])
    result = evaluate_condition("上市公司公告重大诉讼", snapshot())
    assert "未接入公告或新闻事实核验" in result["checks"][0]["reason"]


def test_actual_or_stop_rule_can_prove_intraday_breach_without_waiting_for_close():
    text = "日线收盘价跌破21.60元或盘中击穿21.40元硬性止损底线"
    intraday = evaluate_condition(text, snapshot(stamp="14:31:00"))
    assert intraday["state"] == "met" and intraday["checks"][0]["state"] == "pending_close"
    assert evaluate_condition(text, snapshot())["state"] == "met"


def test_day_touch_not_lost_when_last_interval_is_elsewhere():
    level = {"kind": "resistance", "lower": 10.5, "upper": 10.6, "price_basis": "raw"}
    result = evaluate(snapshot(), [level], [{"kind": "veto", "text": "公告存在重大诉讼", "state": "met"}])
    row = result["conditions"][0]
    assert row["state"] == "not_triggered" and row["day_touched"] is True
    assert row["first_touch_at"] == DAY+" 11:30:00"
    assert result["conditions"][1]["previous_state"] == "met"
    assert result["conditions"][1]["state"] == "unsupported"
    assert not result["trade_authorized"]


def test_prefix_rule_does_not_use_future_high_or_daily_volume():
    partial = snapshot(native_rows()[:2], "10:31:00")
    full_input = snapshot(stamp="10:31:00")
    for text in ["股价触及10.5元", "日线成交量超过1万股", "30分钟收盘价突破10元"]:
        assert evaluate_condition(text, partial) == evaluate_condition(text, full_input)
    assert evaluate_condition("股价触及10.5元", partial)["state"] == "not_met"
