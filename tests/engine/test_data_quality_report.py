from app.engine.data_quality_report import render_data_quality
from app.engine.tools.common.data_access import needs_bounded_refresh


def test_coverage_is_not_confidence_and_stale_is_visible():
    context = {"as_of": "2026-10-07", "last_actual_trade_date": "2026-09-30"}
    output = render_data_quality(context, [{"domain": "margin_trading", "quality": {
        "status": "partial", "cache_freshness": "stale", "data_cutoff": "2026-09-30",
        "missing_fields": ["margin_repay_amount"], "sources": ["akshare"]}}])
    assert "margin_repay_amount" in output
    assert "stale" in output
    assert "不表示报告正确率" in output
    assert "70%" not in output


def test_current_stale_needs_refresh_but_complete_history_does_not():
    result = {"quality": {"status": "success"}, "freshness": "stale"}
    assert needs_bounded_refresh(result, "CN")
    assert not needs_bounded_refresh(result, "CN", "2000-01-01")
    assert not needs_bounded_refresh({**result, "freshness": "fresh"}, "CN")
    assert needs_bounded_refresh({"quality": {"status": "partial"}}, "CN", "2000-01-01")


def test_missing_reads_never_imply_full_coverage():
    assert "不得推断数据完整" in render_data_quality({}, [])
