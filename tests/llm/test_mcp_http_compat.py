"""MCP SDK 2.0/2.1 streamable HTTP 入口兼容性检查。"""


def test_installed_sdk_exposes_supported_streamable_http_client():
    from mcp.client import streamable_http as module

    modern = getattr(module, "streamable_http_client", None)
    legacy = getattr(module, "streamablehttp_client", None)
    assert modern is not None or legacy is not None
    if modern is not None:
        assert getattr(module, "create_mcp_http_client", None) is not None
