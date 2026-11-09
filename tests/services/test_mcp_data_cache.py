import asyncio
import threading

import pytest

from app.data.sync import mcp_ingestion as mcp_data_cache
from app.data.sync.mcp_ingestion import (
    _canonical_mx_tool_name,
    _resolve_symbol,
    extract_sheets,
    is_news_fresh,
    normalize_mx_financial,
    normalize_mx_money_flow,
    normalize_mx_news,
)


def _content(payload):
    import json
    return [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]


def test_wrapped_mcp_tool_name_and_query_symbol_are_normalized():
    assert (
        _canonical_mx_tool_name("mcp__mx-ds-mcp__mx_finance_search_news")
        == "mx_finance_search_news"
    )
    assert _resolve_symbol(
        {"query": "查询国新健康（000503）最近7天新闻"}
    ) == "000503"


def test_extract_and_normalize_mx_news_rows():
    raw = _content({"data": [{
        "columns": ["标题", "摘要", "发布时间", "来源", "跳转链接"],
        "items": [["测试新闻", "摘要", "2026-08-02 10:00:00", "测试源", "https://example.com/1"]],
    }]})
    assert len(extract_sheets(raw)) == 1
    records = normalize_mx_news(raw, "600875")
    assert records[0]["symbol"] == "600875"
    assert records[0]["data_source"] == "mx_mcp"
    assert records[0]["title"] == "测试新闻"


def test_normalize_mx_financial_transposed_table():
    raw = _content({"data": [{
        "columns": ["东方电气(600875.SH)", "2026一季报", "2025年报"],
        "items": [
            ["营业总收入", "174.7亿元", "786.2亿元"],
            ["归属于母公司股东的净利润", "15.85亿元", "38.31亿元"],
            ["净资产收益率ROE(加权)", "3.44%", "9%"],
            ["资产负债率", "69.47%", "70.39%"],
            ["每股收益EPS(基本)", "0.46元", "1.15元"],
        ],
    }]})
    records = {r["report_period"]: r for r in normalize_mx_financial(raw, "600875")}
    assert records["2026-03-31"]["revenue"] == 17_470_000_000
    # FinancialDataSchema 与现有 CN 数据源统一按百分数点存储比率。
    assert records["2026-03-31"]["roe"] == 3.44
    assert records["2026-03-31"]["debt_ratio"] == 69.47
    assert records["2025-12-31"]["eps"] == 1.15


def test_normalize_mx_money_flow_detailed_table():
    raw = _content({"data": [{
        "columns": [
            "时间", "主力净流入金额(万元)", "主力资金净流入比率(%)",
            "超大单净买入金额(万元)", "大单净买入金额(万元)",
            "中单净买入金额(万元)", "小单净买入金额(万元)",
        ],
        "items": [["2026-08-07", "-186.03", "-1.06", "-135.04", "-51.00", "-10.86", "196.89"]],
    }]})

    records = normalize_mx_money_flow(raw, "600332.SH")

    assert len(records) == 1
    assert records[0]["symbol"] == "600332"
    assert records[0]["data_source"] == "mx_mcp"
    assert records[0]["trade_date"] == "2026-08-07"
    assert records[0]["main_net_inflow"] == pytest.approx(-1_860_300)
    assert records[0]["huge_net_inflow"] == pytest.approx(-1_350_400)
    assert records[0]["small_net_inflow"] == pytest.approx(1_968_900)


@pytest.mark.asyncio
async def test_news_freshness_mongo_read_runs_on_registered_main_loop(monkeypatch):
    from app.core.async_utils import set_main_loop

    main_loop = asyncio.get_running_loop()
    main_thread_id = threading.get_ident()

    async def fake_impl(symbol, max_age_hours=24):
        assert asyncio.get_running_loop() is main_loop
        assert threading.get_ident() == main_thread_id
        return symbol == "600875" and max_age_hours == 12

    monkeypatch.setattr(mcp_data_cache, "_is_news_fresh_impl", fake_impl)
    set_main_loop(main_loop)
    try:
        result = await asyncio.to_thread(
            lambda: asyncio.run(is_news_fresh("600875", max_age_hours=12))
        )
    finally:
        set_main_loop(None)

    assert result is True
