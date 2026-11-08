"""Real message serialization contracts for multi-round tool conversations."""

import asyncio

import pytest

from app.llm.core.types import Message, Role, TextBlock, ToolResultBlock, ToolUseBlock
from app.llm.protocols.openai_client import OpenAILLMClient


@pytest.fixture
def client():
    instance = OpenAILLMClient(
        api_key="serialization-only", base_url="http://localhost:51133/v1", model="gpt-6.1-sol"
    )
    yield instance
    asyncio.run(instance._client.close())


def test_three_tool_rounds_keep_each_output_before_next_assistant(client):
    # Shape of the failed 603232 task: two tool-only rounds, then text + calculation.
    messages = [Message(Role.USER, "分析格尔软件")]
    for index in range(3):
        blocks = [ToolUseBlock(f"call_{index}", "finance" if index < 2 else "calc_batch", {})]
        if index == 2:
            blocks.insert(0, TextBlock("数据源不可用，使用已取得的数据计算。"))
        messages += [
            Message(Role.ASSISTANT, blocks),
            Message(Role.USER, [ToolResultBlock(
                f"call_{index}", "工具暂时不可用" if index < 2 else "MA5=16.34", index < 2
            )]),
        ]
    api = client._to_api_messages(messages, "系统指令")
    assert [m["role"] for m in api] == [
        "system", "user", "assistant", "tool", "assistant", "tool", "assistant", "tool"
    ]
    for index in range(3):
        call, output = api[2 + index * 2:4 + index * 2]
        assert call["tool_calls"][0]["id"] == output["tool_call_id"] == f"call_{index}"
    assert api[-2]["content"] == "数据源不可用，使用已取得的数据计算。"


def test_same_message_multiple_calls_and_text_stay_together(client):
    messages = [
        Message(Role.ASSISTANT, [
            TextBlock("先检查行情。"), ToolUseBlock("a", "quote", {}),
            TextBlock("再检查指标。"), ToolUseBlock("b", "indicators", {}),
        ]),
        Message(Role.USER, [
            TextBlock("请继续。"), ToolResultBlock("a", "16.13"), ToolResultBlock("b", "MA5=16.34"),
        ]),
    ]
    api = client._to_api_messages(messages, None)
    assert [m["role"] for m in api] == ["assistant", "tool", "tool", "user"]
    assert [call["id"] for call in api[0]["tool_calls"]] == ["a", "b"]
    assert api[0]["content"] == "先检查行情。再检查指标。"
    assert [m["tool_call_id"] for m in api[1:3]] == ["a", "b"]
    assert api[3]["content"] == "请继续。"


def test_tool_output_stays_before_system_and_final_answer(client):
    api = client._to_api_messages([
        Message(Role.ASSISTANT, [ToolUseBlock("a", "quote", {})]),
        Message(Role.USER, [ToolResultBlock("a", "16.13")]),
        Message(Role.SYSTEM, "继续分析"),
        Message(Role.ASSISTANT, "分析正文"),
    ], None)
    assert [m["role"] for m in api] == ["assistant", "tool", "system", "assistant"]
