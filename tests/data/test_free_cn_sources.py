"""DuckDB HTTP 与腾讯财经免费 A 股数据源测试。"""

import os
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.data.sources.cn import get_cn_adapter, get_cn_provider
from app.data.sources.cn.duckdb.adapter import JinceDuckDBCNAdapter
from app.data.sources.cn.duckdb.provider import JinceDuckDBCNProvider
from app.data.sources.cn.akshare.api.financial import _filter_df_by_report_period
from app.data.sources.cn.akshare.provider import _filter_by_date
from app.data.sources.cn.tencent.adapter import TencentCNAdapter
from app.data.sources.cn.tencent.api.market_quotes import fetch_market_quotes
from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
from app.data.sources.cn.akshare.api.margin_trading import _candidate_trade_dates
from app.data.sources.cn.sina.adapter import SinaCNAdapter
from app.data.sources.cn.sina.provider import SinaCNProvider
from app.llm.mcp.management.config_store import _expand_env_value, load_mcp_config
from app.data.core.reader import Reader


def test_cn_factory_registers_free_sources():
    assert isinstance(get_cn_provider("duckdb"), JinceDuckDBCNProvider)
    assert isinstance(get_cn_adapter("duckdb"), JinceDuckDBCNAdapter)
    assert isinstance(get_cn_adapter("tencent"), TencentCNAdapter)
    assert isinstance(get_cn_provider("sina"), SinaCNProvider)
    assert isinstance(get_cn_adapter("sina"), SinaCNAdapter)


def test_duckdb_adapter_maps_standard_daily_quote():
    adapter = JinceDuckDBCNAdapter()
    records = adapter.adapt_daily_quotes(
        pd.DataFrame(
            [
                {
                    "symbol": "000001",
                    "trade_date": "2026-07-31",
                    "open": 11.5,
                    "high": 11.63,
                    "low": 11.28,
                    "close": 11.63,
                    "pre_close": 11.61,
                    "volume": 201_359_500,
                }
            ]
        )
    )
    assert len(records) == 1
    assert records[0].data_source == "duckdb"
    assert records[0].volume == 201_359_500


def test_tencent_adapter_converts_quote_fields():
    adapter = TencentCNAdapter()
    records = adapter.adapt_market_quotes(
        pd.DataFrame(
            [
                {
                    "symbol": "000001",
                    "price": 11.63,
                    "pre_close": 11.61,
                    "open": 11.5,
                    "high": 11.63,
                    "low": 11.28,
                    "volume": 201_359_500,
                    "amount": 2_318_839_881,
                    "pct_chg": 0.17,
                    "update_time": "20260731161436",
                }
            ]
        )
    )
    assert len(records) == 1
    assert records[0].data_source == "tencent"
    assert records[0].trade_date == "2026-07-31"
    assert records[0].close == 11.63


def test_tencent_adapter_converts_daily_indicators():
    adapter = TencentCNAdapter()
    records = adapter.adapt_daily_indicators(
        pd.DataFrame(
            [{
                "symbol": "000001",
                "update_time": "20260731161436",
                "pe_ttm": 5.24,
                "pb": 0.49,
                "turnover_rate": 1.04,
                "total_mv": 225_687_000_000,
                "circ_mv": 225_691_000_000,
            }]
        )
    )
    assert records[0].trade_date == "2026-07-31"
    assert records[0].pe_ttm is None  # Vendor PE has no verified TTM basis
    assert records[0].pb == 0.49


def test_tencent_adapter_converts_intraday_quotes():
    records = TencentCNAdapter().adapt_intraday_quotes(pd.DataFrame([{
        "symbol": "600875", "datetime": "2026-07-31 09:31:00", "freq": "1min",
        "open": 25.5, "high": 25.52, "low": 25.5, "close": 25.52,
        "volume": 1_181_100, "amount": 30_185_954.31,
    }]))
    assert records[0].data_source == "tencent"
    assert records[0].freq == "1min"
    assert records[0].volume == 1_181_100


def test_sina_adapter_converts_money_flow_without_inventing_splits():
    records = SinaCNAdapter().adapt_money_flow(pd.DataFrame([{
        "symbol": "600875",
        "opendate": "2026-08-07",
        "netamount": "41533889.67",
        "ratioamount": "0.0631",
        "r0_net": "9823312.10",
    }]))
    assert len(records) == 1
    assert records[0].data_source == "sina"
    assert records[0].main_net_inflow == pytest.approx(41_533_889.67)
    assert records[0].huge_net_inflow == pytest.approx(9_823_312.10)
    assert records[0].medium_net_inflow is None
    assert records[0].small_net_inflow is None


def test_akshare_adapter_converts_cn_corporate_actions():
    records = AKShareCNAdapter().adapt_corporate_actions(pd.DataFrame([{
        "symbol": "600875", "实施方案公告日期": "2026-07-17",
        "股权登记日": "2026-07-23", "除权日": "2026-07-24",
        "派息日": "2026-07-24", "派息比例": 5.3,
        "送股比例": 0, "转增比例": 0,
    }]))
    assert len(records) == 1
    assert records[0].action_type == "cash_dividend"
    assert records[0].amount == pytest.approx(0.53)


def test_akshare_adapter_converts_current_sse_margin_columns():
    records = AKShareCNAdapter().adapt_margin_trading(pd.DataFrame([{
        "信用交易日期": "20260731", "标的证券代码": "600875",
        "融资余额": 1_821_736_451, "融资买入额": 115_580_292,
        "融资偿还额": 117_771_294, "融券余量": 678_200,
        "融券卖出量": 24_900,
    }]))
    assert len(records) == 1
    assert records[0].symbol == "600875"
    assert records[0].trade_date == "2026-07-31"
    assert records[0].margin_balance == 1_821_736_451


def test_margin_date_candidates_fall_back_across_weekend():
    candidates = _candidate_trade_dates("2026-08-03")
    assert candidates[:2] == ["20260803", "20260731"]
    assert len(candidates) == 10


@pytest.mark.asyncio
async def test_intraday_reader_expands_date_only_end_boundary(monkeypatch):
    reader = Reader()
    repo = AsyncMock()
    repo.get_by_symbol_and_range.return_value = [
        {"symbol": "600875", "datetime": "2026-08-03 09:30:00"}
    ]
    monkeypatch.setattr(reader, "_get_repo", lambda domain: repo)
    monkeypatch.setattr(reader, "check_freshness", AsyncMock(return_value="fresh"))

    data, freshness = await reader.get_data(
        "CN", "intraday_quotes", "600875",
        start_date="2026-08-03", end_date="2026-08-03",
    )

    repo.get_by_symbol_and_range.assert_awaited_once_with(
        "600875", "CN",
        "2026-08-03 00:00:00", "2026-08-03 23:59:59",
        freq=None,
    )
    assert len(data) == 1
    assert freshness == "fresh"


def test_akshare_financial_period_filter_uses_boolean_mask():
    frame = pd.DataFrame(
        {
            "REPORT_DATE": ["2025-12-31", "2026-03-31", "2026-06-30"],
            "revenue": [1, 2, 3],
        }
    )

    result = _filter_df_by_report_period(
        frame, "2026-01-01", "2026-04-30"
    )

    assert result["revenue"].tolist() == [2]


def test_akshare_provider_date_filter_supports_both_bounds():
    frame = pd.DataFrame({
        "信用交易日期": ["20260730", "20260731", "20260803"],
        "融资余额": [1, 2, 3],
    })
    result = _filter_by_date(
        frame, "2026-07-31", "2026-07-31", date_cols=["信用交易日期"]
    )
    assert result["融资余额"].tolist() == [2]


@pytest.mark.asyncio
async def test_akshare_financial_multi_period_real_data():
    """Live free providers; same-period partial-table checks use real table subsets."""
    if os.environ.get("RUN_FREE_DATA_LIVE") != "1":
        pytest.skip("Set RUN_FREE_DATA_LIVE=1 for public financial providers")
    from app.data.sources.cn.akshare.api.financial import fetch_financial_data
    result = await fetch_financial_data(
        "000503", "2026-01-01", "2026-06-30"
    )
    assert not result.empty
    assert set(result["report_period"]).issubset({"2026-03-31", "2026-06-30"})
    assert result["report_period"].is_unique
    assert result["revenue"].notna().any()
    assert result["monetary_unit"].eq("元").all()
    assert result["available_statements"].str.len().gt(0).all()


def test_mcp_env_placeholder_expansion_and_missing_key_disable(tmp_path):
    previous = os.environ.pop("TEST_MX_SECRET", None)
    try:
        assert _expand_env_value("${TEST_MX_SECRET:-fallback}") == "fallback"
        config_path = tmp_path / "mcp.json"
        config_path.write_text(
            '{"mcpServers":{"mx":{"type":"streamable-http",'
            '"url":"https://example.com/mcp","headers":'
            '{"em_api_key":"${TEST_MX_SECRET}"},"_enabled":true}}}',
            encoding="utf-8",
        )
        config = load_mcp_config(config_path)
        assert config["mcpServers"]["mx"]["_enabled"] is False
    finally:
        if previous is not None:
            os.environ["TEST_MX_SECRET"] = previous


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_FREE_DATA_LIVE") != "1",
    reason="requires an explicitly configured live DuckDB HTTP data service",
)
async def test_real_free_sources_are_reachable():
    duckdb = JinceDuckDBCNProvider()
    assert await duckdb.connect() is True
    daily = await duckdb.get_daily_quotes(
        "000001", "2026-07-27", "2026-07-31"
    )
    assert not daily.empty
    assert daily.iloc[-1]["close"] > 0

    tencent = await fetch_market_quotes(["000001"])
    assert not tencent.empty
    assert tencent.iloc[0]["price"] > 0
