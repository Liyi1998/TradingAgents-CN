"""Deterministic recovery boundaries; no network or SDK mocks."""

import asyncio

import pytest

from app.llm.core.errors import AuthError, ContextWindowExceededError, LLMError
from app.llm.failures import describe_failure
from app.llm.protocols.openai_client import OpenAILLMClient
from app.llm.retry import _should_retry, with_retry


@pytest.mark.parametrize("error,kind,retryable", [
    ("Error code: 504 upstream_first_byte_timeout", "upstream_timeout", True),
    ("peer closed connection without sending complete message body (incomplete chunked read)", "stream_interrupted", True),
    ("Connection error.", "connection", True),
    ("Error code: 429", "rate_limit", True),
    ("Error code: 429 usage_limit_reached", "quota_exhausted", False),
    ("Error code: 401 invalid_api_key", "authentication", False),
    ("Our servers are currently overloaded. Please try again later.", "upstream_overloaded", True),
    ("overloaded_error", "upstream_overloaded", True),
    ("Error code: 503 no_available_account", "model_account_unavailable", True),
    ("No compatible account is available for this model", "model_account_unavailable", True),
    ("no_available_account insufficient_quota", "quota_exhausted", False),
    ("Error code: 401 no_available_account", "authentication", False),
    ("context_length_exceeded", "context", False),
    ("Error code: 400 No tool output found for function call call_private", "tool_history", False),
    ("⚠️ 未能生成有效报告", "empty_response", True),
    (None, "invalid_report", False),
])
def test_safe_failure_classification(error, kind, retryable):
    failure = describe_failure(error)
    assert failure["kind"] == kind
    assert failure["retryable"] is retryable
    assert "invalid_api_key" not in failure["message"]


@pytest.mark.parametrize("error", [
    AuthError("bad key"), ContextWindowExceededError("too long"),
    LLMError("bad request", status_code=400), LLMError("forbidden", status_code=403),
    LLMError("usage_limit_reached", status_code=429),
])
def test_permanent_errors_are_not_retried(error):
    assert not _should_retry(error)


@pytest.mark.parametrize("error", ["currently overloaded", "no_available_account"])
def test_gateway_failures_are_safe_and_stable_through_report_display(error):
    failure = describe_failure(error + " private-token-do-not-display")
    assert "private-token" not in failure["message"]
    assert describe_failure(failure["message"])["kind"] == failure["kind"]


def test_retry_is_bounded_and_reports_attempt():
    calls, notifications = [], []

    async def operation():
        calls.append(1)
        raise LLMError("upstream_first_byte_timeout", status_code=504)

    async def notify(attempt, error, delay):
        notifications.append((attempt, delay))

    with pytest.raises(LLMError):
        asyncio.run(with_retry(operation, max_retries=1, on_retry=notify))
    assert len(calls) == 2
    assert notifications == [(1, 500)]


def test_zero_retries_means_one_attempt():
    calls = []

    async def operation():
        calls.append(1)
        raise LLMError("流中断")

    with pytest.raises(LLMError):
        asyncio.run(with_retry(operation, max_retries=0))
    assert len(calls) == 1


def test_sdk_does_not_multiply_application_retries():
    client = OpenAILLMClient(api_key="local-constructor-test", base_url="http://localhost:51133/v1", model="test")
    assert client._client.max_retries == 0
    asyncio.run(client._client.close())
