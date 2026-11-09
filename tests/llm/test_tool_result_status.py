"""新版工具循环的结果语义分类回归测试。"""

import pytest

from app.llm.core.types import ToolUseBlock
from app.llm.runner import _execute_partitioned
from app.llm.tools.result_status import classify_tool_result


@pytest.mark.parametrize(
    "result",
    [
        "❌ 工具 x 调用失败\n错误详情: timeout",
        '{"message":"你的积分已用完，请购买套餐包"}',
        [{"type": "text", "text": "权限不足，无法访问该资源"}],
        {"success": False, "error": "denied"},
        {"status": "disabled", "message": "断路器已打开"},
    ],
)
def test_classifies_semantic_business_errors(result):
    assert classify_tool_result(result) == "error"


@pytest.mark.parametrize(
    "result",
    [None, [], {}, {"status": "success", "data": []}, "(空结果)", "暂无数据"],
)
def test_classifies_normal_empty_results(result):
    assert classify_tool_result(result) == "empty"


def test_classifies_real_data_as_success():
    assert classify_tool_result({"status": "success", "data": [{"close": 25.1}]}) == "success"
    assert classify_tool_result("真实行情数据") == "success"


@pytest.mark.asyncio
async def test_runner_executes_ad_hoc_async_tool_def():
    """新版 runner 应执行异步 ToolDef，而不依赖已删除的 LangChain executor。"""
    from app.llm.core.types import ToolDef
    from app.llm.tools.registry import ToolRegistry

    received = []

    async def search_news(query: str):
        received.append(query)
        return '{"data":[{"title":"测试新闻"}]}'

    tool = ToolDef(
        name="mcp__mx-ds-mcp__mx_finance_search_news",
        description="搜索新闻",
        params_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=search_news,
    )
    outputs = await _execute_partitioned(
        ToolRegistry(),
        [ToolUseBlock(id="call-1", name=tool.name, input={"query": "国新健康"})],
        {tool.name: False},
        extra_defs={tool.name: tool},
    )

    assert received == ["国新健康"]
    assert classify_tool_result(outputs[0]) == "success"
