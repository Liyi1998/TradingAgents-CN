"""
MCP 工具桥接：发现 → ToolDef，调用 → call_tool（参考 claude-code mcp-tools.ts）

- 命名 `mcp__{server}__{tool}`，非法字符归一化 `[^a-zA-Z0-9_-]` → `_`
- inputSchema 直接透传；description 截 2048；readOnlyHint → is_concurrency_safe
- 调用：isError=True → 错误文本回传（不抛异常，交由模型自纠）
- 结果：text 优先，structuredContent JSON 化，100k 字符截断
"""

import json
import re
from typing import Any, Dict, List, Optional

from mcp import ClientSession

import logging

from ..core.types import ToolDef
from .client import MCPManager
from .config import MCPServerConfig

logger = logging.getLogger(__name__)

TOOL_NAME_PREFIX = "mcp__"
MAX_DESCRIPTION_CHARS = 2048
MAX_TOOL_RESULT_CHARS = 100_000

_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def mcp_tool_name(server: str, tool: str) -> str:
    return f"{TOOL_NAME_PREFIX}{_INVALID_CHARS.sub('_', server)}__{_INVALID_CHARS.sub('_', tool)}"


def _format_content(result, *, task_id: str = "") -> str:
    """call_tool 结果 → 文本：text 优先，structuredContent JSON 化；超限统一走 result_budget 落盘预览"""
    from ..tools.result_budget import apply_result_budget

    parts: List[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if not parts and structured:
        parts.append(json.dumps(structured, ensure_ascii=False))
    if not parts:
        return "(空结果)"
    return apply_result_budget("mcp_result", "\n".join(parts), task_id=task_id, max_chars=MAX_TOOL_RESULT_CHARS)


def make_mcp_tool_def(
    cfg: MCPServerConfig,
    manager: MCPManager,
    tool,
    *,
    task_manager: Optional[Any] = None,
) -> ToolDef:
    """把 server 端单个 tool 包装为本地 ToolDef（handler 绑定该 server）"""
    display_name = mcp_tool_name(cfg.name, tool.name)
    desc = (tool.description or f"MCP tool '{tool.name}' from server '{cfg.name}'")[:MAX_DESCRIPTION_CHARS]

    async def _call(*call_args, **call_kwargs) -> str:
        # registry 以位置参数传 input dict；直接调用时可能用关键字
        payload = call_args[0] if call_args and isinstance(call_args[0], dict) else call_kwargs

        async def _invoke() -> str:
            session: ClientSession = await manager.get_session(cfg)
            result = await session.call_tool(tool.name, arguments=payload or {})
            # mcp SDK 新版 CallToolResult 字段为 is_error（旧版 isError），双兼容避免属性错误
            is_error = getattr(result, "is_error", None)
            if is_error is None:
                is_error = getattr(result, "isError", False)
            formatted = _format_content(result)
            if is_error:
                return f"MCP 工具执行错误: {formatted}"
            return formatted

        try:
            if task_manager is not None:
                formatted = await task_manager.execute_tool(tool.name, _invoke, server_name=cfg.name)
            else:
                formatted = await _invoke()
        except Exception as exc:
            formatted = {"status": "error", "message": f"MCP调用不可用: {type(exc).__name__}"}

        from ..tools.result_status import classify_tool_result

        failure = isinstance(formatted, dict) and formatted.get("status") in {"error", "disabled"}
        if failure or classify_tool_result(str(formatted)) == "error":
            message = str(formatted.get("message") or formatted) if failure else str(formatted)
            from .free_fallback import builtin_fallback
            substitute = await builtin_fallback(tool.name, payload or {})
            suffix = "\n本地定向备用数据（非完整等价替代）：\n" + json.dumps(substitute, ensure_ascii=False) if substitute else ""
            return f"MCP 工具执行错误: {message}{suffix}"
        formatted_text = str(formatted)
        if classify_tool_result(formatted_text) == "success":
            try:
                from app.services.mcp_data_cache import persist_mcp_result

                await persist_mcp_result(display_name, payload or {}, formatted_text)
            except Exception as cache_error:  # noqa: BLE001 - 写回失败不影响本次分析
                logger.warning("⚠️ [mcp] 妙想结果写回失败（继续分析）: %s", cache_error)
        return formatted_text

    annotations = getattr(tool, "annotations", None)  # annotations 可能为 None
    # mcp SDK 新版 annotations 字段为 read_only_hint（旧版 readOnlyHint），双兼容
    read_only = bool(
        annotations
        and (
            getattr(annotations, "read_only_hint", None)
            if getattr(annotations, "read_only_hint", None) is not None
            else getattr(annotations, "readOnlyHint", False)
        )
    )

    return ToolDef(
        name=display_name,
        description=desc,
        # mcp SDK 新版 Tool 字段为 input_schema（旧版 inputSchema），双兼容避免属性错误
        params_schema=(
            getattr(tool, "input_schema", None)
            or getattr(tool, "inputSchema", None)
            or {"type": "object", "properties": {}}
        ),
        handler=_call,
        is_concurrency_safe=read_only,
    )


async def discover_mcp_tools(
    manager: Optional[MCPManager] = None,
    servers: Optional[Dict[str, MCPServerConfig]] = None,
    config_path: Optional[str] = None,  # 保留参数兼容旧签名；实际配置源为 config/mcp.json
    task_manager: Optional[Any] = None,
) -> List[ToolDef]:
    """连接全部配置的 server 并发现工具，返回 ToolDef 列表"""
    mgr = manager or MCPManager()
    if servers is None:
        from .service import enabled_server_configs

        servers = enabled_server_configs()
    cfgs = servers
    defs: List[ToolDef] = []
    for name, cfg in cfgs.items():
        try:
            session = await mgr.connect(cfg)
            listed = await session.list_tools()
        except Exception as e:
            logger.warning(f"⚠️ [mcp] server '{name}' 连接/发现失败: {e}")
            continue
        for tool in listed.tools:
            try:
                defs.append(
                    make_mcp_tool_def(
                        cfg,
                        mgr,
                        tool,
                        task_manager=task_manager,
                    )
                )
            except Exception as e:  # noqa: BLE001 - 单工具包装失败跳过，不炸整个发现流程
                logger.warning(f"⚠️ [mcp] server '{name}' 工具 '{tool.name}' 包装失败: {e}")
        logger.info(f"[mcp] server '{name}' 发现 {len(listed.tools)} 个工具")
    return defs
