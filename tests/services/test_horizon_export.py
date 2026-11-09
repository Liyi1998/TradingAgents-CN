"""PDF/Word use this same Markdown source and must retain the saved period."""
from copy import deepcopy

import pytest

from app.utils.report_exporter import ReportExporter


@pytest.mark.parametrize("horizon,label", [("short", "短线"), ("medium", "中线（1–3个月）"), ("long", "长线（6–12个月）")])
def test_export_source_uses_saved_period(horizon, label):
    report = {"stock_symbol": "000503", "analysis_date": "2026-10-09",
              "analysis_horizon": horizon, "summary": "仅为测试报告摘要", "reports": {}}
    before = deepcopy(report)
    text = ReportExporter().generate_markdown_report(report)
    assert f"**分析周期**: {label}" in text
    assert report == before


def test_legacy_export_is_labelled_short_without_rewriting_report():
    report = {"stock_symbol": "000503", "reports": {}}
    assert "**分析周期**: 短线" in ReportExporter().generate_markdown_report(report)
    assert "analysis_horizon" not in report
