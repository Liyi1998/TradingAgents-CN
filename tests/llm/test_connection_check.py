"""Real clients, pure policies and local TCP failures; no mocks or paid requests."""

import asyncio
import os
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import requests

from app.llm.connection_check import (
    check_failure, check_identity, check_timeout, client_check_timeout, require_check_text,
)
from app.llm.core.errors import LLMError
from app.llm.protocols.openai_client import OpenAILLMClient
from app.llm.providers import EngineClientBundle
from app.services.config.llm_service import LLMService


@pytest.mark.parametrize("value,expected", [
    (None, 180), (0, 180), (-1, 180), (False, 180), (True, 180),
    ("", 180), ("bad", 180), (float("nan"), 180), (float("inf"), 180),
    (15, 15), (30, 30), (120, 120), (180, 180), (600, 180), ("150", 150),
])
def test_check_budgets_are_finite_and_honor_shorter_explicit_values(value, expected):
    assert check_timeout(value) == expected


@pytest.mark.parametrize("error,status,kind", [
    (TimeoutError(), None, "response_timeout"),
    (requests.exceptions.ReadTimeout(), None, "response_timeout"),
    (requests.exceptions.ConnectTimeout(), None, "connection_timeout"),
    ("utls: TLS handshake: EOF", 502, "upstream_tls"),
    (LLMError('Error code: 502 upstream_error TLS handshake: EOF sk-do-not-echo', status_code=502), None, "upstream_tls"),
    ("certificate verify failed", None, "tls"),
    ("upstream_first_byte_timeout", 504, "upstream_timeout"),
    ("invalid_api_key", 401, "authentication"),
    ("forbidden", 403, "authentication"),
    ("usage_limit_reached", 429, "quota_exhausted"),
    ("rate_limit", 429, "rate_limit"),
    ("unavailable", 503, "upstream_unavailable"),
    ("Connection refused sk-do-not-echo", None, "connection"),
    ("arbitrary error sk-do-not-echo", 400, "invalid_report"),
])
def test_check_errors_distinguish_connection_tls_timeout_auth_and_quota(error, status, kind):
    result = check_failure(error, 180, status_code=status)
    assert result["kind"] == kind
    assert "sk-do-not-echo" not in result["message"]
    if kind == "response_timeout":
        assert "180 秒" in result["message"]
    if status == 403:
        assert "额度" not in result["message"]


@pytest.mark.parametrize("content", [None, "", "  ", [], {}, 32])
def test_empty_or_invalid_probe_responses_never_pass(content):
    with pytest.raises(ValueError, match="空正文"):
        require_check_text(content)


def test_nonempty_response_is_required_but_not_exact_case_sensitive_ok():
    assert require_check_text(" OK\n") == "OK"
    assert require_check_text("ok") == "ok"


def test_real_client_timeout_and_equivalent_connections():
    async def run():
        clients = [OpenAILLMClient(api_key=key, base_url=base, model=model, timeout=timeout)
                   for key, base, model, timeout in [
                       ("local-test-key-a", "http://127.0.0.1:1/v1", "model-a", 180),
                       ("local-test-key-a", "http://127.0.0.1:1/v1", "model-a", 180),
                       ("local-test-key-b", "http://127.0.0.1:1/v1", "model-a", 180),
                       ("local-test-key-a", "http://127.0.0.1:2/v1", "model-a", 180),
                       ("local-test-key-a", "http://127.0.0.1:1/v1", "model-b", 180),
                       ("local-test-key-a", "http://127.0.0.1:1/v1", "model-a", 120),
                   ]]
        try:
            assert check_identity(clients[0]) == check_identity(clients[1])
            assert check_identity(EngineClientBundle.from_client(clients[0])) == check_identity(clients[0])
            for client in clients[2:]:
                assert check_identity(client) != check_identity(clients[0])
            assert "local-test-key-a" not in str(check_identity(clients[0]))
            assert client_check_timeout(clients[0]) == 180
            assert client_check_timeout(clients[0], 60) == 60
            assert client_check_timeout(clients[-1], 180) == 120
        finally:
            for client in clients:
                await client._client.close()
    asyncio.run(run())


@contextmanager
def allow_local_test_host():
    key = "LLM_PRIVATE_HOST_ALLOWLIST"
    old = os.environ.get(key)
    os.environ[key] = "127.0.0.1"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def test_real_unreachable_socket_preserves_model_and_provider_diagnostics():
    # Bound but not listening: reserve the port so no unrelated service receives data.
    with socket.socket() as reserved, allow_local_test_host():
        reserved.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{reserved.getsockname()[1]}/v1"
        service = LLMService()
        model = service._test_openai_compatible_config(base, "local-test-key", "test", "test", time.time(), 1)
        provider = service._test_openai_compatible_api("local-test-key", "test", base, "custom_test", "test", 1)
        for result in [model, provider]:
            assert not result["success"]
            # macOS can silently drop a SYN to a bound non-listening socket;
            # Linux/Windows may refuse immediately. Both are real I/O failures.
            assert result["details"]["kind"] in {"connection", "connection_timeout"}
            assert "local-test-key" not in result["message"]
            assert base not in result["message"]


def test_real_silent_tcp_connection_reports_response_timeout():
    stopped = threading.Event()
    with socket.socket() as listener, allow_local_test_host():
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        base = f"http://127.0.0.1:{listener.getsockname()[1]}/v1"

        def hold_connection():
            connection, _ = listener.accept()
            with connection:
                stopped.wait(3)

        worker = threading.Thread(target=hold_connection)
        worker.start()
        try:
            result = LLMService()._test_openai_compatible_config(
                base, "local-test-key", "test", "test", time.time(), 0.15,
            )
            assert not result["success"]
            assert result["details"]["kind"] == "response_timeout"
            assert "不等于网关未启动" in result["message"]
        finally:
            stopped.set()
            worker.join(timeout=4)
        assert not worker.is_alive()


def test_runtime_rejects_real_unreachable_gateway_without_starting_pipeline(tmp_path):
    from app.engine.runtime import AnalysisRuntime

    async def run():
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            base = f"http://127.0.0.1:{reserved.getsockname()[1]}/v1"
            client = OpenAILLMClient(api_key="local-test-key", base_url=base, model="test", timeout=1)
            runtime = AnalysisRuntime(selected_analysts=["market"], config={
                "memory_enabled": False, "data_cache_dir": str(tmp_path),
            })
            try:
                with pytest.raises(RuntimeError, match="模型连接检查未通过") as error:
                    await runtime._preflight_clients({"analyst": client, "debate": client})
                assert "local-test-key" not in str(error.value)
                assert "无法连接" in str(error.value) or "超时" in str(error.value)
                assert runtime.curr_state is None
            finally:
                await client._client.close()
    asyncio.run(run())
