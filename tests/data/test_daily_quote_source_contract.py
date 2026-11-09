"""Real pure parsing contracts; no provider replacement or network traffic.

Unit evidence: https://akshare.akfamily.xyz/data/stock/stock.html
stock_zh_a_hist reports lots/元, stock_zh_a_daily reports shares/元.
The installed AKShare source parses both without a volume multiplier.
"""

import ast
import inspect
import textwrap

import numpy as np
import pandas as pd
import pytest

from app.data.sources.base.exceptions import DataFormatError
from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
from app.data.sources.cn.akshare.api.daily_quotes import (
    bind_daily_quote_source,
    fetch_daily_quotes,
    validate_daily_quote_options,
)
from app.data.sources.cn.akshare.provider import AKShareCNProvider


def source_frame(api="stock_zh_a_hist", **changes):
    if api == "stock_zh_a_hist":
        row = {"日期": "2026-09-30", "开盘": 10, "最高": 11, "最低": 9,
               "收盘": 10, "成交量": 12, "成交额": 12000,
               "换手率": 1.2, "涨跌额": 0, "涨跌幅": 0}
    else:
        row = {"date": "2026-09-30", "open": 10, "high": 11, "low": 9,
               "close": 10, "volume": 1200, "amount": 12000, "turnover": .012}
    row.update(symbol="000001", price_basis="raw", quote_period="daily")
    row.update(changes)
    return bind_daily_quote_source(pd.DataFrame([row]), source_api=api)


def adapt(frame):
    return AKShareCNAdapter().adapt_daily_quotes(frame)[0]


@pytest.mark.parametrize("adjust", ["", "qfq", "hfq"])
@pytest.mark.parametrize("period", ["daily", "weekly", "monthly"])
def test_supported_request_options_are_explicit(adjust, period):
    assert validate_daily_quote_options(period, adjust) is None


@pytest.mark.parametrize("adjust", [None, False, 0, "raw", "QFQ", "qfq-factor", []])
async def test_api_and_provider_reject_invalid_adjust_before_network(adjust):
    with pytest.raises(DataFormatError):
        await fetch_daily_quotes("000001", "2026-02-01", "2026-10-08", adjust=adjust)
    with pytest.raises(DataFormatError):
        await AKShareCNProvider().get_daily_quotes(
            "000001", "2026-02-01", "2026-10-08", adjust=adjust)


@pytest.mark.parametrize("period", [None, False, "minute", "", []])
def test_unsupported_period_is_not_silently_relabelled_daily(period):
    with pytest.raises(DataFormatError):
        validate_daily_quote_options(period, "")


def test_provider_defaults_to_raw_and_forwards_explicit_options():
    method = AKShareCNProvider.get_daily_quotes
    signature = inspect.signature(method)
    assert signature.parameters["adjust"].default == ""
    assert signature.parameters["period"].default == "daily"
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "fetch_daily_quotes"]
    assert len(calls) == 1
    kwargs = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    assert isinstance(kwargs["adjust"], ast.Name) and kwargs["adjust"].id == "adjust"
    assert isinstance(kwargs["period"], ast.Name) and kwargs["period"].id == "period"


def test_both_sources_agree_after_exactly_one_volume_conversion():
    eastmoney = adapt(source_frame())
    sina = adapt(source_frame("stock_zh_a_daily"))
    assert eastmoney.volume == sina.volume == 1200
    assert eastmoney.amount == sina.amount == 12000
    assert eastmoney.turnover_rate == sina.turnover_rate == pytest.approx(1.2)
    assert eastmoney.volume_unit == sina.volume_unit == "share"
    assert eastmoney.amount_unit == sina.amount_unit == "CNY"
    assert "eastmoney" in eastmoney.unit_conversion
    assert "sina" in sina.unit_conversion
    assert eastmoney.price_basis == sina.price_basis == "raw"


@pytest.mark.parametrize("api", ["stock_zh_a_hist", "stock_zh_a_daily"])
def test_re_adapting_standard_record_does_not_multiply_again(api):
    record = adapt(source_frame(api)).to_db_doc()
    again = adapt(pd.DataFrame([record]))
    assert again.volume == record["volume"] == 1200
    assert again.amount == record["amount"] == 12000
    assert again.unit_conversion == record["unit_conversion"]
    assert again.volume_unit == "share" and again.amount_unit == "CNY"


def test_source_binding_is_explicit_and_nonmutating():
    original = pd.DataFrame([{"成交量": 0, "成交额": 0}])
    result = bind_daily_quote_source(original, source_api="stock_zh_a_hist")
    assert "quote_source" not in original
    assert result.loc[0, "quote_source"] == "eastmoney"
    assert result.loc[0, "volume_unit"] == "lot100"
    with pytest.raises(DataFormatError):
        bind_daily_quote_source(original, source_api="unknown")


def test_zero_values_are_not_replaced_by_aliases():
    item = adapt(source_frame(volume=0, amount=0, open=0, high=0, low=0,
                              close=0, change=0, pct_chg=0, turnover_rate=0))
    for field in ("volume", "amount", "open", "high", "low", "close",
                  "change", "pct_chg", "turnover_rate"):
        assert getattr(item, field) == 0


@pytest.mark.parametrize("field,value", [
    ("quote_units_verified", "True"), ("quote_units_verified", 1),
    ("quote_units_verified", False), ("quote_source", "sina"),
    ("volume_unit", "share"), ("amount_unit", "万元"), ("quote_api", None),
])
def test_inconsistent_source_evidence_never_certifies_units(field, value):
    frame = source_frame().astype({field: object})
    frame.at[0, field] = value
    item = adapt(frame)
    assert item.volume_unit is None and item.amount_unit is None
    assert item.unit_conversion is None
    assert item.volume == 12  # no unproven conversion


def test_legacy_values_without_source_metadata_are_not_certified():
    frame = source_frame().drop(columns=["quote_api", "quote_source", "quote_units_verified"])
    item = adapt(frame)
    assert item.amount_unit is None and item.volume_unit is None


def test_sina_turnover_is_not_an_amount_fallback():
    frame = source_frame("stock_zh_a_daily").drop(columns="amount")
    item = adapt(frame)
    assert item.amount is None
    assert item.turnover_rate == pytest.approx(1.2)


@pytest.mark.parametrize("value", [True, np.bool_(True), float("inf"), -float("inf")])
def test_invalid_quantities_cannot_become_numbers(value):
    item = adapt(source_frame(volume=value, amount=value, close=value))
    assert item.volume is None and item.amount is None and item.close is None


def test_quote_period_is_not_relabelled():
    item = adapt(source_frame(quote_period="weekly"))
    assert item.period == "weekly"
