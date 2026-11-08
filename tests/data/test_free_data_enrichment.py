"""Real MongoDB replay and optional live free-provider checks; no mocked I/O."""

import asyncio
import copy
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from app.data.query.technical_analysis import build_technical_snapshot, select_daily_bars
from app.data.sources.cn.akshare.api.financial import _build_financial_row, _fetch_sina_table, _safe_float
from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
from app.data.sources.cn.shared.official_notices import extract_official_pdf, fetch_official_notices
from app.engine.orchestrator.agents import _resolve_inject_args
from app.engine.tools.datasources.registry import get_spec_by_id
from app.engine.tools.common.tool_result import format_tool_result, success_result, no_data_result
from app.llm.tools.result_status import classify_tool_result


@pytest.fixture(scope="module")
def actual_history():
    if os.environ.get("RUN_PRODUCTION_HISTORY_REPLAY") != "1":
        pytest.skip("Production history replay requires explicit opt-in")
    script = """import json
from app.core.database import get_mongo_db_sync
db = get_mongo_db_sync()
print(json.dumps({'daily': list(db.stock_daily_quotes.find({'symbol':'603232'}, {'_id':0})),
                  'calendar': list(db.trade_calendar.find({'exchange':'SSE', 'cal_date':{'$gte':'2024-10-01','$lte':'2026-10-01'}}, {'_id':0}))}, default=str))
"""
    result = subprocess.run(["docker", "exec", "tradingagents-backend", "python", "-c", script], capture_output=True, text=True, timeout=20)
    if result.returncode:
        pytest.skip("Requires running local production MongoDB via backend container")
    history = json.loads(result.stdout)
    assert len(history["daily"]) >= 260
    return history


def snapshot(history, end="2026-10-01"):
    return build_technical_snapshot(history["daily"], history["calendar"], end, ["duckdb", "akshare", "baostock"])


def test_real_holiday_is_not_a_missing_session(actual_history):
    result = snapshot(actual_history)
    assert result["data_cutoff"] == "2026-09-30"
    assert result["calendar_complete"]
    assert "2026-09-25" in result["closed_dates"]
    assert "2026-09-25" not in result["missing_open_dates"]
    assert result["latest_expected_trade_date"] == "2026-09-30"
    assert not result["missing_open_dates"]


def test_real_long_history_indicators_and_aggregation(actual_history):
    result = snapshot(actual_history)
    bars = select_daily_bars(actual_history["daily"], "2026-10-01", ["duckdb"])
    for period in (5, 10, 20, 60, 120, 250):
        assert result["indicators"][f"ma{period}"] == pytest.approx(bars.close.tail(period).mean(), abs=1e-6)
    assert 0 <= result["indicators"]["rsi14"] <= 100
    assert result["indicators"]["macd_hist"] == pytest.approx(2 * (result["indicators"]["macd_dif"] - result["indicators"]["macd_dea"]), abs=2e-6)
    latest_month = bars[bars.trade_date.dt.strftime("%Y-%m") == "2026-09"]
    assert result["monthly"][-1]["volume"] == latest_month.volume.sum()
    assert len(result["weekly"]) == 26
    assert len(json.dumps(result, allow_nan=False)) < 25000


def test_removed_real_open_bar_blocks_cross_gap_indicators(actual_history):
    history = copy.deepcopy(actual_history)
    history["daily"] = [row for row in history["daily"] if row["trade_date"] != "2026-09-24"]
    result = snapshot(history)
    assert result["missing_open_dates"] == ["2026-09-24"]
    assert result["continuous_tail_bars"] == 3
    assert result["indicators"]["ma5"] is None
    assert result["indicators"]["macd_dif"] is None


def test_insufficient_real_history_does_not_fabricate_ma250(actual_history):
    history = copy.deepcopy(actual_history)
    frame = select_daily_bars(history["daily"], "2026-10-01", ["duckdb"]).tail(20)
    history["daily"] = frame.to_dict("records")
    result = snapshot(history)
    assert result["indicators"]["ma20"] is not None
    assert result["indicators"]["ma60"] is None
    assert result["indicators"]["ma250"] is None


def test_as_of_date_never_uses_future_prices(actual_history):
    result = snapshot(actual_history, "2026-09-24")
    assert result["data_cutoff"] == "2026-09-24"
    assert all(row["trade_date"] <= "2026-09-24" for row in result["recent_daily"])


def test_provider_duplicates_never_double_count_volume(actual_history):
    history = copy.deepcopy(actual_history)
    history["daily"] += copy.deepcopy(history["daily"])
    assert snapshot(history)["monthly"] == snapshot(actual_history)["monthly"]


def test_date_arguments_anchor_to_analysis_day():
    spec = get_spec_by_id("daily_quotes")
    args = _resolve_inject_args(spec, {"ticker": "603232", "trade_date": "2026-09-24"})
    assert args["end_date"] == "2026-09-24"
    assert args["start_date"] == "2026-08-10"


@pytest.mark.parametrize("value,expected", [(0, 0), (1234.5, 1234.5), ("12.5%", 12.5), ("3.5亿", 350000000), ("--", None)])
def test_numeric_financial_units_preserve_zero(value, expected):
    assert _safe_float(value) == expected


@pytest.mark.parametrize("url", ["http://static.cninfo.com.cn/test.pdf", "https://example.com/test.pdf", "https://static.cninfo.com.cn/a.html", "https://static.cninfo.com.cn:8443/a.pdf"])
def test_pdf_reader_rejects_non_official_urls_without_network(url):
    assert extract_official_pdf(url) == ("", "invalid_official_url")


@pytest.fixture(scope="module")
def live_tables():
    if os.environ.get("RUN_FREE_DATA_LIVE") != "1":
        pytest.skip("Set RUN_FREE_DATA_LIVE=1 for real public-provider checks")
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {name: executor.submit(_fetch_sina_table, "sh603232", name) for name in ("balance", "income", "cashflow")}
        return {name: future.result(timeout=15) for name, future in futures.items()}


def test_live_sina_three_statements_map_to_standard_fields(live_tables):
    row = _build_financial_row("603232", live_tables, "2026-06-30")
    assert row["announce_date"] == "2026-08-29"
    assert row["total_assets"] > 1000000000
    assert row["cash_and_equivalents"] > 0
    assert row["operating_cashflow"] < 0
    schema = AKShareCNAdapter().adapt_financial_data(pd.DataFrame([row]))[0]
    assert schema.monetary_unit == "元"
    assert schema.total_liabilities == row["total_liab"]
    assert schema.available_statements == "balance,income,cashflow"
    assert "sina_finance" in schema.statement_sources


def test_live_cross_period_statements_are_not_mixed(live_tables):
    tables = dict(live_tables)
    tables["balance"] = tables["balance"][tables["balance"]["报告日"] != "20260630"]
    row = _build_financial_row("603232", tables, "2026-06-30")
    assert row["total_assets"] is None
    assert "balance" not in row["available_statements"]
    assert row["net_profit"] is not None


def test_live_official_notice_text_and_provenance():
    if os.environ.get("RUN_FREE_DATA_LIVE") != "1":
        pytest.skip("Set RUN_FREE_DATA_LIVE=1 for real public-provider checks")
    records = asyncio.run(fetch_official_notices("603232", 3))
    assert records
    assert all(row["symbol"] == "603232" and row["source_kind"] == "official" for row in records)
    assert any(len(row["content"]) > 500 for row in records)
    for schema in AKShareCNAdapter().adapt_news(records):
        assert schema.category == "official_announcement"
        assert schema.content_status.startswith(("pdf_targeted_notes_excerpt", "pdf_fetch_", "pdf_text_unavailable"))
        assert schema.url.startswith("https://static.cninfo.com.cn/")
    # A limitations disclaimer must not cause all real official documents to disappear.
    payload = {"records": records, "limitations": "未取得记录不等于没有公告或重大事项。"}
    assert classify_tool_result(format_tool_result(success_result(json.dumps(payload, ensure_ascii=False)))) == "success"


def test_explicit_empty_and_business_error_envelopes_still_work():
    assert classify_tool_result(format_tool_result(no_data_result())) == "empty"
    assert classify_tool_result(format_tool_result(success_result("积分已用完，请购买套餐包"))) == "error"
    assert classify_tool_result({"status": "success", "records": []}) == "empty"
