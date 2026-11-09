"""Money-flow contract regressions: real adapters and an isolated real MongoDB.

No provider/network replacement or simulated database. External provider acceptance
is a separately bounded run; pure table inputs here exercise parsing edge cases.
"""

from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from app.data.processor.validator import Validator
from app.data.schema.domains.money_flow import (
    MONEY_FLOW_SNAPSHOT_FIELDS,
    MoneyFlowSchema,
    normalize_money_flow_symbol,
)
from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
from app.data.sources.cn.akshare.api.money_flow import (
    bind_money_flow_identity,
    fetch_money_flow_rank,
    money_flow_market,
)
from app.data.sources.cn.akshare.provider import _filter_by_date
from app.data.sources.cn.sina.adapter import SinaCNAdapter
from app.data.sources.cn.sina.api.money_flow import _prefix
from app.data.sources.cn.tushare.adapter import TushareCNAdapter
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.repositories.money_flow_repo import MoneyFlowRepo


def eastmoney_doc(**overrides):
    values = dict(
        symbol="002241", market="CN", data_source="akshare",
        trade_date="2026-09-30", main_net_inflow=0.0,
        flow_basis="eastmoney_order_size_v1", flow_source="eastmoney",
        amount_unit="CNY", flow_pct_unit="percent",
        flow_date_verified=True, flow_date_basis="source_row_date",
    )
    values.update(overrides)
    return MoneyFlowSchema(**values).to_db_doc()


@pytest.mark.parametrize("symbol,market", [
    ("600875", "sh"), ("688001.SH", "sh"), ("900901", "sh"),
    ("002241", "sz"), ("000503.SZ", "sz"), ("300094", "sz"),
    ("430047", "bj"), ("832000", "bj"), ("920001.BJ", "bj"),
])
def test_market_identity_includes_bse(symbol, market):
    code, actual = money_flow_market(symbol)
    assert actual == market
    assert code == symbol.split(".")[0]
    assert _prefix(symbol) == market + code


@pytest.mark.parametrize("value", [None, "", " ", "000000", 0, True, False,
                                         "nan", "not-a-stock", "1234567", "００２２４１"])
def test_missing_identity_is_never_000000(value):
    with pytest.raises(ValueError):
        normalize_money_flow_symbol(value)


def test_identity_suffix_must_agree_with_exchange():
    for fn in (money_flow_market, _prefix):
        with pytest.raises(ValueError):
            fn("920001.SH")


def test_individual_history_binds_symbol_without_relabeling_dates():
    dates = ["2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28",
             "2026-09-29", "2026-09-30", "2026-10-08"]
    original = pd.DataFrame({"日期": dates, "主力净流入-净额": range(len(dates))})
    bound = bind_money_flow_identity(original, "002241.SZ")
    assert "symbol" not in original
    assert list(bound["日期"]) == dates
    filtered = _filter_by_date(bound, "2026-09-24", "2026-09-30", date_cols=["日期"])
    records = AKShareCNAdapter().adapt_money_flow(filtered)
    assert len(records) == 5
    assert {x.symbol for x in records} == {"002241"}
    assert [x.trade_date for x in records] == dates[1:6]
    assert all(x.flow_date_verified is True for x in records)
    assert all(x.flow_date_basis == "source_row_date" for x in records)
    assert len(Validator().validate([x.to_db_doc() for x in records], "money_flow", "CN")[0]) == 5


def test_individual_response_identity_mismatch_and_missing_dates_are_rejected():
    with pytest.raises(ValueError):
        bind_money_flow_identity(pd.DataFrame([{"symbol": "000503", "日期": "2026-09-30"}]), "002241")
    with pytest.raises(ValueError):
        bind_money_flow_identity(pd.DataFrame([{"主力净流入-净额": 1}]), "002241")
    with pytest.raises(ValueError):
        bind_money_flow_identity(pd.DataFrame([{"日期": None, "主力净流入-净额": 1}]), "002241")
    assert AKShareCNAdapter().adapt_money_flow(pd.DataFrame([{"日期": "2026-09-30", "主力净流入-净额": 1}])) == []


def test_zero_is_not_replaced_by_alias_or_none():
    frame = pd.DataFrame([{
        "symbol": "002241", "日期": "2026-09-30", "主力净流入-净额": 0,
        "main_net_inflow": 987654, "主力净流入-净占比": 0,
        "main_net_pct": 99, "超大单净流入-净额": 0,
        "大单净流入-净额": 0, "中单净流入-净额": 0, "小单净流入-净额": 0,
    }])
    item = AKShareCNAdapter().adapt_money_flow(frame)[0]
    assert item.main_net_inflow == item.main_net_inflow_pct == 0
    assert item.huge_net_inflow == item.large_net_inflow == 0
    assert item.medium_net_inflow == item.small_net_inflow == 0
    assert item.flow_source == "eastmoney"
    assert item.amount_unit == "CNY"


def test_today_rank_prefix_maps_but_capture_date_is_not_history():
    item = AKShareCNAdapter().adapt_money_flow(pd.DataFrame([{
        "代码": "002241", "日期": "2026-09-30", "今日主力净流入-净额": 100,
        "今日主力净流入-净占比": 0, "今日大单净流入-净额": -10,
        "今日超大单净流入-净额": 110,
    }]))[0]
    assert item.main_net_inflow == 100
    assert item.main_net_inflow_pct == 0
    assert item.large_net_inflow == -10
    assert item.flow_date_verified is False
    assert item.flow_date_basis == "capture_date"
    assert item.flow_api == "stock_individual_fund_flow_rank"


async def test_rank_never_relabels_an_arbitrary_historical_day():
    from app.data.sources.base.exceptions import DataNotFoundError
    with pytest.raises(DataNotFoundError):
        await fetch_money_flow_rank("1999-01-04")


@pytest.mark.parametrize("value", [True, False, np.bool_(True)])
def test_boolean_source_amount_is_not_converted_to_money(value):
    item = AKShareCNAdapter().adapt_money_flow(pd.DataFrame([{
        "symbol": "002241", "日期": "2026-09-30", "主力净流入-净额": value,
    }]))[0]
    assert item.main_net_inflow is None
    assert not Validator().validate([item.to_db_doc()], "money_flow", "CN")[0]


@pytest.mark.parametrize("value", [True, np.bool_(True), "10", float("nan"), float("inf"), -float("inf")])
def test_validator_rejects_nonfinite_bool_and_unnormalized_money(value):
    record = eastmoney_doc()
    record["main_net_inflow"] = value
    valid, errors = Validator().validate([record], "money_flow", "CN")
    assert not valid and errors


@pytest.mark.parametrize("changes", [
    {"symbol": "000000"}, {"symbol": "1"}, {"trade_date": "2026-02-30"},
    {"main_net_inflow": None}, {"flow_date_verified": "True"},
    {"flow_date_verified": 1}, {"amount_unit": "万元"},
    {"flow_source": "sina"}, {"main_net_inflow_pct": 101},
])
def test_validator_rejects_invalid_contract(changes):
    record = eastmoney_doc()
    record.update(changes)
    assert not Validator().validate([record], "money_flow", "CN")[0]


def test_sina_and_tushare_are_not_eastmoney_main_flow():
    sina = SinaCNAdapter().adapt_money_flow(pd.DataFrame([{
        "symbol": "002241", "opendate": "2026-09-30", "netamount": 0,
        "r0_net": -4, "ratioamount": 0.025,
    }]))[0]
    assert sina.flow_basis == "sina_large_order_v1"
    assert sina.main_net_inflow == 0
    assert sina.main_net_inflow_pct is None
    assert sina.reported_flow_ratio == 0.025
    assert sina.reported_flow_ratio_unit == "unverified"
    assert sina.to_db_doc()["large_net_inflow"] is None
    tushare = TushareCNAdapter().adapt_money_flow(pd.DataFrame([{
        "ts_code": "002241.SZ", "trade_date": "20260930", "net_mf_amount": 1.25,
    }]))[0]
    assert tushare.main_net_inflow == 12500
    assert tushare.flow_basis == "tushare_moneyflow_net_v1"
    assert tushare.amount_unit == "CNY"


def test_schema_emits_all_snapshot_nulls_for_source_replacement():
    item = MoneyFlowSchema(symbol="002241", market="CN", data_source="unknown",
                           trade_date="2026-09-30", main_net_inflow=0)
    doc = item.to_db_doc()
    assert set(MONEY_FLOW_SNAPSHOT_FIELDS) <= doc.keys()
    assert doc["flow_basis"] is None and doc["flow_source"] is None
    assert doc["main_net_inflow"] == 0


async def test_real_mongo_source_switch_clears_old_flow_fields(real_mongo_db):
    # Refuse production/non-test DBs; never delete a pre-existing document.
    assert any(word in real_mongo_db.name.lower() for word in ("test", "validation"))
    coll = real_mongo_db[get_collection_name("money_flow", "CN")]
    key = {"symbol": "920998", "trade_date": "1999-01-04"}
    assert await coll.find_one(key) is None
    marker = uuid4().hex
    repo = MoneyFlowRepo()
    first = eastmoney_doc(**key, main_net_inflow=0, main_net_inflow_pct=12,
                          huge_net_inflow=2, large_net_inflow=3, small_net_inflow=4,
                          unit_conversion="previous-source-conversion")
    first["test_run_id"] = marker
    try:
        await repo.upsert_many([first], "CN")
        assert (await coll.find_one(key))["main_net_inflow_pct"] == 12
        # Dict caller deliberately omits optional fields, bypassing Schema.to_db_doc.
        second = dict(key, market="CN", data_source="sina", main_net_inflow=-7,
                      flow_basis="sina_large_order_v1", flow_source="sina",
                      amount_unit="CNY", flow_date_verified=True,
                      flow_date_basis="source_row_date", test_run_id=marker)
        await repo.upsert_many([second], "CN")
        actual = await coll.find_one(key)
        assert actual["data_source"] == "sina"
        assert actual["main_net_inflow"] == -7
        for field in ("main_net_inflow_pct", "huge_net_inflow", "large_net_inflow",
                      "medium_net_inflow", "small_net_inflow", "flow_pct_unit", "flow_api"):
            assert actual[field] is None
        assert actual["unit_conversion"] is None
        assert actual["flow_basis"] == "sina_large_order_v1"
        # Invalid replacement must not erase the existing valid source snapshot.
        await repo.upsert_many([dict(second, main_net_inflow=True)], "CN")
        assert (await coll.find_one(key))["main_net_inflow"] == -7
    finally:
        await coll.delete_many(dict(key, test_run_id=marker))
