"""Pure checks using real registry declarations; no provider or LLM mocks."""
import asyncio

import pytest

from app.engine.data_preflight import declare_role, record_tool, render_role_checks, summarize_result
from app.engine.data_quality_report import render_data_quality
from app.engine.tools.datasources.registry import get_specs_by_ids


def test_requirements_come_from_tools_not_custom_role_names():
    context = {}
    row = declare_role(context, "custom-short-funds", ["news", "unregistered"], get_specs_by_ids(["news"]))
    assert [item["tool_id"] for item in row["tools"]] == ["news", "unregistered"]
    assert row["tools"][1]["status"] == "unknown_capability"
    assert "margin_trading" not in str(row)
    record_tool(context, "custom-short-funds", "news", "failed", "timeout")
    output = render_role_checks(context)
    assert "timeout" in output and "不得表述为可执行结论" in output


def test_no_declared_tools_and_reused_reports_never_claim_complete():
    context = {}
    assert declare_role(context, "custom", [], [])["limitations"]
    row = declare_role(context, "reused", ["news"], get_specs_by_ids(["news"]), reused=True)
    assert row["tools"][0]["status"] == "not_rechecked_reused_report"
    assert "not_rechecked_reused_report" in render_data_quality(context, [])


def test_actual_facade_partial_is_not_hidden_by_success_wrapper():
    context = {"market": "CN", "as_of": "2026-10-07"}
    read = {"domain": "news", "market": "CN", "symbol": "000503", "end_date": "2026-10-07", "quality": {"status": "partial"}}
    assert summarize_result({"status": "success", "data": "returned data"}, ["news"], context,
                            coverage=(read,), args={"stock_code": "000503"})[0] == "partial"
    assert summarize_result({"status": "success", "data": "returned data"}, ["news"], {})[0] == "returned_unverified"
    assert summarize_result({"status": "success", "data": []}, ["news"], {})[0] == "empty"
    assert summarize_result({"status": "error", "data": "quota exceeded"}, ["news"], {})[0] == "failed"


def test_role_coverage_cannot_borrow_another_symbol_date_or_missing_domain():
    result = {"status": "success", "data": "returned data"}
    read = {"domain": "daily_quotes", "market": "CN", "symbol": "000503", "end_date": "2026-10-07", "quality": {"status": "success"}}
    context = {"market": "CN", "as_of": "2026-10-07", "coverage_snapshots": {"another-call": read}}
    args = {"symbol": "000503", "end_date": "2026-10-07"}
    assert summarize_result(result, ["daily_quotes"], context, args=args)[0] == "returned_unverified"
    assert summarize_result(result, ["daily_quotes"], context, args=args, coverage=(read,))[0] == "available"
    assert summarize_result(result, ["daily_quotes", "financial_data"], context, args=args, coverage=(read,))[0] == "partial"
    for changed in ({"symbol": "000001.SH"}, {"end_date": "2025-10-07"}, {"market": "US"}):
        assert summarize_result(result, ["daily_quotes"], context, args=args, coverage=({**read, **changed},))[0] == "returned_unverified"


def test_successful_reread_replaces_the_same_failed_query_only():
    result = {"status": "success", "data": "returned data"}
    read = {"domain": "news", "market": "CN", "symbol": "000503", "end_date": "2026-10-07", "quality": {"status": "partial"}}
    context = {"market": "CN", "as_of": "2026-10-07"}
    assert summarize_result(result, ["news"], context, args={"stock_code": "000503"},
                            coverage=(read, {**read, "quality": {"status": "success"}}))[0] == "available"


def test_classification_coverage_survives_quality_appendix_without_news_text():
    output = render_data_quality({"news_classification_coverage": {
        "CN:000503:2026-09-01:2026-10-01:15": {"label_source_counts": {"provider": 1, "local_rule": 2, "unknown": 3}}
    }}, [])
    assert "local_rule" in output and "不是社交情绪或置信度" in output


@pytest.mark.asyncio
async def test_shared_invocation_propagates_real_trace_helper_to_thread_once():
    from app.data.core.analysis_context import record_read_trace
    from app.engine.data_preflight import ACTIVE_PREFLIGHT, PreflightBudgetExceeded, PreflightSession, invoke_shared
    from app.engine.tools.datasources.registry import DatasourceSpec

    # Exercise the production trace helper itself, not a replacement provider.
    spec = DatasourceSpec("trace_helper", "trace helper", ["news"], ["CN"], record_read_trace, {}, "trace helper")
    session = PreflightSession(max_calls=1)
    token = ACTIVE_PREFLIGHT.set(session)
    item = {"domain": "news", "symbol": "000503", "quality": {"status": "success"}}
    try:
        first, second = await asyncio.gather(invoke_shared(spec, {"item": item}), invoke_shared(spec, {"item": item}))
        assert first is second
        assert len(session.calls) == 1
        assert len(first.coverage) == 1
        item["quality"]["status"] = "changed_after_return"
        assert first.coverage[0]["quality"]["status"] == "success"
        with pytest.raises(PreflightBudgetExceeded):
            await invoke_shared(spec, {"item": {"domain": "news", "symbol": "002241"}})
    finally:
        await session.close()
        ACTIVE_PREFLIGHT.reset(token)
