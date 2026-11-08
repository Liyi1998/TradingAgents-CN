"""Pure snapshot/date validation using actual models and parser, not I/O mocks."""

from datetime import datetime, timezone
import ast
from pathlib import Path

import pytest

from app.llm.events import REPORT_CONTENT_MAX_CHARS
from app.models.analysis import SingleAnalysisRequest
from app.services.analysis_retry import reusable_reports, same_day_parameters


def event(seq, key, content):
    return {"seq": seq, "payload": {"report_key": key, "content": content}}


def test_reuse_only_selected_complete_reports():
    events = [
        event(1, "market_report", "# 行情\n" + "有来源的行情与风险分析。" * 100),
        event(2, "financial_news_report", "❌ 分析失败：Error code: 504"),
        event(3, "other_report", "其他内容"),
        event(4, "social_media_report", "x" * REPORT_CONTENT_MAX_CHARS),
    ]
    result = reusable_reports(events, {"market_report", "financial_news_report", "social_media_report"})
    assert set(result) == {"market_report"}
    assert result["market_report"] == events[0]["payload"]["content"]


def test_latest_failure_cannot_revert_to_older_success():
    assert reusable_reports([
        event(1, "market_report", "# 有效报告"),
        event(2, "market_report", "❌ 分析失败"),
    ], {"market_report"}) == {}


def test_latest_success_replaces_failed_attempt():
    assert reusable_reports([
        event(1, "market_report", "❌ 分析失败"),
        event(2, "market_report", "# 已成功完成报告"),
    ], {"market_report"}) == {"market_report": "# 已成功完成报告"}


def test_private_snapshots_cannot_be_injected_by_client():
    request = SingleAnalysisRequest.model_validate({
        "symbol": "002241", "_resume_reports": {"market_report": "client injected"},
        "_resume_source_task_id": "forged",
    })
    assert request._resume_reports == {}
    assert request._resume_source_task_id is None
    request._resume_reports = {"market_report": "server validated"}
    assert "_resume_reports" not in request.model_dump()


def test_engine_does_not_read_deprecated_request_field_directly():
    # Architecture regression: callers may send only symbol (including recovery).
    source = Path(__file__).parents[2] / "app/services/analysis_service.py"
    tree = ast.parse(source.read_text())
    direct_legacy_reads = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "stock_code"
        and isinstance(node.value, ast.Name) and node.value.id == "request"
    ]
    assert direct_legacy_reads == []
    assert SingleAnalysisRequest(symbol="002241").get_symbol() == "002241"
    assert SingleAnalysisRequest(stock_code="002241").get_symbol() == "002241"


def test_same_china_day_preserves_parameters():
    source = {"parameters": {"analysis_date": "2026-09-22", "selected_analysts": ["financial-news"]}}
    result = same_day_parameters(source, datetime(2026, 9, 22, 10, tzinfo=timezone.utc))
    assert result.analysis_date == datetime(2026, 9, 22)
    assert result.selected_analysts == ["financial-news"]


def test_missing_date_uses_created_utc_converted_to_china():
    result = same_day_parameters({"created_at": datetime(2026, 9, 21, 17)}, datetime(2026, 9, 22, 0, tzinfo=timezone.utc))
    assert result.analysis_date == datetime(2026, 9, 22)


def test_cross_midnight_reuse_is_rejected():
    with pytest.raises(ValueError, match="跨分析日期"):
        same_day_parameters({"parameters": {"analysis_date": "2026-09-21"}}, datetime(2026, 9, 21, 17, tzinfo=timezone.utc))


def test_unknown_source_date_is_rejected():
    with pytest.raises(ValueError, match="缺少分析日期"):
        same_day_parameters({}, datetime.now(timezone.utc))
