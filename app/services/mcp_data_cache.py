"""Compatibility exports for the MCP ingestion adapter now owned by the data layer."""
from app.data.sync.mcp_ingestion import (  # noqa: F401
    extract_sheets, normalize_mx_news, normalize_mx_financial, normalize_mx_money_flow,
)
from app.data.core.interface import DataInterface


async def is_news_fresh(symbol: str, max_age_hours: int = 24) -> bool:
    return await DataInterface.get_instance().mcp_news_is_fresh(symbol, max_age_hours)


async def persist_mcp_result(tool_name, tool_args, raw, context=None):
    return await DataInterface.get_instance().persist_mcp_result(tool_name, tool_args, raw, context)
