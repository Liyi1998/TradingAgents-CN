"""Real stdio/HTTP SDK I/O; only kill processes created by these tests."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from app.llm.mcp.client import MCPManager
from app.llm.mcp.config import MCPServerConfig

SERVER = Path(__file__).with_name('mcp_lifetime_server.py')


def stdio_config(name='owned'):
    return MCPServerConfig(name=name, command=sys.executable, args=[str(SERVER)])


async def server_pid(session):
    result = await session.call_tool('pid', arguments={})
    return int(result.content[0].text)


class LocalHTTPServer:
    def __init__(self, port):
        self.port = port
        self.process = None
        self.config = MCPServerConfig(name='local-http', type='streamable-http', url=f'http://127.0.0.1:{port}/mcp')

    async def start(self):
        self.process = subprocess.Popen([sys.executable, str(SERVER), '--http-port', str(self.port)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            assert self.process.poll() is None, 'Real local MCP server exited before listening'
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.1):
                    return
            except OSError:
                await asyncio.sleep(.05)
        raise AssertionError('Local MCP server startup deadline')

    async def kill(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            await asyncio.to_thread(self.process.wait, 5)


@pytest_asyncio.fixture
async def local_http_server():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = LocalHTTPServer(port)
    await server.start()
    try:
        yield server
    finally:
        await server.kill()


@pytest.mark.asyncio
async def test_close_connection_from_different_task():
    manager = MCPManager()
    session = await manager.connect(stdio_config())
    assert await server_pid(session) > 0
    await asyncio.create_task(manager.close_all())
    await asyncio.sleep(0)
    assert asyncio.current_task().cancelling() == 0
    with pytest.raises(RuntimeError, match='已关闭'):
        await manager.connect(stdio_config())


@pytest.mark.asyncio
async def test_http_disconnect_does_not_cancel_application(local_http_server):
    manager = MCPManager()
    try:
        session = await manager.connect(local_http_server.config)
        assert await server_pid(session) == local_http_server.process.pid
        await local_http_server.kill()
        # A real socket refusal in SDK post_writer can cancel its AnyIO owner.
        # That owner must not be this application/request task.
        with pytest.raises(Exception):
            await asyncio.wait_for(session.send_ping(), timeout=5)
        await asyncio.sleep(.1)
        assert asyncio.current_task().cancelling() == 0
        await local_http_server.start()
        restored = await manager.get_session(local_http_server.config)
        assert restored is not session
        assert await server_pid(restored) == local_http_server.process.pid
        result = await restored.call_tool('add', arguments={'a': 17, 'b': 23})
        assert result.content[0].text == '40.0'
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_stdio_peer_survives_another_server_death():
    manager = MCPManager()
    try:
        failed = await manager.connect(stdio_config('failed'))
        peer = await manager.connect(stdio_config('peer'))
        dead_pid = await server_pid(failed)
        peer_pid = await server_pid(peer)
        os.kill(dead_pid, signal.SIGKILL)
        await asyncio.sleep(.15)
        assert await server_pid(await manager.get_session(stdio_config('peer'))) == peer_pid
        replacement = await manager.get_session(stdio_config('failed'))
        assert await server_pid(replacement) != dead_pid
        assert await manager.get_session(stdio_config('peer')) is peer
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_cancel_connect_request_does_not_cancel_connection_owner():
    manager = MCPManager()
    try:
        request = asyncio.create_task(manager.connect(stdio_config()))
        await asyncio.sleep(.02)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        session = await asyncio.wait_for(manager.get_session(stdio_config()), timeout=10)
        assert await server_pid(session) > 0
        assert asyncio.current_task().cancelling() == 0
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_failed_initialization_is_isolated_and_manager_remains_usable():
    manager = MCPManager()
    try:
        bad = MCPServerConfig(name='bad', command='definitely-not-a-real-mcp-command-xyz')
        with pytest.raises(ConnectionError):
            await manager.connect(bad)
        session = await manager.get_session(stdio_config('good'))
        assert await server_pid(session) > 0
        assert asyncio.current_task().cancelling() == 0
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_configuration_change_closes_only_previous_owner():
    manager = MCPManager()
    try:
        first = await manager.get_session(stdio_config('changed'))
        first_pid = await server_pid(first)
        changed = MCPServerConfig(name='changed', command=sys.executable, args=['-u', str(SERVER)])
        second = await manager.get_session(changed)
        assert second is not first and await server_pid(second) != first_pid
        with pytest.raises(ProcessLookupError):
            os.kill(first_pid, 0)
    finally:
        await manager.close_all()
