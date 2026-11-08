"""财务披露契约回归：纯函数与隔离 Mongo 真读写，不请求数据源、不 mock。"""

from copy import deepcopy
from datetime import date, datetime
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from app.data.schema.domains.financial_data import (
    financial_date,
    financial_number,
    financial_record_as_of,
)
from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
from app.data.sources.cn.akshare.api.financial import (
    _build_financial_row,
    _filter_df_by_report_period,
    _safe_float as financial_source_number,
)
from app.data.sources.cn.akshare.api.financial_disclosures import normalize_period_rows, period_request_params
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.repositories.financial_data_repo import FinancialDataRepo


CONTRACT = "financial_disclosed_fields_v1"
PERIOD = "2026-06-30"
AS_OF = "2026-09-30"


def test_default_balance_request_keeps_native_security_and_non_otc_filters():
    params = period_request_params("RPT_DMSK_FN_BALANCE", PERIOD)
    assert '(SECURITY_TYPE_CODE in ("058001001","058001008"))' in params["filter"]
    assert '(TRADE_MARKET_CODE!="069001017")' in params["filter"]
    assert "(REPORT_DATE='2026-06-30')" in params["filter"]


def test_explicit_balance_sample_only_narrows_native_market_scope():
    base = period_request_params("RPT_DMSK_FN_BALANCE", PERIOD)["filter"]
    sample = period_request_params("RPT_DMSK_FN_BALANCE", PERIOD, ["000001"])["filter"]
    assert sample == base + '(SECURITY_CODE in ("000001"))'


def test_cpd_request_preserves_its_original_period_only_universe():
    assert period_request_params("RPT_LICO_FN_CPD", PERIOD)["filter"] == "(REPORTDATE='2026-06-30')"


@pytest.mark.parametrize("symbols", [[], ["000001", "bad"], list(range(101))])
def test_sample_scope_cannot_be_empty_invalid_or_unbounded(symbols):
    with pytest.raises((TypeError, ValueError)):
        period_request_params("RPT_DMSK_FN_BALANCE", PERIOD, symbols)


def _proof(field, *, period=PERIOD, announced="2026-08-20", channel="eastmoney_indicator"):
    basis = ("attributable" if field in {"net_profit", "net_profit_attributable"}
             else "consolidated" if field == "net_profit_consolidated"
             else "deducted" if field == "net_profit_deducted" else "reported")
    return {
        "data_source": "akshare", "provider_channel": channel,
        "report_period": period, "announce_date": announced,
        "method": "reported",
        "basis": basis,
        "status": "available" if announced else "unproven_disclosure",
    }


def _record(*, symbol="600000", period=PERIOD, announced="2026-08-20", values=None):
    values = {"net_profit": 100.0, "roe": 12.0} if values is None else values
    return {
        "symbol": symbol, "market": "CN", "data_source": "akshare",
        "report_period": period, "statement_type": "indicator",
        "report_type": "consolidated_ytd", "financial_contract": CONTRACT,
        "announce_date": announced, "net_profit_basis": "attributable",
        "currency": "CNY", "monetary_unit": "元",
        "source_disclosures": {"indicator": {
            "report_period": period, "announce_date": announced,
            "provider_channel": "eastmoney_indicator", "included": True,
        }},
        "field_provenance": {
            field: _proof(field, period=period, announced=announced) for field in values
        },
        **values,
    }


def _indicator(**changes):
    return {
        "SECURITY_CODE": "600000", "REPORTDATE": PERIOD + " 00:00:00",
        "NOTICE_DATE": "2026-08-20 00:00:00", "UPDATE_DATE": "2026-10-09 00:00:00",
        "BASIC_EPS": 1.2, "TOTAL_OPERATE_INCOME": 1_000.0,
        "PARENT_NETPROFIT": 100.0, "NETPROFIT": 150.0,
        "BPS": 8.0, "WEIGHTAVG_ROE": 12.0, "XSMLL": 30.0,
        **changes,
    }


def _balance(**changes):
    return {
        "SECURITY_CODE": "600000", "REPORT_DATE": PERIOD + " 00:00:00",
        "NOTICE_DATE": "2026-08-22 00:00:00", "UPDATE_DATE": "2026-10-09 00:00:00",
        "TOTAL_ASSETS": 2_000.0, "TOTAL_LIABILITIES": 800.0,
        "TOTAL_EQUITY": 1_200.0, "MONETARYFUNDS": 250.0,
        "ACCOUNTS_RECE": 100.0, "DEBT_ASSET_RATIO": 40.0,
        **changes,
    }


@pytest.mark.parametrize("value", [
    True, False, np.bool_(True), np.bool_(False), np.nan,
    np.inf, -np.inf, "Infinity", "-Infinity", "NaN", None, "", 10 ** 400,
])
def test_financial_number_rejects_boolean_nonfinite_and_unrepresentable_values(value):
    assert financial_number(value) is None


@pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (-10, -10.0), ("125.5", 125.5)])
def test_financial_number_preserves_real_zero_losses_and_numeric_strings(value, expected):
    assert financial_number(value) == expected


@pytest.mark.parametrize("value", [
    "2026-09-30", "20260930", "2026-09-30 23:59:58",
    "2026-09-30T23:59:58", date(2026, 9, 30), datetime(2026, 9, 30, 23, 59, 58),
])
def test_financial_date_normalizes_actual_dates_and_datetimes(value):
    assert financial_date(value) == AS_OF


@pytest.mark.parametrize("value", [
    None, "", "2026-02-30", "20260230", "2026-13-01", "abcdefghij", True,
])
def test_financial_date_rejects_missing_impossible_and_nondate_values(value):
    assert financial_date(value) is None


def test_asof_rejects_invalid_cutoff_instead_of_disabling_the_gate():
    with pytest.raises(ValueError):
        financial_record_as_of(_record(), "2026-02-30")


@pytest.mark.parametrize("period", ["2026-12-31", "2026-02-30", None])
def test_future_or_unproven_report_period_is_not_a_disclosed_record(period):
    assert financial_record_as_of(_record(period=period), AS_OF) is None


@pytest.mark.parametrize("announced", [None, "2026-02-30", "2026-06-29", "2026-10-01"])
def test_no_usable_field_remains_when_disclosure_is_unknown_invalid_early_or_future(announced):
    assert financial_record_as_of(_record(announced=announced), AS_OF) is None


def test_legacy_record_still_requires_an_actual_disclosure_date():
    legacy = _record()
    legacy.pop("financial_contract")
    legacy.pop("field_provenance")
    assert financial_record_as_of(legacy, AS_OF)["net_profit"] == 100.0
    legacy["announce_date"] = None
    assert financial_record_as_of(legacy, AS_OF) is None


def test_future_field_does_not_hide_an_independently_disclosed_field():
    record = _record(values={"net_profit": 100.0, "debt_ratio": 40.0})
    record["announce_date"] = "2026-10-28"
    record["field_provenance"]["debt_ratio"] = _proof(
        "debt_ratio", announced="2026-10-28", channel="eastmoney_balance",
    )
    got = financial_record_as_of(record, AS_OF)
    assert got["net_profit"] == 100.0
    assert got["net_profit_basis"] == "attributable"
    assert got["debt_ratio"] is None
    assert got["announce_date"] == "2026-08-20"
    assert got["field_provenance"]["debt_ratio"]["status"] != "available"


def test_unknown_profit_does_not_borrow_another_fields_disclosure_or_keep_basis():
    record = _record()
    record["field_provenance"]["net_profit"] = _proof("net_profit", announced=None)
    got = financial_record_as_of(record, AS_OF)
    assert got["roe"] == 12.0
    assert got["net_profit"] is None
    assert got["net_profit_basis"] is None


def test_complete_disclosed_fields_keep_amounts_units_and_separate_profit_bases():
    record = _record(values={
        "net_profit": 100.0, "net_profit_attributable": 100.0,
        "net_profit_consolidated": 150.0, "net_profit_deducted": 80.0,
        "revenue": 1_000.0, "total_assets": 2_000.0, "roe": 12.0,
    })
    got = financial_record_as_of(record, datetime(2026, 9, 30, 12, 0, 0))
    for field in record["field_provenance"]:
        assert got[field] == record[field]
    assert got["net_profit"] != got["net_profit_consolidated"]
    assert got["currency"] == "CNY"
    assert got["monetary_unit"] == "元"
    assert got["financial_as_of"] == AS_OF


def test_zero_disclosed_profit_is_usable_and_retains_its_basis():
    got = financial_record_as_of(_record(values={"net_profit": 0.0}), AS_OF)
    assert got is not None
    assert got["net_profit"] == 0.0
    assert got["net_profit_basis"] == "attributable"


@pytest.mark.parametrize("value", [True, np.bool_(True), np.inf, -np.inf, "Infinity"])
def test_bad_field_number_is_trimmed_without_hiding_other_good_fields(value):
    got = financial_record_as_of(_record(values={"net_profit": value, "roe": 12.0}), AS_OF)
    assert got["net_profit"] is None
    assert got["roe"] == 12.0
    assert got["net_profit_basis"] is None


@pytest.mark.parametrize(("key", "value"), [
    ("report_period", "2025-06-30"), ("data_source", "other"),
    ("provider_channel", ""), ("provider_channel", "unknown"),
    ("method", ""), ("basis", ""),
    ("status", "unproven_disclosure"),
])
def test_field_proof_must_match_period_source_and_have_required_semantics(key, value):
    record = _record()
    record["field_provenance"]["net_profit"][key] = value
    got = financial_record_as_of(record, AS_OF)
    assert got["net_profit"] is None
    assert got["roe"] == 12.0


def test_asof_does_not_mutate_original_amounts_or_nested_provenance():
    record = _record()
    record["field_provenance"]["net_profit"]["announce_date"] = "2026-10-28"
    before = deepcopy(record)
    financial_record_as_of(record, AS_OF)
    assert record == before


def test_cpd_and_balance_use_their_own_notice_dates_never_update_dates():
    got = normalize_period_rows([_indicator()], [_balance()], PERIOD).iloc[0].to_dict()
    assert got["field_provenance"]["net_profit"]["announce_date"] == "2026-08-20"
    assert got["field_provenance"]["debt_ratio"]["announce_date"] == "2026-08-22"
    assert got["source_disclosures"]["indicator"]["announce_date"] == "2026-08-20"
    assert got["source_disclosures"]["balance"]["announce_date"] == "2026-08-22"
    assert got["announce_date"] == "2026-08-22"


def test_update_date_cannot_supply_a_missing_notice_date():
    got = normalize_period_rows([_indicator(NOTICE_DATE=None)], [], PERIOD).iloc[0].to_dict()
    assert got["field_provenance"]["net_profit"]["announce_date"] is None
    assert got["field_provenance"]["net_profit"]["status"] != "available"
    assert financial_record_as_of(got, AS_OF) is None


def test_named_cpd_parent_profit_does_not_use_consolidated_alias_and_keeps_zero():
    got = normalize_period_rows(
        [_indicator(PARENT_NETPROFIT=0.0, NETPROFIT=999.0)], [], PERIOD,
    ).iloc[0]
    assert got["net_profit"] == 0.0
    assert got["net_profit_attributable"] == 0.0
    assert got["net_profit_basis"] == "attributable"
    assert got["currency"] == "CNY" and got["monetary_unit"] == "元"


def test_asof_selects_the_disclosed_revision_before_a_future_channel_revision():
    old = _indicator(NOTICE_DATE="2026-08-20", PARENT_NETPROFIT=100.0)
    future = _indicator(NOTICE_DATE="2026-10-28", PARENT_NETPROFIT=900.0)
    got = normalize_period_rows([future, old], [], PERIOD, as_of=AS_OF).iloc[0]
    assert got["net_profit"] == 100.0
    assert got["field_provenance"]["net_profit"]["announce_date"] == "2026-08-20"


def test_equivalent_same_notice_date_rows_collapse_to_one_record():
    row = _indicator()
    got = normalize_period_rows([row, deepcopy(row)], [_balance()], PERIOD)
    assert len(got) == 1
    assert got.iloc[0]["net_profit"] == 100.0


def test_conflicting_same_notice_date_values_are_rejected():
    with pytest.raises(ValueError):
        normalize_period_rows(
            [_indicator(), _indicator(PARENT_NETPROFIT=999.0)], [], PERIOD,
        )


@pytest.mark.parametrize("value", [True, np.bool_(True), np.inf, "Infinity"])
def test_period_normalization_does_not_certify_invalid_vendor_amounts(value):
    got = normalize_period_rows([_indicator(PARENT_NETPROFIT=value)], [], PERIOD).iloc[0]
    assert pd.isna(got["net_profit"])
    assert got["field_provenance"]["net_profit"]["status"] != "available"


def test_single_stock_builder_selects_one_period_and_tracks_exact_statement_origin():
    tables = {
        "abstract": pd.DataFrame([{
            "报告期": PERIOD, "公告日期": "2026-08-15", "营业收入": "999万",
            "扣非净利润": 80.0, "净资产收益率": "12.5%", "provider_channel": "ths_abstract",
        }]),
        "income": pd.DataFrame([
            {"REPORT_DATE": "2026-09-30", "NOTICE_DATE": "2026-10-28", "PARENT_NETPROFIT": 999.0},
            {"REPORT_DATE": PERIOD, "NOTICE_DATE": "2026-08-20",
             "OPERATE_INCOME": 1_000.0, "PARENT_NETPROFIT": 100.0, "NETPROFIT": 150.0,
             "provider_channel": "eastmoney_statement"},
        ]),
    }
    got = _build_financial_row("600000", tables, PERIOD)
    assert got["report_period"] == PERIOD
    assert got["revenue"] == 1_000.0
    assert got["net_profit"] == got["net_profit_attributable"] == 100.0
    assert got["net_profit_consolidated"] == 150.0
    assert got["net_profit_deducted"] == 80.0
    assert got["field_provenance"]["revenue"]["provider_channel"] == "eastmoney_statement"
    assert got["field_provenance"]["revenue"]["announce_date"] == "2026-08-20"
    assert got["field_provenance"]["net_profit_deducted"]["provider_channel"] == "ths_abstract"


def test_single_stock_abstract_revenue_fallback_keeps_abstract_disclosure():
    tables = {
        "abstract": pd.DataFrame([{
            "报告期": PERIOD, "公告日期": "2026-08-15", "营业收入": "3.5亿",
            "provider_channel": "ths_abstract",
        }]),
        "income": pd.DataFrame([{
            "REPORT_DATE": PERIOD, "NOTICE_DATE": "2026-08-20",
            "PARENT_NETPROFIT": 100.0, "provider_channel": "eastmoney_statement",
        }]),
    }
    got = _build_financial_row("600000", tables, PERIOD)
    assert got["revenue"] == 350_000_000.0
    assert got["field_provenance"]["revenue"]["provider_channel"] == "ths_abstract"
    assert got["field_provenance"]["revenue"]["announce_date"] == "2026-08-15"


@pytest.mark.parametrize("unit_evidence", [{"币种": "USD"}, {"金额单位": "万元"}])
def test_single_stock_explicit_currency_or_table_unit_conflict_is_not_relabeled(unit_evidence):
    tables = {
        "income": pd.DataFrame([{
            "REPORT_DATE": PERIOD, "NOTICE_DATE": "2026-08-20",
            "PARENT_NETPROFIT": 100.0, "provider_channel": "sina_finance",
            **unit_evidence,
        }]),
    }
    got = _build_financial_row("600000", tables, PERIOD)
    assert got["net_profit"] is None
    assert got["net_profit_attributable"] is None
    assert got["field_provenance"]["net_profit"]["status"] == "unproven_unit"
    assert got["field_provenance"]["net_profit"]["currency"] is None
    assert got["field_provenance"]["net_profit"]["monetary_unit"] is None


@pytest.mark.parametrize(("value", "expected"), [
    ("3.5亿", 350_000_000.0), ("12万", 120_000.0), ("12.5%", 12.5),
    ("0%", 0.0), ("1,234.5", 1234.5),
])
def test_single_stock_source_conversion_keeps_amount_and_percentage_units(value, expected):
    assert financial_source_number(value) == expected


@pytest.mark.parametrize("value", [True, np.bool_(True), np.inf, "Infinity%", "Infinity万"])
def test_single_stock_source_conversion_rejects_boolean_and_nonfinite_values(value):
    assert financial_source_number(value) is None


def test_datetime_report_period_at_inclusive_cutoff_is_not_dropped():
    table = pd.DataFrame({
        "REPORT_DATE": ["2026-03-31 00:00:00", "2026-06-30 00:00:00", "2026-09-30 00:00:00"],
        "value": [1, 2, 3],
    })
    got = _filter_df_by_report_period(table, PERIOD, PERIOD)
    assert got["value"].tolist() == [2]


@pytest.mark.parametrize("value", [True, np.bool_(True), np.inf, -np.inf])
def test_financial_adapter_rejects_invalid_numbers_before_schema_conversion(value):
    raw = _record(values={"net_profit": value, "roe": 12.0})
    schema = AKShareCNAdapter().adapt_financial_data([raw])[0]
    assert schema.net_profit is None
    assert schema.roe == 12.0
    doc = schema.to_db_doc()
    assert doc["net_profit"] is None
    assert doc["field_provenance"] == raw["field_provenance"]


def test_complete_normalized_snapshot_survives_adapter_schema_and_asof_view():
    raw = normalize_period_rows([_indicator()], [_balance()], PERIOD)
    doc = AKShareCNAdapter().adapt_financial_data(raw)[0].to_db_doc()
    got = financial_record_as_of(doc, AS_OF)
    assert got["net_profit"] == 100.0 and got["total_assets"] == 2_000.0
    assert got["total_liabilities"] == 800.0
    assert got["debt_ratio"] == 40.0
    assert got["financial_contract"] == CONTRACT
    assert got["field_provenance"]["total_liabilities"]["announce_date"] == "2026-08-22"


@pytest.mark.requires_db
async def test_real_mongo_new_snapshot_cannot_inherit_old_fields_evidence_or_units(real_mongo_db):
    symbol = "TESTFIN" + uuid4().hex
    coll = real_mongo_db[get_collection_name("financial_data", "CN")]
    repo = FinancialDataRepo()
    try:
        await repo.upsert_many([_record(symbol=symbol, values={"net_profit": 100.0, "total_assets": 2_000.0})], "CN")
        await repo.upsert_many([{
            "symbol": symbol, "market": "CN", "data_source": "akshare",
            "report_period": PERIOD, "statement_type": "indicator",
            "financial_contract": CONTRACT, "net_profit": 999.0,
        }], "CN")
        got = await coll.find_one({"symbol": symbol})
        assert await coll.count_documents({"symbol": symbol}) == 1
        assert got["net_profit"] == 999.0
        assert got["total_assets"] is None and got["announce_date"] is None
        assert got["field_provenance"] == {} and got["source_disclosures"] == {}
        assert got["currency"] is None and got["monetary_unit"] is None
        assert await repo.get_by_symbol(symbol, "CN", end_date=AS_OF) == []
    finally:
        await coll.delete_many({"symbol": symbol})


@pytest.mark.requires_db
async def test_real_mongo_legacy_writer_cannot_inherit_a_previous_v1_contract(real_mongo_db):
    symbol = "TESTFIN" + uuid4().hex
    coll = real_mongo_db[get_collection_name("financial_data", "CN")]
    repo = FinancialDataRepo()
    try:
        await repo.upsert_many([_record(symbol=symbol)], "CN")
        await repo.upsert_many([{
            "symbol": symbol, "market": "CN", "data_source": "akshare",
            "report_period": PERIOD, "statement_type": "indicator", "net_profit": 999.0,
        }], "CN")
        got = await coll.find_one({"symbol": symbol})
        assert got["financial_contract"] is None
        assert got["field_provenance"] == {} and got["source_disclosures"] == {}
        assert got["announce_date"] is None and got["net_profit_basis"] is None
        assert got["currency"] is None and got["monetary_unit"] is None
        assert await repo.get_by_symbol(symbol, "CN", end_date=AS_OF) == []
    finally:
        await coll.delete_many({"symbol": symbol})


@pytest.mark.requires_db
async def test_real_mongo_legacy_disclosure_cannot_recertify_an_old_missing_field(real_mongo_db):
    symbol = "TESTFIN" + uuid4().hex
    coll = real_mongo_db[get_collection_name("financial_data", "CN")]
    repo = FinancialDataRepo()
    try:
        await repo.upsert_many([_record(symbol=symbol, values={"net_profit": 100.0, "roe": 99.0})], "CN")
        await repo.upsert_many([{
            "symbol": symbol, "market": "CN", "data_source": "akshare",
            "report_period": PERIOD, "statement_type": "indicator",
            "announce_date": "2026-08-25", "net_profit": 50.0,
            "net_profit_basis": "attributable",
        }], "CN")
        stored = await coll.find_one({"symbol": symbol})
        assert stored["roe"] is None
        got = await repo.get_by_symbol(symbol, "CN", end_date=AS_OF)
        assert got[0]["net_profit"] == 50.0
        assert got[0]["roe"] is None
    finally:
        await coll.delete_many({"symbol": symbol})


@pytest.mark.requires_db
async def test_real_mongo_future_balance_cannot_hide_disclosed_indicator(real_mongo_db):
    symbol = "TESTFIN" + uuid4().hex
    coll = real_mongo_db[get_collection_name("financial_data", "CN")]
    repo = FinancialDataRepo()
    raw = normalize_period_rows(
        [_indicator()], [_balance(NOTICE_DATE="2026-10-28")], PERIOD,
    )
    doc = AKShareCNAdapter().adapt_financial_data(raw)[0].to_db_doc()
    doc["symbol"] = symbol
    try:
        await repo.upsert_many([doc], "CN")
        got = await repo.get_by_symbol(symbol, "CN", end_date=AS_OF)
        assert len(got) == 1
        assert got[0]["net_profit"] == 100.0 and got[0]["roe"] == 12.0
        assert got[0]["total_assets"] is None and got[0]["debt_ratio"] is None
        assert got[0]["announce_date"] == "2026-08-20"
    finally:
        await coll.delete_many({"symbol": symbol})


@pytest.mark.requires_db
async def test_real_mongo_unknown_and_future_newer_reports_do_not_consume_limit(real_mongo_db):
    symbol = "TESTFIN" + uuid4().hex
    coll = real_mongo_db[get_collection_name("financial_data", "CN")]
    repo = FinancialDataRepo()
    try:
        await repo.upsert_many([
            _record(symbol=symbol, period="2026-09-30", announced="2026-10-28"),
            _record(symbol=symbol, period="2026-06-30", announced=None),
            _record(symbol=symbol, period="2026-03-31", announced="2026-04-20", values={"net_profit": 75.0}),
        ], "CN")
        got = await repo.get_by_symbol(symbol, "CN", limit=1, end_date=AS_OF)
        assert len(got) == 1
        assert got[0]["report_period"] == "2026-03-31"
        assert got[0]["net_profit"] == 75.0
    finally:
        await coll.delete_many({"symbol": symbol})


@pytest.mark.requires_db
async def test_real_mongo_asof_read_does_not_rewrite_stored_future_evidence(real_mongo_db):
    symbol = "TESTFIN" + uuid4().hex
    coll = real_mongo_db[get_collection_name("financial_data", "CN")]
    repo = FinancialDataRepo()
    record = _record(symbol=symbol, values={"net_profit": 100.0, "debt_ratio": 40.0})
    record["announce_date"] = "2026-10-28"
    record["field_provenance"]["debt_ratio"] = _proof(
        "debt_ratio", announced="2026-10-28", channel="eastmoney_balance",
    )
    try:
        await repo.upsert_many([record], "CN")
        before = await coll.find_one({"symbol": symbol})
        got = await repo.get_by_symbol(symbol, "CN", end_date=AS_OF)
        assert got[0]["debt_ratio"] is None
        assert await coll.find_one({"symbol": symbol}) == before
    finally:
        await coll.delete_many({"symbol": symbol})
