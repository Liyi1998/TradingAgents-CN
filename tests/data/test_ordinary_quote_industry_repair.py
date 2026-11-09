"""Price/industry contracts and actual Mongo writes, without replaced providers."""

from uuid import uuid4

import pandas as pd
import pytest

from app.data.core.coverage import evaluate
from app.data.factors.engine import FactorScoreEngine
from app.data.factors.scoring import rank_factors_by_industry
from app.data.processor.industry_contract import INDUSTRY_CONTRACT, INDUSTRY_FIELDS, industry_label, industry_verified
from app.data.processor.quote_contract import select_quote_series
from app.data.query.technical_analysis import build_technical_snapshot, select_daily_bars, verified_price_group
from app.data.schema.domains.basic_info import StockBasicInfoSchema
from app.data.sources.cn.baostock.adapter import BaoStockCNAdapter
from app.data.sources.cn.baostock.api.daily_quotes import fetch_daily_quotes
from app.data.sources.cn.baostock.api.stock_basic import merge_industry_data
from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.repositories.basic_info_repo import BasicInfoRepo
from app.data.processor.validator import Validator


def verified_industry(**changes):
    row = dict(symbol="000001", market="CN", name="平安银行", industry="J66货币金融服务",
        industry_source="baostock:query_stock_industry", industry_classification="证监会行业分类",
        industry_as_of="2026-10-05", industry_contract=INDUSTRY_CONTRACT, data_source="baostock")
    row.update(changes)
    return row


@pytest.mark.parametrize("label", [None, "", "1", 1, "1.0", "-2", "nan", "unknown", "-", True, float("inf"), {}, []])
def test_security_types_and_invalid_values_are_not_industries(label):
    assert industry_label(label) is None
    assert not industry_verified(verified_industry(industry=label), "2026-10-08")


@pytest.mark.parametrize("changes", [
    {"industry_source": None}, {"industry_source": "1"}, {"industry_classification": None},
    {"industry_contract": None}, {"industry_as_of": "2026-10-09"},
    {"industry_as_of": "2026-99-01"}, {"industry_as_of": "20261005"},
])
def test_label_requires_effective_source_and_taxonomy(changes):
    assert not industry_verified(verified_industry(**changes), "2026-10-08")


def test_dated_industry_roundtrips_standard_schema_and_read_quality():
    row = verified_industry()
    assert StockBasicInfoSchema(**row).to_db_doc().items() >= row.items()
    data, quality = evaluate([row], "basic_info", None, "2026-10-08")
    assert quality["status"] == "success"
    assert data[0]["industry_status"] == "verified"
    future, quality = evaluate([row], "basic_info", None, "2026-09-30")
    assert future[0]["industry"] is None
    assert quality["status"] == "partial"
    assert row["industry"] == "J66货币金融服务"  # evaluation is nonmutating


def test_bao_basic_joins_actual_industry_not_security_type():
    basic = pd.DataFrame([dict(code="sz.000001", code_name="平安银行", type="1", status="1")])
    industries = pd.DataFrame([dict(code="sz.000001", updateDate="2026-10-05",
        industry="J66货币金融服务", industryClassification="证监会行业分类")])
    result = merge_industry_data(basic, industries, "2026-10-08")
    row = BaoStockCNAdapter().adapt_basic_info(result)[0].to_db_doc()
    assert row["symbol"] == "000001" and row["list_status"] == "L"
    assert industry_verified(row, "2026-10-08")
    before = merge_industry_data(basic, industries, "2026-09-30")
    assert pd.isna(before.iloc[0].industry)
    assert not industry_verified(BaoStockCNAdapter().adapt_basic_info(before)[0].to_db_doc())


@pytest.mark.parametrize("field,value", [("industry", "其他行业"), ("industryClassification", "其他分类")])
def test_same_date_industry_conflict_does_not_choose_arbitrary_label(field, value):
    basic = pd.DataFrame([dict(code="sz.000001", type="1")])
    original = dict(code="sz.000001", updateDate="2026-10-05",
        industry="J66货币金融服务", industryClassification="证监会行业分类")
    other = {**original, field: value}
    with pytest.raises(ValueError, match="conflicting"):
        merge_industry_data(basic, pd.DataFrame([original, other]), "2026-10-08")


@pytest.fixture
async def industry_stock(real_mongo_db):
    symbol = "TESTIND" + uuid4().hex[:12]
    coll = real_mongo_db[get_collection_name("basic_info", "CN")]
    try:
        yield coll, symbol
    finally:
        await coll.delete_many({"symbol": symbol})


@pytest.mark.parametrize("legacy", ["1", "1.0", 1, None, "nan"])
async def test_sparse_basic_write_retires_old_security_type(industry_stock, legacy):
    coll, symbol = industry_stock
    await coll.insert_one(dict(symbol=symbol, industry=legacy, industry_source="incorrect_old_source"))
    await BasicInfoRepo().upsert_many([dict(symbol=symbol, market="CN", name="$literal company", data_source="akshare")], "CN")
    stored = await coll.find_one({"symbol": symbol})
    assert stored["name"] == "$literal company"
    assert stored["industry"] is None
    assert all(stored[key] is None for key in INDUSTRY_FIELDS[1:])


async def test_sparse_basic_updates_keep_genuine_field_provenance(industry_stock):
    coll, symbol = industry_stock
    row = verified_industry(symbol=symbol)
    await BasicInfoRepo().upsert_many([row], "CN")
    await BasicInfoRepo().upsert_many([dict(symbol=symbol, market="CN", name="new name", data_source="tencent", industry="1")], "CN")
    stored = await coll.find_one({"symbol": symbol})
    assert stored["data_source"] == "tencent" and stored["name"] == "new name"
    assert all(stored[key] == row[key] for key in INDUSTRY_FIELDS)
    assert industry_verified(stored, "2026-10-08")


async def test_old_industry_response_cannot_downgrade_newer_taxonomy(industry_stock):
    coll, symbol = industry_stock
    repo = BasicInfoRepo()
    current = verified_industry(symbol=symbol)
    await repo.upsert_many([current], "CN")
    await repo.upsert_many([verified_industry(symbol=symbol, industry="旧行业", industry_as_of="2026-09-28")], "CN")
    stored = await coll.find_one({"symbol": symbol})
    assert all(stored[key] == current[key] for key in INDUSTRY_FIELDS)


async def test_invalid_future_industry_proof_does_not_block_real_repair(industry_stock):
    coll, symbol = industry_stock
    await coll.insert_one(verified_industry(symbol=symbol, industry="错误未来行业", industry_as_of="2099-01-01"))
    current = verified_industry(symbol=symbol)
    await BasicInfoRepo().upsert_many([current], "CN")
    stored = await coll.find_one({"symbol": symbol})
    assert stored["industry"] == current["industry"]
    assert stored["industry_as_of"] == "2026-10-05"


def test_industry_ranking_never_pools_different_taxonomies():
    df = pd.DataFrame([dict(industry="银行", _industry_classification=taxonomy, ret_5d=ret)
        for taxonomy, ret in [("证监会", 1), ("证监会", 2), ("东方财富", 3)]])
    got = rank_factors_by_industry(df, ["ret_5d"])
    assert got.ret_5d_rank.tolist() == [50, 100, 100]


async def test_non_numeric_but_unproven_labels_do_not_publish_style_scores(real_mongo_db, seed_factor_inputs):
    coll = real_mongo_db[get_collection_name("basic_info", "CN")]
    await coll.update_many({"symbol": {"$in": seed_factor_inputs["symbols"]}},
        {"$unset": {key: "" for key in INDUSTRY_FIELDS[1:]}})
    got = await FactorScoreEngine().compute_factors("CN")
    assert not got.empty
    assert got["_industry_status"].eq("unverified").all()
    assert got[["score_short_term", "score_balanced", "score_value"]].isna().all().all()


def quote(day, source="akshare", basis="raw", **changes):
    row = dict(symbol="000001", trade_date=day, data_source=source, period="daily", price_basis=basis,
        open=10, high=11, low=9, close=10, volume=100, amount=1000,
        volume_unit="share", amount_unit="CNY")
    row.update(changes)
    return row


def test_known_raw_quotes_cannot_be_shadowed_by_unknown_cache_priority():
    rows = [quote("2026-10-08"), quote("2026-10-09", "duckdb", "unknown")]
    got = select_daily_bars(rows, "2026-10-09", ["duckdb", "akshare"])
    assert got.data_source.eq("akshare").all()
    assert verified_price_group(got, "2026-10-09")


def test_batch_selection_chooses_one_series_per_stock_and_never_double_counts_dates():
    rows = [quote("2026-10-05"), quote("2026-10-06"), quote("2026-10-06"),
        quote("2026-10-06", "duckdb", "unknown"),
        quote("2026-10-07", symbol="600000"), quote("2026-10-08", "duckdb", "unknown", symbol="600000")]
    got = select_quote_series(pd.DataFrame(rows), "2026-10-08", ["duckdb", "akshare"])
    assert got.groupby("symbol").size().to_dict() == {"000001": 2, "600000": 1}
    assert got.data_source.eq("akshare").all()
    assert not got.duplicated(["symbol", "trade_date"]).any()


async def test_actual_factor_engine_rejects_unknown_parallel_quote_series(real_mongo_db, seed_factor_inputs):
    engine = FactorScoreEngine()
    baseline = await engine.compute_factors("CN")
    coll = real_mongo_db[get_collection_name("daily_quotes", "CN")]
    rows = await coll.find({"symbol": {"$in": seed_factor_inputs["symbols"]}}, {"_id": 0}).to_list(length=None)
    for row in rows:
        row.update(data_source="duckdb", price_basis="unknown", close=999)
        row.pop("adjustment_contract", None)
        row.pop("adjustment_as_of", None)
    from pymongo.errors import BulkWriteError
    with pytest.raises(BulkWriteError):
        await coll.insert_many(rows)
    got = await engine.compute_factors("CN")
    pd.testing.assert_frame_equal(got.sort_values("symbol").reset_index(drop=True),
        baseline.sort_values("symbol").reset_index(drop=True))


def test_same_source_raw_and_qfq_history_are_not_spliced():
    rows = [quote("2026-10-05"), quote("2026-10-06"), quote("2026-10-07", basis="qfq", close=5)]
    got = select_daily_bars(rows, "2026-10-08", ["akshare"])
    assert got.price_basis.eq("raw").all() and len(got) == 2


def test_adjusted_snapshots_with_distinct_horizons_are_not_spliced():
    rows = [quote("2026-10-05", basis="qfq", adjustment_contract="cn_adjusted_quotes_v1", adjustment_as_of="2026-10-06"),
            quote("2026-10-07", basis="qfq", adjustment_contract="cn_adjusted_quotes_v1", adjustment_as_of="2026-10-08")]
    got = select_daily_bars(rows, "2026-10-08", ["akshare"])
    assert len(got) == 1 and got.iloc[0].adjustment_as_of == "2026-10-08"


def test_technical_snapshot_declares_raw_and_does_not_claim_qfq():
    rows = [quote("2026-10-08")]
    result = build_technical_snapshot(rows, [dict(cal_date="2026-10-08", is_open=True)], "2026-10-08", ["akshare"], trajectory_days=0)
    assert result["price_basis"] == "raw"
    assert any("不复权" in text for text in result["warnings"])


def actual_raw_ex_event():
    # Actual 301608 source row exposed by this bounded refresh acceptance.
    return dict(symbol="301608", trade_date="2026-05-20", period="daily", data_source="akshare",
        open=66.91, high=67.50, low=66.09, close=66.65, change=-34.10, pct_chg=-33.85,
        volume=1873000.0, amount=124802016.0, price_basis="raw", volume_unit="share", amount_unit="CNY",
        unit_conversion="akshare:stock_zh_a_hist/eastmoney;volume:lot100->share*100;amount:CNY->CNY")


def test_raw_basis_jump_does_not_delete_a_real_daily_bar():
    original = actual_raw_ex_event()
    valid, errors = Validator().validate([original], "daily_quotes", "CN")
    assert not errors and len(valid) == 1
    assert all(valid[0][key] == original[key] for key in ("open", "high", "low", "close", "volume", "amount"))
    assert valid[0]["pct_chg"] is None and valid[0]["change"] is None
    assert valid[0]["reported_pct_chg"] == -33.85
    assert valid[0]["price_change_status"] == "raw_jump_reference_unverified"
    assert original["pct_chg"] == -33.85  # validator never mutates caller evidence


@pytest.mark.parametrize("changes", [dict(price_basis="qfq"), dict(price_basis="unknown"),
    dict(volume_unit=None), dict(unit_conversion=None), dict(close=99), dict(amount=1), dict(volume=-1)])
def test_invalid_or_unproven_jump_is_not_granted_a_raw_price_exception(changes):
    row = {**actual_raw_ex_event(), **changes}
    valid, errors = Validator().validate([row], "daily_quotes", "CN")
    assert not valid and errors


@pytest.mark.parametrize("adjust", [False, None, [], "invalid"])
async def test_bao_rejects_unknown_adjustment_before_network(adjust):
    with pytest.raises(ValueError, match="unsupported"):
        await fetch_daily_quotes("000001", "2026-10-01", "2026-10-08", adjust=adjust)
