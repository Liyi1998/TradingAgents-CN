"""Dated long-window, financial derivation and native intraday pure contracts."""
from copy import deepcopy
from datetime import date, timedelta

import pandas as pd
import pytest

from app.data.query.valuation_evidence import summarize_valuation_history, circulating_value_evidence
from app.data.sources.cn.akshare.api.financial import _build_financial_row
from app.data.sources.cn.tencent.api.intraday_quotes import parse_intraday_payload


def observations():
    days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(120)]
    rows = [{"trade_date": day, "pb": float(i + 1), "pe_ttm": float(i + 10),
             "field_provenance": {field: {"as_of": day, "date_verified": True,
                  "data_source": "baostock", "method": "reported_daily", "basis": basis}
                for field, basis in (("pb", "vendor_pb"), ("pe_ttm", "ttm"))}}
            for i, day in enumerate(days)]
    return rows, days  # supplied calendar observations, not weekday inference


def test_complete_dated_single_basis_window_has_120_observations():
    rows, days = observations()
    original = deepcopy(rows)
    result = summarize_valuation_history(rows, days)
    assert rows == original
    assert result["fields"]["pb"]["observation_count"] == 120
    assert result["fields"]["pe_ttm"]["status"] == "available"
    assert result["fields"]["pb"]["sample_percentile_pct"] == 100


@pytest.mark.parametrize("damage", ["date", "basis", "source", "nonfinite", "conflict", "missing"])
def test_incomplete_or_conflicting_evidence_never_makes_a_full_percentile(damage):
    rows, days = observations()
    if damage == "date": rows[0]["field_provenance"]["pb"]["as_of"] = days[1]
    elif damage == "basis": rows[0]["field_provenance"]["pb"]["basis"] = "different"
    elif damage == "source": rows[0]["field_provenance"]["pb"]["data_source"] = "other"
    elif damage == "nonfinite": rows[0]["pb"] = float("nan")
    elif damage == "conflict": rows.append({**deepcopy(rows[0]), "pb": 555})
    elif damage == "missing": rows.pop(0)
    result = summarize_valuation_history(rows, days)
    assert result["fields"]["pb"]["status"] == "insufficient_history"
    assert result["fields"]["pb"]["sample_percentile_pct"] is None


def test_nonpositive_pe_is_not_called_a_cheap_valuation():
    rows, days = observations()
    rows[-1]["pe_ttm"] = -9
    result = summarize_valuation_history(rows, days)
    assert result["fields"]["pe_ttm"]["status"] == "not_applicable_nonpositive"
    assert result["fields"]["pe_ttm"]["sample_percentile_pct"] is None
    assert result["fields"]["pb"]["status"] == "available"


def test_circulating_value_requires_its_own_observation_date_and_conflict_free_value():
    day = "2026-10-09"
    row = {"trade_date": day, "circ_mv": 10000000,
        "field_provenance": {"circ_mv": {"as_of": day, "date_verified": True,
            "data_source": "tencent", "method": "tencent_quote_timestamp", "basis": "vendor_reported"}}}
    assert circulating_value_evidence([row], day)["value"] == 10000000
    assert circulating_value_evidence([row], "2026-10-08")["status"] == "unavailable"
    assert circulating_value_evidence([row, {**row, "circ_mv": 2}], day)["value"] is None


def income(**changes):
    return {"REPORT_DATE": "2026-06-30", "NOTICE_DATE": "2026-08-20",
        "OPERATE_INCOME": 1000, "OPERATE_COST": 550, "BASIC_EPS": -.2,
        "provider_channel": "eastmoney_statement", **changes}


def test_gross_margin_uses_dated_same_period_operating_cost_not_total_expenses():
    tables = {"income": pd.DataFrame([income(TOTAL_OPERATE_COST=900)]),
        "abstract": pd.DataFrame([{"报告期": "2026-06-30", "销售毛利率": 99,
            "provider_channel": "ths_abstract"}])}
    result = _build_financial_row("600000", tables, "2026-06-30")
    assert result["gross_margin"] == 45
    proof = result["field_provenance"]["gross_margin"]
    assert proof["announce_date"] == "2026-08-20"
    assert proof["method"] == "derived_income_revenue_cost"
    assert proof["inputs"]["operating_cost"] == 550
    assert result["eps"] == -.2


@pytest.mark.parametrize("changes", [{"NOTICE_DATE": None}, {"REPORT_DATE": "2025-06-30"},
    {"OPERATE_COST": None}, {"OPERATE_COST": -1}, {"OPERATE_INCOME": 0},
    {"金额单位": "万元"}, {"CURRENCY": "USD"}])
def test_financial_derivation_cannot_borrow_disclosure_or_unverified_units(changes):
    result = _build_financial_row("600000", {"income": pd.DataFrame([income(**changes)])}, "2026-06-30")
    assert result["gross_margin"] is None


def test_native_bar_preserves_true_extrema_and_does_not_invent_turnover():
    payload = {"data": {"sz000503": {"m30": [["202610091500", "8.45", "8.43", "8.49", "8.39", "80554", {}, "82.26"]]}}}
    rows = parse_intraday_payload(payload, "000503")
    assert rows[0]["high"] == 8.49
    assert rows[0]["low"] == 8.39
    assert rows[0]["volume"] == 8055400
    assert rows[0]["amount"] is None
    assert parse_intraday_payload(payload, "603232") == []
    from app.data.sources.cn.tencent.adapter import TencentCNAdapter
    stored = TencentCNAdapter().adapt_intraday_quotes(rows)[0].to_db_doc()
    assert "amount" in stored and stored["amount"] is None
    assert "amount_unit" in stored and stored["amount_unit"] is None


@pytest.mark.parametrize("row", [["bad", 1, 1, 1, 1, 1], ["202610091500", 1, 2, 1, 1, 1],
    ["202610091500", 1, 1, 1, 1, -1], ["202610091500", 1, "nan", 1, 1, 1]])
def test_invalid_native_candles_are_not_certified(row):
    assert parse_intraday_payload({"data": {"sz000503": {"m30": [row]}}}, "000503") == []


def test_legacy_tencent_close_samples_are_not_native_ohlc():
    from app.engine.tools.datasources.market import _prepare_intraday_bars
    row = {"data_source": "tencent", "datetime": "2026-10-09 15:00:00", "freq": "1min",
        "open": 8.43, "high": 8.43, "low": 8.43, "close": 8.43, "volume": 10, "amount": 84.3}
    assert _prepare_intraday_bars([row], "30min") is None


def test_akshare_intraday_keeps_target_identity_units_and_real_zero_volume():
    from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
    row = {"code": "000503", "时间": "2026-10-09 15:00:00", "freq": "30min",
        "开盘": 8.45, "收盘": 8.43, "最高": 8.49, "最低": 8.39,
        "成交量": 0, "成交额": 0, "ohlc_kind": "native_bar", "price_basis": "raw",
        "volume_unit": "share", "amount_unit": "CNY"}
    stored = AKShareCNAdapter().adapt_intraday_quotes([row])[0].to_db_doc()
    assert stored["symbol"] == "000503" and stored["freq"] == "30min"
    assert stored["volume"] == stored["amount"] == 0
    assert stored["ohlc_kind"] == "native_bar" and stored["volume_unit"] == "share"
    assert AKShareCNAdapter().adapt_intraday_quotes([{k:v for k,v in row.items() if k != "code"}]) == []


def test_daily_cross_check_preserves_prices_but_discloses_missing_turnover():
    from app.data.query.intraday_evidence import compare_intraday_daily
    from app.data.sources.cn.tencent.adapter import TencentCNAdapter
    day = "2026-10-09"
    bars = [{"datetime": day + " 15:00:00", "open": 10., "high": 11., "low": 9., "close": 10.,
             "volume": 1000., "amount": 10000., "amount_unit": "CNY", "data_source": "akshare"}]
    daily = [{"trade_date": day, "open": 10., "high": 11., "low": 9., "close": 10.,
              "volume": 1050., "amount": 10500., "amount_unit": "CNY", "volume_unit": "share",
              "price_basis": "raw", "data_source": "akshare", "period": "daily",
              "unit_conversion": "akshare:stock_zh_a_daily/sina;volume:share->share;amount:CNY->CNY"}]
    original = deepcopy(bars)
    result = compare_intraday_daily(bars, daily, [day])
    assert bars == original
    assert result["status"] == "partial"
    assert result["checks"][0]["price_mismatches"] == []
    assert result["checks"][0]["volume_difference"] == -50
    assert result["checks"][0]["full_session_vwap_verified"] is False
    daily[0]["volume"] = 1000.; daily[0]["amount"] = 10000.
    matched = compare_intraday_daily(bars, daily, [day])
    assert matched["status"] == "matched" and matched["checks"][0]["full_session_vwap_verified"] is True
