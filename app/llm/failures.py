"""Safe, deterministic failure classification for retries and user-facing errors."""

from __future__ import annotations

import re
from typing import Any


def describe_failure(error: Any, *, status_code: int | None = None) -> dict:
    parts = [type(error).__name__, str(error or "")]
    cause = getattr(error, "__cause__", None)
    for _ in range(4):
        if cause is None:
            break
        parts.extend([type(cause).__name__, str(cause)])
        cause = getattr(cause, "__cause__", None)
    text = " ".join(parts).lower()
    status = status_code if status_code is not None else getattr(error, "status_code", None)
    if any(s in text for s in ("usage_limit_reached", "insufficient_quota", "quota_exceeded", "quota_exhausted", "individual quota reached")):
        return {"kind": "quota_exhausted", "retryable": False, "message": "模型额度已用尽（quota_exhausted），请等待额度恢复或选择可用模型；已提交的有效报告应保留"}
    if status in (401, 403) or any(s in text for s in ("invalid_api_key", "authentication", "error code: 401", "error code: 403")):
        return {"kind": "authentication", "retryable": False, "message": "模型鉴权失败，请核对该模型的密钥和权限"}
    if any(s in text for s in ("no_available_account", "no compatible account is available for this model")):
        return {"kind": "model_account_unavailable", "retryable": True,
                "message": "模型网关暂无可用的兼容账号（no_available_account），请检查账号可用状态和模型支持；这不等于已确认额度耗尽"}
    if any(s in text for s in ("currently overloaded", "overloaded_error", "server_overloaded")):
        return {"kind": "upstream_overloaded", "retryable": True,
                "message": "模型上游服务器过载（server_overloaded），请稍后重试；这不是行情数据缺失的证据"}
    if "upstream_first_byte_timeout" in text:
        return {"kind": "upstream_timeout", "retryable": True, "message": "模型网关上游首段响应超时（504），并非 Jev 或新闻数据源鉴权失败"}
    if any(s in text for s in ("tls handshake", "ssl handshake", "sslerror", "certificate verify failed")):
        upstream = status in (502, 503, 504) or "upstream" in text or "error code: 502" in text
        return {"kind": "upstream_tls" if upstream else "tls", "retryable": True,
                "message": "模型网关连接上游时 TLS 握手失败，请检查网关的上游网络/代理；这不是本地网关未启动或额度不足的证据" if upstream else "模型连接的 TLS 握手失败，请检查目标地址、证书和网络/代理；不要关闭证书校验"}
    if status == 429 or "rate_limit" in text or "error code: 429" in text:
        return {"kind": "rate_limit", "retryable": True, "message": "模型服务限流（429）"}
    if any(s in text for s in ("context_length_exceeded", "context window", "prompt is too long")):
        return {"kind": "context", "retryable": False, "message": "模型输入超过上下文限制，需要压缩输入"}
    if any(s in text for s in ("no tool output found for function call", "tool_call_ids", "tool response must")):
        return {"kind": "tool_history", "retryable": False, "message": "模型工具调用与结果的配对或消息顺序不正确（400），需修复对话转换后重试"}
    if any(s in text for s in ("incomplete chunked read", "stream disconnected", "stream closed", "流中断", "peer closed")):
        return {"kind": "stream_interrupted", "retryable": True, "message": "模型响应流中途断开，未收到完整正文"}
    if "connecttimeout" in text or "connection to" in text and "timed out" in text:
        return {"kind": "connection_timeout", "retryable": True, "message": "连接配置的模型网关超时，请检查网关进程、地址和网络；尚未收到模型响应"}
    if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower() or any(s in text for s in ("timed out", "timeout")):
        return {"kind": "response_timeout", "retryable": True, "message": "等待模型响应超时，请检查上游响应速度和网络；不等于模型网关未启动"}
    if isinstance(error, ConnectionError) or any(s in text for s in ("connection error", "connection reset", "connection refused", "failed to establish a new connection", "连接失败")):
        return {"kind": "connection", "retryable": True, "message": "无法连接模型服务，请检查配置的地址、网关进程和网络"}
    if status in (408, 409, 500, 502, 503, 504, 529) or re.search(r"(?:error code:|http)\s*(?:408|409|500|502|503|504|529)\b", text):
        return {"kind": "upstream_unavailable", "retryable": True, "message": "模型上游服务暂时不可用"}
    if any(s in text for s in ("未生成报告", "未生成有效", "未能生成有效", "空响应", "空正文")):
        return {"kind": "empty_response", "retryable": True, "message": "模型未返回有效报告正文"}
    return {"kind": "invalid_report", "retryable": False, "message": "报告缺失或生成失败，请查看该分析师的具体错误"}
