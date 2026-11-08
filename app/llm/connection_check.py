"""Bounded model probes shared by the engine and configuration tests.

No retries or fallback calls: a probe must not multiply billable requests.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from .failures import describe_failure

DEFAULT_CHECK_TIMEOUT = 180.0
MAX_CHECK_TIMEOUT = 180.0
CONNECT_CHECK_TIMEOUT = 10.0
CHECK_MAX_TOKENS = 256


def require_check_text(content: Any) -> str:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型返回空正文或响应格式异常，未通过连通性检查")
    return content.strip()


def check_timeout(value: Any = None) -> float:
    """Honor shorter explicit budgets; invalid/unbounded values use a safe default."""
    if isinstance(value, bool):
        return DEFAULT_CHECK_TIMEOUT
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_CHECK_TIMEOUT
    if not math.isfinite(seconds) or seconds <= 0:
        return DEFAULT_CHECK_TIMEOUT
    return min(seconds, MAX_CHECK_TIMEOUT)


def client_check_timeout(client: Any, override: Any = None) -> float:
    primary = getattr(client, "primary", client)
    sdk = getattr(primary, "_client", None)
    value = getattr(sdk, "timeout", None)
    value = getattr(value, "read", value)
    budget = check_timeout(value)
    return min(budget, check_timeout(override)) if override is not None else budget


def check_identity(client: Any) -> tuple:
    """Deduplicate equivalent primary connections, never different credentials.

    The fingerprint is in-memory only. Unknown client implementations are not merged.
    """
    primary = getattr(client, "primary", client)
    sdk = getattr(primary, "_client", None)
    key = getattr(sdk, "api_key", None)
    base = getattr(sdk, "base_url", None)
    protocol = getattr(primary, "protocol", None)
    if not isinstance(key, str) or not base or protocol not in {"openai", "anthropic"}:
        return (id(primary),)
    return (
        protocol, str(base), getattr(primary, "model", None),
        hashlib.sha256(key.encode()).digest(),
        getattr(primary, "provider", None), getattr(primary, "temperature", None),
        getattr(primary, "thinking_effort", None), getattr(primary, "thinking_budget", None),
        client_check_timeout(client),
    )


def check_failure(error: Any, timeout: float, *, status_code: int | None = None) -> dict:
    """Return safe actionable messages, never raw upstream bodies/URLs/credentials."""
    failure = describe_failure(error, status_code=status_code)
    kind = failure["kind"]
    if kind == "response_timeout":
        message = f"模型检查等待响应超时（等待上限 {timeout:g} 秒），不等于网关未启动；请检查上游响应速度和网络"
    elif kind == "connection":
        message = "无法连接配置的模型网关，请检查网关进程、地址和网络"
    elif kind == "invalid_report":
        message = "模型连接检查未通过，响应格式或请求配置异常；请查看网关日志"
    else:
        message = failure["message"]
    return {**failure, "message": message}
