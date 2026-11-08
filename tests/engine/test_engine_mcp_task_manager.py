"""任务级 MCP 业务错误与断路回归测试。"""

import pytest

from app.llm.mcp.task_manager import TaskLevelMCPManager


@pytest.mark.asyncio
async def test_quota_exhaustion_disables_all_tools_on_same_server():
    manager = TaskLevelMCPManager("quota-test")

    async def exhausted_tool():
        return [{
            "type": "text",
            "text": '{"message":"你的积分已用完，请购买套餐包"}',
        }]

    result = await manager.execute_tool(
        "mx_finance_search_news",
        exhausted_tool,
        server_name="mx-ds-mcp",
    )

    assert result["status"] == "error"
    assert "免费数据源" in result["message"]
    assert await manager.is_tool_available(
        "mx_ashare_finance_data", "mx-ds-mcp"
    ) is False


@pytest.mark.asyncio
async def test_permission_error_is_classified_but_not_server_wide():
    manager = TaskLevelMCPManager("permission-test")

    async def denied_tool():
        return {"message": "权限不足，无法访问该资源"}

    result = await manager.execute_tool(
        "restricted_tool", denied_tool, server_name="mx-ds-mcp"
    )

    assert result["status"] == "error"
    assert await manager.is_tool_available("other_tool", "mx-ds-mcp") is True
