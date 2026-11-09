"""工具结果的协议无关语义分类。

部分数据服务会在 HTTP/MCP 调用成功时，把配额、权限或业务错误放在正文中；
另一些工具用 ``status=success`` 包装空数组。统一在这里识别，避免各执行路径
只检查字符串前缀而把业务错误当成有效数据。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

ToolResultStatus = Literal["success", "empty", "error"]

_ERROR_MARKERS = (
    "积分已用完",
    "购买套餐包",
    "额度已用完",
    "权限不足",
    "无权访问",
    "quota exceeded",
    "insufficient quota",
    "permission denied",
    "access denied",
    "工具执行失败",
    "工具调用失败",
    "mcp 工具执行错误",
    "数据获取失败",
    "分析失败",
)

_EMPTY_EXACT = {
    "",
    "none",
    "null",
    "[]",
    "{}",
    "(空结果)",
    "空结果",
    "暂无数据",
    "无数据",
    "no data",
    "no results",
    "empty",
}


def _string_status(value: str) -> ToolResultStatus:
    text = value.strip()
    lowered = text.lower()
    # Parse protocol envelopes before scanning prose. A valid announcement may
    # mention "没有公告" or quote an error; that does not make the whole result empty.
    if text[:1] in "[{" and text:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            return classify_tool_result(parsed)
    if lowered in _EMPTY_EXACT:
        return "empty"
    if text.startswith("❌") or text.startswith("错误：") or lowered.startswith("error:"):
        return "error"
    if any(marker in lowered for marker in _ERROR_MARKERS):
        return "error"
    if re.search(r"(?:暂无|没有|未找到|无法获取)[^。；\n]{0,12}(?:数据|记录|结果|新闻|公告)", text):
        return "empty"

    return "success"


def classify_tool_result(value: Any) -> ToolResultStatus:
    """把任意工具返回值分类为 ``success``、``empty`` 或 ``error``。"""
    if value is None:
        return "empty"
    if isinstance(value, str):
        return _string_status(value)
    if isinstance(value, (bytes, bytearray)):
        return _string_status(value.decode("utf-8", errors="replace"))
    if isinstance(value, dict):
        if not value:
            return "empty"

        status = str(value.get("status", "")).strip().lower()
        if status in {"error", "failed", "failure", "disabled", "denied"}:
            return "error"
        if status in {"no_data", "empty", "no_results"}:
            return "empty"
        if value.get("success") is False:
            return "error"
        error_value = value.get("error")
        if error_value not in (None, "", False, [], {}):
            return "error"

        # message 常用于承载 HTTP-200 业务错误，需先检查其语义。
        message = value.get("message")
        if message not in (None, "") and classify_tool_result(str(message)) == "error":
            return "error"

        # 显式数据容器为空时，即使外层 status=success 也仍是空数据。
        for key in ("data", "items", "records", "results", "rows"):
            if key in value and value[key] in (None, "", [], {}):
                return "empty"

        child_states = [
            classify_tool_result(item)
            for key, item in value.items()
            if key not in {"status", "success", "limitations", "warnings"}
            and not (value.get("title") and "publish_time" in value and key in {"title", "content", "summary"})
        ]
        if "error" in child_states:
            return "error"
        if child_states and all(state == "empty" for state in child_states):
            return "empty"
        return "success"
    if isinstance(value, (list, tuple, set)):
        if not value:
            return "empty"
        states = [classify_tool_result(item) for item in value]
        if "error" in states:
            return "error"
        if all(state == "empty" for state in states):
            return "empty"
        return "success"
    return "success"


def tool_result_is_error(value: Any) -> bool:
    """供 runner / 事件流使用的布尔快捷函数。"""
    return classify_tool_result(value) == "error"
