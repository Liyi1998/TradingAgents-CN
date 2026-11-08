"""Real bridge transformations; storage integration lives in tests/data."""
import inspect
import pytest
from app.engine.tools.datasources import capital_flow, market

@pytest.mark.parametrize("tool", [capital_flow.get_money_flow, capital_flow.get_margin_trade])
def test_capital_tools_normalize_compact_dates(tool):
    assert "symbol" in inspect.signature(tool).parameters
    assert "ts_code" not in inspect.signature(tool).parameters
    assert capital_flow._normalize_storage_date("20260701") == "2026-07-01"
    assert capital_flow._normalize_storage_date("20260807") == "2026-08-07"
    with pytest.raises(ValueError):
        capital_flow._normalize_storage_date("20260230")

def test_money_flow_free_source_priority_and_conflict_notice():
    text = capital_flow._format_money_flow([
        {"symbol": "600332", "trade_date": "2026-08-07", "data_source": "mx_mcp", "main_net_inflow": -1860300},
        {"symbol": "600332", "trade_date": "2026-08-07", "data_source": "sina", "main_net_inflow": 3531780.36},
    ], "资金流")
    assert "不能相互覆盖或直接相加" in text
    assert "sina, mx_mcp" in text
    assert "3531780.36" in text and "-1860300" in text

def test_intraday_tool_aggregates_one_minute_bars():
    raw = [dict(symbol="600332", market="CN", data_source="tencent", freq="1min",
                ohlc_kind="native_bar", open=10., high=10.4, low=9.9, close=10.1,
                volume=100, amount=1000, datetime=f"2026-08-07 {stamp}")
           for stamp in ("09:31:00", "09:59:00", "10:01:00")]
    frame = market._prepare_intraday_bars(raw, "30min")
    assert list(frame.datetime) == ["2026-08-07 10:00:00", "2026-08-07 10:30:00"]
    assert list(frame.volume) == [200, 100]
    assert not frame.bar_complete.any()
    assert frame.iloc[0].high == 10.4
    assert market._prepare_intraday_bars([{**row, "ohlc_kind": "last_price_sample"} for row in raw], "30min") is None
