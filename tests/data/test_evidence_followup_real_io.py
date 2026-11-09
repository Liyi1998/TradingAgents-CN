"""Actual exchange/news → adapter → real Mongo → facade/analyst acceptance."""
import asyncio
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import time

import pandas as pd
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("EVIDENCE_FOLLOWUP_REAL_IO") != "1",
    reason="Explicit isolated real-storage/public-provider acceptance only",
)


@pytest.fixture
async def real_analysis_facade(real_mongo_db):
    import app.core.database as database
    from app.core.async_utils import set_main_loop
    from app.core.config import settings
    from app.data.core.interface import DataInterface
    from app.data.storage.redis.client import reset_client
    from isolated_database import require_isolated_database
    require_isolated_database()
    await database.db_manager.init_redis()
    database.redis_client = database.db_manager.redis_client
    database.redis_pool = database.db_manager.redis_pool
    reset_client()
    assert await database.redis_client.ping()
    set_main_loop(asyncio.get_running_loop())
    DataInterface.reset_instance()
    try:
        yield DataInterface.get_instance()
    finally:
        set_main_loop(None)
        DataInterface.reset_instance()
        database.redis_client = database.redis_pool = None
        reset_client()


@pytest.mark.asyncio
async def test_real_calendar_supplies_seven_sessions_without_crossing_a_hole(real_analysis_facade):
    from app.data.core.analysis_context import verified_future_sessions
    from app.engine.orchestrator.agents import _resolve_inject_args
    from app.engine.tools.datasources.registry import get_spec_by_id
    context = await real_analysis_facade.analysis_context("CN", "2026-10-10")
    assert context["calendar_status"] == context["future_calendar_status"] == "success"
    assert context["last_completed_trade_date"] == "2026-10-09"
    assert context["next_trade_dates"] == [
        "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20",
    ]
    injected = _resolve_inject_args(get_spec_by_id("dragon_tiger"), {
        "ticker": "600977", "trade_date": "2026-10-10", **context,
    })
    assert injected["trade_date"] == "20261009"  # Not the Saturday analysis date.
    rows = (await real_analysis_facade.read("CN", "trade_calendar", start_date="2026-10-10", end_date="2026-11-09"))["data"]
    complete = deepcopy(rows)
    anchor = date.fromisoformat("2026-10-10")
    assert verified_future_sessions(complete, anchor) == context["next_trade_dates"]
    hole = [row for row in complete if row["cal_date"] != "2026-10-15"]
    assert verified_future_sessions(hole, anchor) == context["next_trade_dates"][:3]
    original = next(row for row in complete if row["cal_date"] == "2026-10-15")
    conflicting = complete + [{**original, "is_open": not bool(original["is_open"])}]
    assert verified_future_sessions(conflicting, anchor) == context["next_trade_dates"][:3]
    invalid = complete + [{**original, "is_open": "unknown"}]
    assert verified_future_sessions(invalid, anchor) == context["next_trade_dates"][:3]
    print("ACTUAL_SEVEN_SESSION_CALENDAR", json.dumps(context["next_trade_dates"]))


@pytest.mark.asyncio
async def test_worker_context_uses_real_application_loop_and_completed_session(real_analysis_facade):
    context = await asyncio.to_thread(lambda: asyncio.run(real_analysis_facade.analysis_context("CN", "2026-10-10")))
    assert context["last_completed_trade_date"] == "2026-10-09"
    assert len(context["next_trade_dates"]) == 7 and context["calendar_status"] == "success"


@pytest.fixture(scope="module")
def actual_exchange_exports():
    from app.data.sources.cn.shared.margin_series import _exchange_day
    return {"SSE": _exchange_day("SSE", "2026-10-09"), "SZSE": _exchange_day("SZSE", "2026-10-08")}


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,exchange,day", [
    ("600977", "SSE", "2026-10-09"), ("600997", "SSE", "2026-10-09"), ("300124", "SZSE", "2026-10-08"),
])
async def test_actual_margin_unit_proof_survives_storage_and_consumer(
    real_analysis_facade, real_mongo_db, actual_exchange_exports, symbol, exchange, day,
):
    from app.data.core.requirements import margin_units_verified, verify_rows
    from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
    from app.data.sources.cn.akshare.api.margin_trading import _filter_symbol
    from app.data.storage.mongo.collections import get_collection_name
    from app.data.storage.mongo.repositories.margin_trading_repo import MarginTradingRepo
    frame = _filter_symbol(actual_exchange_exports[exchange], symbol)
    assert len(frame) == 1
    schema = AKShareCNAdapter().adapt_margin_trading(frame)[0]
    row = schema.to_db_doc()
    assert margin_units_verified(row) and schema.margin_balance == float(frame.iloc[0]["融资余额"])
    coll = real_mongo_db[get_collection_name("margin_trading", "CN")]
    others_before = await coll.count_documents({"symbol": {"$ne": symbol}})
    await MarginTradingRepo().upsert_many([row], "CN")
    filters = {"contract": "verified_exchange_margin", "expected_dates": [day],
               "required_fields": ["margin_balance", "margin_buy_amount"], "calendar_verified": True}
    result = await real_analysis_facade.read("CN", "margin_trading", symbol=symbol,
                                            start_date=day, end_date=day, filters=filters)
    consumed = next(item for item in result["data"] if item["data_source"] == "akshare")
    assert margin_units_verified(consumed) and consumed["margin_balance"] == row["margin_balance"]
    assert consumed["unit_evidence"] == row["unit_evidence"]
    assert await coll.count_documents({"symbol": {"$ne": symbol}}) == others_before
    for field in ("unit_evidence", "source_response_date", "date_evidence_status", "amount_unit", "monetary_unit"):
        broken = deepcopy(row)
        broken.pop(field)
        checked, issues = verify_rows([broken], "margin_trading", filters)
        assert checked[0]["margin_balance"] is None and checked[0]["short_balance"] is None
        assert checked[0]["amount_unit"] is None and issues
        outside_required_window = {**filters, "expected_dates": ["2018-08-02"]}
        checked, issues = verify_rows([deepcopy(broken)], "margin_trading", outside_required_window)
        assert checked[0]["margin_amount_evidence_status"] == "unverified" and issues
    nonfinite = {**deepcopy(row), "total_balance": float("nan")}
    checked, issues = verify_rows([nonfinite], "margin_trading", filters)
    assert checked[0]["total_balance"] is None and issues
    if exchange == "SZSE":
        assert consumed.get("margin_repay_amount") is None  # No invented official repayment.
    legacy = {key: value for key, value in row.items() if key not in {
        "unit_evidence", "date_evidence_status", "source_response_date", "amount_unit", "unit_conversion", "monetary_unit",
    }}
    await MarginTradingRepo().upsert_many([legacy], "CN")
    stored = await coll.find_one({"symbol": symbol, "trade_date": day, "data_source": "akshare"}, {"_id": 0})
    assert stored["unit_evidence"] is None and stored["amount_unit"] is None
    assert not margin_units_verified(stored)
    legacy_view = await real_analysis_facade.read("CN", "margin_trading", symbol=symbol,
                                                start_date=day, end_date=day, filters=filters)
    assert all(item.get("margin_balance") is None for item in legacy_view["data"] if item["data_source"] == "akshare")
    await MarginTradingRepo().upsert_many([row], "CN")  # Restore only this isolated test observation.
    print("ACTUAL_MARGIN_PROOF", json.dumps({key: consumed.get(key) for key in
        ("symbol", "trade_date", "exchange", "amount_unit", "source_response_date", "unit_evidence", "margin_repay_amount")}, ensure_ascii=False))


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,name", [("600977", "中国电影"), ("600997", "开滦股份"), ("300124", "汇川技术")])
async def test_actual_stock_news_body_reaches_the_analyst_without_false_full_coverage(real_analysis_facade, symbol, name):
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
    from app.data.sources.cn.shared.news_channels import fetch_sina_news
    from app.data.storage.mongo.repositories.basic_info_repo import BasicInfoRepo
    from app.data.storage.mongo.repositories.news_repo import NewsRepo
    from app.engine.tools.datasources.news import _fetch_news_snapshot, _format_news_list
    from app.engine.tools.datasources.news_classification import has_body_text, sentiment_coverage
    path = os.environ.get("REPORT_PUBLIC_BASIC_SNAPSHOT")
    assert path, "Requires exact read-only production public-profile observations"
    basic = json.loads(Path(path).read_text())
    rows = [row for row in basic if row["symbol"] == symbol]
    assert rows and rows[0]["name"] == name
    await BasicInfoRepo().upsert_many(rows, "CN")
    began = time.monotonic()
    news = await fetch_sina_news(name, symbol, 10, end_date="2026-10-10")
    assert time.monotonic() - began < 12
    assert news and max(row["publish_time"][:10] for row in news) >= "2026-10-09"
    assert all(row["publish_time"][:10] <= "2026-10-10" for row in news)
    assert any(has_body_text(row) for row in news), "A real substantive excerpt, not a copied title, is required"
    if symbol == "600977":
        assert not any("博物馆" in row["title"] or "金鸡奖" in row["title"] or "老扎" in row["title"] for row in news)
    adapted = AKShareCNAdapter().adapt_news(news)
    await NewsRepo().upsert_many([row.to_db_doc() for row in adapted], "CN")
    context = await real_analysis_facade.analysis_context("CN", "2026-10-10")
    token = ACTIVE_ANALYSIS_CONTEXT.set(context)
    try:
        snapshot = await asyncio.to_thread(_fetch_news_snapshot, symbol, 15, "2026-10-10")
        assert snapshot["latest_publication_date"] >= "2026-10-09"
        assert snapshot["current_day_news_coverage"] == "unverified"
        assert snapshot["completeness"] == "observed_sample_not_all_news"
        assert any(has_body_text(row) for row in snapshot["items"])
        output = _format_news_list(snapshot["items"])
        assert "html_excerpt" in output and "非公司事实核验" in output
        assert "not_verified" in output or "not_fact_verified" in output
        title_only = [{**row, "content": row["title"], "content_status": "title_only"} for row in news]
        assert sentiment_coverage(title_only)["body_coverage"] == 0
        assert all(row["discovery_metadata"]["subject_binding"].endswith("not_fact_verified") for row in news)
        print("ACTUAL_TARGETED_NEWS", json.dumps({"symbol": symbol, "latest": snapshot["latest_publication_date"],
              "coverage": snapshot["classification_coverage"], "count": len(snapshot["items"])}, ensure_ascii=False))
    finally:
        ACTIVE_ANALYSIS_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_actual_code_search_preserves_snippet_origin_not_a_full_body():
    from app.data.sources.cn.shared.news_channels import fetch_em_search
    rows = await fetch_em_search("中国电影", "600977", 10, end_date="2026-10-10")
    assert rows
    assert all(row["discovery_metadata"]["query_symbol"] == "600977" for row in rows)
    assert all(row["discovery_metadata"]["body_completeness"] == "search_excerpt_not_full_article" for row in rows)
    assert all(row["url"] for row in rows) and all(row["publish_time"][:10] <= "2026-10-10" for row in rows)


@pytest.mark.asyncio
async def test_registered_free_news_refresh_reaches_the_actual_analyst_tool(real_analysis_facade):
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.engine.tools.datasources.news import _fetch_news_snapshot
    began = time.monotonic()
    refreshed = await real_analysis_facade.refresh("CN", "600977", domains=["news"], force=True,
        timeout=15, start_date="2026-09-10", end_date="2026-10-10", coverage_filters={"include_filings": False})
    assert time.monotonic() - began < 22
    detail = refreshed.domains["news"]
    assert detail.status == "refreshed" and refreshed.status == "refreshed"
    assert detail.record_count > 0 and detail.source == "akshare" and detail.error is None
    context = await real_analysis_facade.analysis_context("CN", "2026-10-10")
    token = ACTIVE_ANALYSIS_CONTEXT.set(context)
    try:
        snapshot = await asyncio.to_thread(_fetch_news_snapshot, "600977", 15, "2026-10-10")
        assert snapshot["latest_publication_date"] >= "2026-10-09"
        assert any(item.get("content_status") == "html_excerpt" for item in snapshot["items"])
        assert snapshot["current_day_news_coverage"] == "unverified"
    finally:
        ACTIVE_ANALYSIS_CONTEXT.reset(token)


def test_documented_tushare_adapter_keeps_native_units_without_a_paid_call():
    # The exact first row in the official margin_detail documentation, not a
    # fabricated provider response or a network-client replacement.
    from app.data.sources.cn.tushare.adapter import TushareCNAdapter
    from app.data.core.requirements import margin_units_verified
    raw = [{"ts_code": "000001.SZ", "trade_date": "20180802", "rzye": 4430811000.0,
            "rzmre": 78800436.0, "rzche": 147005397.0, "rqye": 8238210.0, "rzrqye": 4439049000.0}]
    row = TushareCNAdapter().adapt_margin_trading(pd.DataFrame(raw))[0].to_db_doc()
    assert margin_units_verified(row) and row["margin_buy_amount"] == 78800436.0
    assert row["amount_unit"] == "CNY" and row["source_response_date"] == "2018-08-02"
