"""Submission/recovery contracts and real loopback transport failure; no model mocks."""

import asyncio
import socket

import pytest

from app.engine.orchestrator.agents import _await_analyst_conversation
from app.engine.orchestrator.pipeline import _require_valid_reports, AnalysisQualityError
from app.engine.orchestrator.workflow.submission import (
    Submission, SubmissionBox, finalize_submission, make_submit_report_tool,
)
from app.llm.core.errors import LLMError
from app.llm.protocols.openai_client import OpenAILLMClient
from app.llm.runner import RunResult, run_conversation


REPORT = "# 市场技术分析\n已取得日线与技术快照；局部数据不足如实披露，不授权买入。"


async def submit(box, state, content=REPORT):
    tool = make_submit_report_tool(
        box, node_type="analyst", report_key="market_report", state=state,
    )
    await tool.handler(content=content)


async def fail(error):
    raise error


@pytest.mark.parametrize("horizon", ["short", "medium", "long"])
@pytest.mark.parametrize("error", [
    LLMError("Our servers are currently overloaded. Please try again later."),
    LLMError("no_available_account", status_code=503),
    TimeoutError("timed out"), ConnectionError("connection reset"),
])
def test_explicit_submission_survives_wrapup_failure(horizon, error):
    async def check():
        box = SubmissionBox()
        state = {"analysis_horizon": horizon, "reports": {"news_report": REPORT}}
        await submit(box, state)
        original = box.value
        result = await _await_analyst_conversation(fail(error), box, agent_key="market")
        assert result is None
        assert finalize_submission(box) is original
        assert state["reports"] == {"news_report": REPORT, "market_report": REPORT}
        _require_valid_reports(state["reports"], ["news_report", "market_report"], "基础分析阶段")
    asyncio.run(check())


@pytest.mark.parametrize("submission", [
    None, Submission(""), Submission("❌ 分析失败：no_available_account"),
    Submission("无法唯一识别目标证券"), Submission(REPORT, source="fallback_text"),
])
def test_missing_invalid_or_uncommitted_report_does_not_hide_failure(submission):
    async def check():
        box = SubmissionBox()
        if submission is not None:
            box.set(submission)
        with pytest.raises(LLMError, match="overloaded"):
            await _await_analyst_conversation(
                fail(LLMError("currently overloaded")), box, agent_key="market",
            )
    asyncio.run(check())


@pytest.mark.parametrize("error", [ValueError("bug"), asyncio.CancelledError()])
def test_programming_errors_and_cancellation_are_not_salvaged(error):
    async def check():
        box = SubmissionBox()
        await submit(box, {})
        with pytest.raises(type(error)):
            await _await_analyst_conversation(fail(error), box, agent_key="market")
    asyncio.run(check())


def test_normal_result_is_unchanged():
    async def check():
        result = RunResult(final_text=REPORT, turns=1, stop_reason="end_turn")
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(result)
        assert await _await_analyst_conversation(completed, SubmissionBox(), agent_key="market") is result
    asyncio.run(check())


@pytest.mark.parametrize("has_submission", [False, True])
def test_real_sdk_connection_failure_preserves_only_submitted_report(has_submission):
    async def check():
        # A reserved, non-listening local port causes real connection refusal.
        # No external endpoint, fake server, scripted client, or model invocation.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            client = OpenAILLMClient(
                api_key="local-transport-test", model="local-transport-test",
                base_url=f"http://127.0.0.1:{reserved.getsockname()[1]}/v1", timeout=1,
            )
            box, state = SubmissionBox(), {}
            try:
                if has_submission:
                    await submit(box, state)
                operation = _await_analyst_conversation(
                    run_conversation(client, "end", tools=[], max_turns=1, retry_times=0),
                    box, agent_key="market",
                )
                if has_submission:
                    assert await operation is None
                    assert finalize_submission(box).content == REPORT
                    _require_valid_reports(state["reports"], ["market_report"], "基础分析阶段")
                else:
                    with pytest.raises(LLMError):
                        await operation
                    with pytest.raises(AnalysisQualityError):
                        _require_valid_reports({}, ["market_report"], "基础分析阶段")
            finally:
                await client._client.close()
    asyncio.run(check())


@pytest.mark.parametrize("error,expected", [
    ("Our servers are currently overloaded. Please try again later.", "模型上游服务器过载"),
    ("Error code: 503 no_available_account", "模型网关暂无可用的兼容账号"),
])
def test_quality_gate_displays_specific_safe_failure(error, expected):
    with pytest.raises(AnalysisQualityError) as captured:
        _require_valid_reports(
            {"market_report": f"❌ 分析失败：{error}", "news_report": REPORT},
            ["market_report", "news_report"], "基础分析阶段",
        )
    assert expected in str(captured.value)
    assert "已有 1 份报告正文保留" in str(captured.value)
