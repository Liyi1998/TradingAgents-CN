"""Five-day factor contract: pure frames and isolated, real Mongo projection.

No provider calls, mocks, patched methods or simulated databases are used.
"""

from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from app.data.factors.engine import FactorScoreEngine
from app.data.storage.mongo.collections import get_collection_name


DATES = ["2026-09-30", "2026-09-29", "2026-09-28", "2026-09-25", "2026-09-24"]
SYMBOL = "002241"


def flow_frame(values=(0, 10, -5, 20, -10), dates=DATES, symbol=SYMBOL):
    return pd.DataFrame([
        dict(symbol=symbol, trade_date=day, main_net_inflow=value,
             data_source="akshare", flow_source="eastmoney",
             flow_basis="eastmoney_order_size_v1", amount_unit="CNY",
             flow_date_verified=True, flow_date_basis="source_row_date")
        for day, value in zip(dates, values)
    ])


def amount_frame(values=(100, 100, 100, 100, 100), dates=DATES, symbol=SYMBOL):
    return pd.DataFrame([
        dict(symbol=symbol, trade_date=day, amount=value, amount_unit="CNY")
        for day, value in zip(dates, values)
    ])


def compute(flow=None, amounts=None, dates=DATES):
    return FactorScoreEngine()._flow_from_frames(
        flow_frame() if flow is None else flow,
        amount_frame() if amounts is None else amounts, dates)


def test_five_verified_same_basis_days_preserve_real_zero():
    row = compute().loc[SYMBOL]
    assert row["main_inflow_5d"] == 15
    assert row["main_inflow_5d_pct"] == pytest.approx(3)
    assert row["_money_flow_days"] == 5
    zero = compute(flow_frame((0, 0, 0, 0, 0))).loc[SYMBOL]
    assert zero["main_inflow_5d"] == zero["main_inflow_5d_pct"] == 0


@pytest.mark.parametrize("field,value", [
    ("data_source", "sina"), ("data_source", None),
    ("flow_source", "sina"), ("flow_source", "unknown"),
    ("flow_basis", "sina_large_order_v1"),
    ("flow_basis", "tushare_moneyflow_net_v1"), ("flow_basis", None),
    ("amount_unit", "万元"), ("amount_unit", None),
    ("flow_date_verified", False), ("flow_date_verified", None),
    ("flow_date_verified", 1), ("flow_date_verified", "True"),
    ("flow_date_basis", "capture_date"), ("flow_date_basis", None),
])
def test_one_unverified_or_different_basis_day_invalidates_window(field, value):
    frame = flow_frame().astype({field: object})
    frame.at[2, field] = value
    row = compute(frame).loc[SYMBOL]
    assert pd.isna(row["main_inflow_5d"])
    assert pd.isna(row["main_inflow_5d_pct"])
    assert row["_money_flow_days"] == 4


@pytest.mark.parametrize("field", [
    "data_source", "flow_source", "flow_basis", "amount_unit",
    "flow_date_verified", "flow_date_basis", "main_net_inflow",
])
def test_legacy_missing_column_fails_closed(field):
    row = compute(flow_frame().drop(columns=field)).loc[SYMBOL]
    assert pd.isna(row["main_inflow_5d"])
    assert row["_money_flow_days"] == 0


@pytest.mark.parametrize("value", [True, False, np.bool_(True), "0", None,
                                   np.nan, np.inf, -np.inf])
def test_invalid_money_cannot_be_summed_as_a_valid_day(value):
    frame = flow_frame().astype({"main_net_inflow": object})
    frame.at[2, "main_net_inflow"] = value
    row = compute(frame).loc[SYMBOL]
    assert pd.isna(row["main_inflow_5d"])
    assert row["_money_flow_days"] == 4


def test_duplicates_never_supply_a_missing_day():
    frame = flow_frame().iloc[:4]
    frame = pd.concat([frame, frame.iloc[:1]], ignore_index=True)
    row = compute(frame).loc[SYMBOL]
    assert pd.isna(row["main_inflow_5d"])
    assert row["_money_flow_days"] == 4


@pytest.mark.parametrize("duplicate_change", [{}, {"main_net_inflow": 999},
                                              {"main_net_inflow": None},
                                              {"flow_source": "sina"}])
def test_duplicate_day_must_be_identical_and_valid(duplicate_change):
    frame = flow_frame()
    duplicate = frame.iloc[0].to_dict()
    duplicate.update(duplicate_change)
    row = compute(pd.concat([frame, pd.DataFrame([duplicate])], ignore_index=True)).loc[SYMBOL]
    if duplicate_change:
        assert pd.isna(row["main_inflow_5d"])
        assert row["_money_flow_days"] == 4
    else:
        assert row["main_inflow_5d"] == 15
        assert row["_money_flow_days"] == 5


@pytest.mark.parametrize("dates", [DATES[:4], [*DATES[:4], DATES[0]],
                                   [*DATES, DATES[0]], [*DATES[:4], "2026-02-30"]])
def test_recent_window_requires_five_distinct_real_dates(dates):
    assert pd.isna(compute(dates=dates).loc[SYMBOL, "main_inflow_5d"])


@pytest.mark.parametrize("changes", [
    {"amount_unit": "万元"}, {"amount_unit": None}, {"amount": True},
    {"amount": np.inf}, {"amount": None}, {"amount": -1},
])
def test_amount_contract_only_blocks_percentage(changes):
    frame = amount_frame().astype(object)
    for key, value in changes.items():
        frame.at[0, key] = value
    row = compute(amounts=frame).loc[SYMBOL]
    assert row["main_inflow_5d"] == 15
    assert pd.isna(row["main_inflow_5d_pct"])


def test_missing_or_zero_amount_denominator_is_not_a_percentage():
    for amounts in [pd.DataFrame(), amount_frame().iloc[:4],
                    amount_frame((0, 0, 0, 0, 0)),
                    amount_frame().drop(columns="amount_unit")]:
        row = compute(amounts=amounts).loc[SYMBOL]
        assert row["main_inflow_5d"] == 15
        assert pd.isna(row["main_inflow_5d_pct"])


def test_overflowed_aggregate_is_unavailable():
    row = compute(flow_frame((1e308,) * 5)).loc[SYMBOL]
    assert pd.isna(row["main_inflow_5d"])
    assert pd.isna(row["main_inflow_5d_pct"])


def test_out_of_window_rows_and_other_symbols_do_not_supply_days():
    frame = pd.concat([flow_frame(), flow_frame(dates=["2026-10-08"], values=[999]),
                       flow_frame(values=(5, 5, 5, 5), symbol="000503")], ignore_index=True)
    out = compute(frame)
    assert out.loc[SYMBOL, "main_inflow_5d"] == 15
    assert pd.isna(out.loc["000503", "main_inflow_5d"])


async def test_real_mongo_projection_keeps_contract_and_rejects_capture_only(real_mongo_db):
    assert any(word in real_mongo_db.name.lower() for word in ("test", "validation"))
    flows = real_mongo_db[get_collection_name("money_flow", "CN")]
    quotes = real_mongo_db[get_collection_name("daily_quotes", "CN")]
    dates = ["1999-01-08", "1999-01-07", "1999-01-06", "1999-01-05", "1999-01-04"]
    key = {"symbol": "920997", "trade_date": {"$in": dates}}
    assert await flows.find_one(key) is None
    assert await quotes.find_one(key) is None
    marker = uuid4().hex
    flow_docs = flow_frame(dates=dates, symbol=key["symbol"]).to_dict("records")
    quote_docs = amount_frame(dates=dates, symbol=key["symbol"]).to_dict("records")
    for doc in [*flow_docs, *quote_docs]:
        doc.update(market="CN", test_run_id=marker)
    for doc in quote_docs:
        doc.update(period="daily", data_source="akshare")
    engine = FactorScoreEngine()
    cleanup = dict(key, test_run_id=marker)
    try:
        await flows.insert_many(flow_docs)
        await quotes.insert_many(quote_docs)
        out = await engine._compute_flow("CN", dates)
        assert out.loc[key["symbol"], "main_inflow_5d"] == 15
        assert out.loc[key["symbol"], "main_inflow_5d_pct"] == pytest.approx(3)
        await flows.update_one(dict(symbol=key["symbol"], trade_date=dates[0], test_run_id=marker),
                               {"$set": {"flow_date_verified": False,
                                          "flow_date_basis": "capture_date"}})
        out = await engine._compute_flow("CN", dates)
        assert pd.isna(out.loc[key["symbol"], "main_inflow_5d"])
        assert out.loc[key["symbol"], "_money_flow_days"] == 4
    finally:
        await flows.delete_many(cleanup)
        await quotes.delete_many(cleanup)
