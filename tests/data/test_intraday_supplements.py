"""Reject stale, conflicting and ambiguous funds/turnover evidence."""
import pytest

from app.data.query.intraday_research import verified_daily_supplement

DAY = "2026-11-05"
FLOW = dict(symbol="000503", trade_date=DAY, amount_unit="CNY", data_source="akshare",
            main_net_inflow=-4e6, flow_basis="eastmoney_order_size_v1", flow_date_verified=True,
            flow_date_basis="source_row_date")


@pytest.mark.parametrize("change", [
    {"symbol": "002241"}, {"trade_date": "2026-11-04"}, {"amount_unit": "万元"},
    {"data_source": "sina"}, {"flow_basis": "large_orders"}, {"flow_date_verified": False},
    {"flow_date_basis": "requested_date"}, {"main_net_inflow": None},
    {"main_net_inflow": float("nan")}, {"main_net_inflow": True},
])
def test_invalid_funds_proof_is_missing_not_zero(change):
    value = verified_daily_supplement("000503", DAY, "money_flow", [{**FLOW, **change}])
    assert not value["verified"] and value["value"] is None


def test_same_day_source_proof_and_conflicts():
    assert verified_daily_supplement("000503", DAY, "money_flow", [FLOW])["value"] == -4e6
    assert not verified_daily_supplement("000503", DAY, "money_flow", [FLOW, {**FLOW, "main_net_inflow": 1}])["verified"]
    amount = dict(symbol="000503", trade_date=DAY, amount_unit="CNY", data_source="tencent", price_basis="raw", amount=2e8)
    assert verified_daily_supplement("000503", DAY, "daily_quotes", [amount])["value"] == 2e8
    assert not verified_daily_supplement("000503", DAY, "daily_quotes", [{**amount, "price_basis": "qfq"}])["verified"]
