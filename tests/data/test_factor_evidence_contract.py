"""Numerical contract checks; no provider/database replacements."""
import copy

import numpy as np
import pandas as pd
import pytest

from app.data.factors.engine import FactorScoreEngine

ENGINE = FactorScoreEngine()
ASOF = "2026-09-30"


def valuation_rows():
    rows = []
    for day in pd.bdate_range(end=ASOF, periods=125):
        date = day.date().isoformat()
        rows.append(dict(symbol="000001", trade_date=date, pe_ttm=20, pb=2,
            dividend_yield=2, dividend_yield_ttm=3, volume_ratio=1.2,
            field_provenance={field: dict(data_source="akshare", as_of=date,
                date_verified=True, method="dated_history", basis=basis)
                for field, basis in dict(pe_ttm="ttm", pb="vendor_pb",
                    dividend_yield="annual", dividend_yield_ttm="ttm", volume_ratio="reported").items()}))
    return rows


def test_unverified_legacy_valuation_is_not_a_trusted_percentile():
    rows = valuation_rows()
    for row in rows:
        row.pop("field_provenance")
    got = ENGINE._valuation_from_frame(pd.DataFrame(rows), ASOF).iloc[0]
    assert pd.isna(got.pe_ttm_percentile)
    assert got._pe_history_days == 0
    assert pd.isna(got.dividend_yield)


def test_actual_sessions_filter_calendar_filled_valuation_history():
    rows = valuation_rows()
    got = ENGINE._valuation_from_frame(pd.DataFrame(rows), ASOF,
        valid_dates=[row["trade_date"] for row in rows[-119:]]).iloc[0]
    assert got._pe_history_days == 119
    assert pd.isna(got.pe_ttm_percentile)


def test_mixed_valuation_source_does_not_complete_window():
    rows = valuation_rows()
    for row in rows[:10]:
        row["field_provenance"]["pe_ttm"]["data_source"] = "other"
    got = ENGINE._valuation_from_frame(pd.DataFrame(rows), ASOF).iloc[0]
    assert got._pe_history_days == 115
    assert pd.isna(got.pe_ttm_percentile)
    assert got.pb_percentile == 100


@pytest.mark.parametrize("change", [{"date_verified": False}, {"as_of": "2026-09-29"}, {"basis": "dynamic"}])
def test_current_pe_proof_must_match_date_and_ttm(change):
    rows = valuation_rows()
    rows[-1]["field_provenance"]["pe_ttm"].update(change)
    got = ENGINE._valuation_from_frame(pd.DataFrame(rows), ASOF).iloc[0]
    assert pd.isna(got.pe_ttm_percentile)


def test_ttm_dividend_preferred_with_explicit_basis_and_real_zero():
    rows = valuation_rows()
    rows[-1]["dividend_yield_ttm"] = 0
    got = ENGINE._valuation_from_frame(pd.DataFrame(rows), ASOF).iloc[0]
    assert got.dividend_yield == 0
    assert got._dividend_yield_basis == "ttm"


def financial_row():
    row = dict(symbol="000001", report_period="2026-06-30", announce_date="2026-10-15",
        statement_type="indicator", report_type="consolidated_ytd", data_source="akshare",
        financial_contract="financial_disclosed_fields_v1", net_profit=100, roe=10,
        gross_margin=30, debt_ratio=40, net_profit_basis="attributable")
    row["field_provenance"] = {field: dict(data_source="akshare", report_period="2026-06-30",
        provider_channel="eastmoney_indicator", method="reported",
        announce_date="2026-08-15", status="available", basis="attributable" if field == "net_profit" else "reported")
        for field in ("net_profit", "roe", "gross_margin", "debt_ratio")}
    row["field_provenance"]["debt_ratio"]["announce_date"] = "2026-10-15"
    return row


def test_each_financial_field_uses_own_disclosure_not_composite_date():
    row = financial_row()
    got = ENGINE._quality_from_frame(pd.DataFrame([row]), ASOF).iloc[0]
    assert got.roe == 10
    assert got.gross_margin == 30
    assert pd.isna(got.debt_ratio)
    assert got._financial_announce_date == "2026-08-15"


@pytest.mark.parametrize("change", [{"announce_date": None}, {"report_period": "2025-06-30"},
    {"data_source": "other"}, {"status": "unproven"}, {"announce_date": "2026-01-01"}])
def test_unproven_financial_field_is_not_blessed_by_other_fields(change):
    row = financial_row()
    row["field_provenance"]["roe"].update(change)
    got = ENGINE._quality_from_frame(pd.DataFrame([row]), ASOF).iloc[0]
    assert pd.isna(got.roe)
    assert got.gross_margin == 30


@pytest.mark.parametrize("basis", [None, "", "unknown", "consolidated"])
def test_yoy_requires_same_known_profit_basis(basis):
    current = financial_row()
    previous = copy.deepcopy(current)
    previous.update(report_period="2025-06-30", announce_date="2025-08-15")
    for proof in previous["field_provenance"].values():
        proof.update(report_period="2025-06-30", announce_date="2025-08-15")
    previous["field_provenance"]["net_profit"]["basis"] = basis
    got = ENGINE._quality_from_frame(pd.DataFrame([previous, current]), ASOF).iloc[0]
    assert pd.isna(got.net_profit_yoy)


def test_missing_current_adjusted_close_does_not_reuse_previous_price():
    quotes = pd.DataFrame(dict(symbol="000001", trade_date=d.date().isoformat(),
        close_adj=float(i + 10), volume=100) for i, d in enumerate(pd.bdate_range(end=ASOF, periods=120)))
    quotes.loc[quotes.index[-1], "close_adj"] = np.nan
    got = ENGINE._compute_momentum_trend_volume(quotes).iloc[0]
    for field in ("ret_5d", "ret_20d", "ret_60d", "bias_ma20", "ma20_slope", "macd_golden_days"):
        assert pd.isna(got[field])


@pytest.mark.requires_db
async def test_invalid_security_type_never_publishes_industry_style_scores(real_mongo_db, seed_factor_inputs):
    from app.data.storage.mongo.collections import get_collection_name
    await real_mongo_db[get_collection_name("basic_info", "CN")].update_many(
        {"symbol": {"$in": seed_factor_inputs["symbols"]}}, {"$set": {"industry": "1"}})
    got = await ENGINE.compute_factors("CN")
    assert got["_industry_status"].eq("unverified").all()
    assert got["ret_60d"].notna().all()
    assert got[["score_short_term", "score_balanced", "score_value"]].isna().all().all()


@pytest.mark.parametrize("basis", [None, "qfq", "hfq"])
def test_adjusted_or_unknown_prices_cannot_quarantine_raw_turnover(basis):
    from app.data.core.coverage import evaluate
    rows, quality = evaluate([dict(trade_date=ASOF, price_basis=basis,
        open=10, close=10, low=9, high=11, volume=100, amount=2000,
        volume_unit="share", amount_unit="CNY")], "daily_quotes", None, ASOF)
    assert rows[0]["volume"] == 100
    assert rows[0]["amount"] == 2000
    assert not quality["unit_conflicts"]


def test_proven_raw_price_and_units_still_detect_amount_conflict():
    from app.data.core.coverage import evaluate
    rows, quality = evaluate([dict(trade_date=ASOF, price_basis="raw",
        open=10, close=10, low=9, high=11, volume=100, amount=2000,
        volume_unit="share", amount_unit="CNY")], "daily_quotes", None, ASOF)
    assert rows[0]["volume"] is None
    assert quality["unit_conflicts"]


@pytest.mark.parametrize("bad", [True, np.bool_(True), np.inf, -np.inf, -1])
def test_invalid_volume_cannot_win_the_volume_ranking(bad):
    quotes = pd.DataFrame(dict(symbol="000001", trade_date=d.date().isoformat(),
        close_adj=10.0, volume=100.0) for d in pd.bdate_range(end=ASOF, periods=120))
    quotes["volume"] = quotes["volume"].astype(object)
    quotes.loc[quotes.index[-1], "volume"] = bad
    got = ENGINE._compute_momentum_trend_volume(quotes).iloc[0]
    assert pd.isna(got.turnover_amp)
    assert pd.isna(got.consecutive_vol_up_days)
