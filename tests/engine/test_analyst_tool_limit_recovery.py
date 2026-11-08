"""分析师工具预算与无工具强制收尾回归测试。"""

import pytest

import app.engine.orchestrator.agents as agents_module
from app.engine.orchestrator.agents import AnalystSpec, run_analyst
from app.llm.core.base import BaseLLMClient, StreamEvent
from app.llm.core.types import (
    ChatResponse,
    Message,
    Role,
    StopReason,
    ToolDef,
    ToolUseBlock,
    Usage,
)


class ToolLoopThenFinalClient(BaseLLMClient):
    """首轮批量请求工具，第二轮在 tools=[] 时输出正式报告。"""

    protocol = "test"
    model = "test-model"

    def __init__(self):
        self.requests = []
        self.responses = [
            ChatResponse(
                message=Message(
                    role=Role.ASSISTANT,
                    content=[
                        ToolUseBlock(
                            id=f"calc_{index}",
                            name="calc_expression",
                            input={"expression": f"{index} + 1"},
                        )
                        for index in range(5)
                    ],
                ),
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(input_tokens=100, output_tokens=20),
                model=self.model,
            ),
            ChatResponse(
                message=Message(role=Role.ASSISTANT, content="# 完整财经新闻分析报告\n\n结论：中性。"),
                stop_reason=StopReason.END_TURN,
                usage=Usage(input_tokens=120, output_tokens=30),
                model=self.model,
            ),
        ]

    async def chat(self, messages, **kwargs):
        return self.responses.pop(0)

    async def chat_stream(self, messages, **kwargs):
        self.requests.append({"messages": list(messages), **kwargs})
        yield StreamEvent("message", response=self.responses.pop(0))

    async def count_tokens(self, messages):
        return 16


class NoopRecorder:
    def record(self, **kwargs):
        return None


@pytest.mark.asyncio
async def test_financial_news_caps_tools_then_forces_text_report(monkeypatch):
    async def resolve_company(_ticker):
        return "测试公司"

    async def no_fresh_news(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agents_module, "_resolve_company", resolve_company)
    monkeypatch.setattr(agents_module, "_prefetch_mx_news_if_stale", no_fresh_news)

    import app.services.token_usage_recorder as usage_module

    monkeypatch.setattr(usage_module, "token_usage_recorder", NoopRecorder())

    executed = []

    def calculate(expression: str) -> str:
        executed.append(expression)
        return f"{expression} = ok"

    tool = ToolDef(
        name="calc_expression",
        description="计算表达式",
        params_schema={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        handler=calculate,
    )
    spec = AnalystSpec(
        internal_key="financial_news",
        name="📰 财经新闻分析师",
        system_prompt="先取数再生成报告。",
        callable_tools=[tool],
        max_tool_calls=2,
    )
    client = ToolLoopThenFinalClient()

    state = {
        "task_id": "task-tool-limit",
        "user_id": "user-1",
        "company_of_interest": "300094",
        "trade_date": "2026-08-30",
    }
    result = await run_analyst(spec, client, state)

    assert result["financial_news_report"].startswith("# 完整财经新闻分析报告")
    assert executed == ["0 + 1", "1 + 1"]
    assert len(client.requests) == 2
    assert client.requests[0]["tools"]
    assert client.requests[1]["tools"] is None
    assert "calc_batch" in client.requests[0]["system"]
    assert "强制收尾阶段" in client.requests[1]["system"]

