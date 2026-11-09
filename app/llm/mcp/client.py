"""
MCP 连接管理（参考 claude-code mcp-client 生命周期策略，官方 mcp SDK）

- 连接按 (name, config) 缓存；配置变化则重建
- 连接超时 30s；stdio 优雅关闭
- 会话失效（进程退出等）时清缓存，下次调用懒重连
- 本地 server 并发连接数 3
"""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Dict, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import logging

from .config import MCPServerConfig
from .management.config_store import resolve_command

logger = logging.getLogger("app.llm.mcp")

CONNECT_TIMEOUT = 30.0  # 秒，参考 claude-code
LOCAL_CONNECT_CONCURRENCY = 3
CLOSE_TIMEOUT = 10.0


@dataclass
class _Connection:
    cfg: MCPServerConfig
    ready: asyncio.Future[ClientSession]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    session: Optional[ClientSession] = None
    task: Optional[asyncio.Task] = None


class MCPManager:
    """管理全部 MCP server 连接的生命周期"""

    def __init__(self):
        self._connections: Dict[str, _Connection] = {}
        self._connecting: Dict[str, asyncio.Lock] = {}
        self._local_semaphore = asyncio.Semaphore(LOCAL_CONNECT_CONCURRENCY)
        self._closed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def connect(self, cfg: MCPServerConfig) -> ClientSession:
        """建立（或复用）到指定 server 的会话；缓存命中直接返回"""
        return await self._get_connection(cfg)

    async def _get_connection(
        self, cfg: MCPServerConfig, failed_session: Optional[ClientSession] = None
    ) -> ClientSession:
        if self._closed:
            raise RuntimeError("MCPManager 已关闭")
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("MCPManager 必须在创建连接的事件循环上使用")
        lock = self._connecting.setdefault(cfg.name, asyncio.Lock())
        async with lock:
            if self._closed:
                raise RuntimeError("MCPManager 已关闭")
            current = self._connections.get(cfg.name)
            if (current and current.cfg.cache_key == cfg.cache_key
                    and current.task is not None and not current.task.done()
                    and (failed_session is None or current.session is not failed_session)):
                return await asyncio.shield(current.ready)
            if current is not None:
                await self._stop_connection(current)
            connection = _Connection(cfg, loop.create_future())
            # If a request is canceled during startup, the manager still owns
            # this connection; consume a later startup exception without making
            # the requesting task own an SDK cancel scope.
            connection.ready.add_done_callback(
                lambda future: None if future.cancelled() else future.exception()
            )
            self._connections[cfg.name] = connection
            connection.task = asyncio.create_task(
                self._run_connection(connection), name=f"mcp-connection:{cfg.name}"
            )
            return await asyncio.shield(connection.ready)

    async def _run_connection(self, connection: _Connection) -> None:
        """Enter, use and exit SDK task groups in one dedicated owner task.

        SDK transport/dispatcher failures can cancel their owner. That owner
        must never be FastAPI lifespan, a request or another MCP connection.
        """
        cfg = connection.cfg
        failure: Optional[Exception] = None
        try:
            async with AsyncExitStack() as stack:
                async with self._local_semaphore:
                    async with asyncio.timeout(CONNECT_TIMEOUT):
                        if cfg.type in ("http", "streamable-http"):
                            session = await self._connect_http(stack, cfg)
                        elif cfg.type == "sse":
                            session = await self._connect_sse(stack, cfg)
                        else:
                            session = await self._connect_stdio(stack, cfg)
                if self._closed or connection.stop.is_set():
                    raise RuntimeError("MCPManager 已关闭")
                connection.session = session
                connection.ready.set_result(session)
                logger.info("🔗 [mcp] 已连接 %s (%s)", cfg.name, cfg.type)
                await connection.stop.wait()
        except asyncio.CancelledError:
            failure = ConnectionError(f"MCP server '{cfg.name}' 连接任务已结束")
            if not self._closed and not connection.stop.is_set():
                logger.warning("[mcp] server '%s' 连接中断；仅隔离该连接", cfg.name)
        except Exception as exc:
            failure = (TimeoutError(f"MCP server '{cfg.name}' 连接超时({CONNECT_TIMEOUT}s)")
                       if isinstance(exc, TimeoutError) else
                       ConnectionError(f"MCP server '{cfg.name}' 连接失败 ({type(exc).__name__})"))
            logger.warning("[mcp] server '%s' 连接退出: %s", cfg.name, type(exc).__name__)
        finally:
            if not connection.ready.done():
                connection.ready.set_exception(failure or ConnectionError("MCP 连接已关闭"))
            if self._connections.get(cfg.name) is connection:
                self._connections.pop(cfg.name, None)

    async def _stop_connection(self, connection: _Connection) -> None:
        connection.stop.set()
        if connection.task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(connection.task), timeout=CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            connection.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(connection.task), timeout=CLOSE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("[mcp] server '%s' 关闭超时；已请求连接任务退出", connection.cfg.name)

    async def _connect_stdio(self, stack: AsyncExitStack, cfg: MCPServerConfig) -> ClientSession:
        # 连接前解析命令（Windows 下 uvx/npx 包装器需 which 解析/回退），
        # 不可用时给出安装引导而不是让 subprocess 抛模糊错误
        resolved, cmd_err = resolve_command(cfg.command or "")
        if cmd_err:
            raise RuntimeError(f"MCP server '{cfg.name}' 命令不可用: {cmd_err}")
        params = StdioServerParameters(command=resolved, args=cfg.args, env=cfg.env or None)
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        return session

    async def _connect_http(self, stack: AsyncExitStack, cfg: MCPServerConfig) -> ClientSession:
        # MCP 2.1 把 ``streamablehttp_client`` 改名为
        # ``streamable_http_client``，并要求通过独立 http_client 传 headers。
        # 同时兼容 2.0 与 2.1，避免 HTTP MCP 在升级 SDK 后完全不可发现。
        from mcp.client import streamable_http as streamable_http_module

        modern_client = getattr(streamable_http_module, "streamable_http_client", None)
        if modern_client is not None:
            create_http_client = getattr(streamable_http_module, "create_mcp_http_client", None)
            if create_http_client is None:
                raise RuntimeError("当前 MCP SDK 缺少 create_mcp_http_client")
            http_client = await stack.enter_async_context(
                create_http_client(headers=cfg.headers or None)
            )
            transport_context = modern_client(cfg.url, http_client=http_client)
        else:
            legacy_client = getattr(streamable_http_module, "streamablehttp_client", None)
            if legacy_client is None:
                raise RuntimeError("当前 MCP SDK 不支持 streamable HTTP client")
            transport_context = legacy_client(cfg.url, headers=cfg.headers or None)

        transport = await stack.enter_async_context(transport_context)
        # 2.0 返回三元组（含 session id callback），2.1 返回二元组。
        read_stream, write_stream, *_ = transport
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        return session

    async def _connect_sse(self, stack: AsyncExitStack, cfg: MCPServerConfig) -> ClientSession:
        from mcp.client.sse import sse_client

        try:
            read_stream, write_stream = await stack.enter_async_context(
                sse_client(cfg.url, headers=cfg.headers or None)
            )
        except Exception as e:
            raise RuntimeError(
                f"MCP server '{cfg.name}' SSE 连接失败: {e}（旧版 SSE 传输，建议升级为 streamable-http）"
            ) from e
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        return session

    async def get_session(self, cfg: MCPServerConfig) -> ClientSession:
        """获取会话；会话已死时清缓存并重连一次（懒重连，参考 claude-code）"""
        session = await self.connect(cfg)
        try:
            # 廉价探活：list_tools 空参数由 server 自行处理，此处用 ping
            await asyncio.wait_for(session.send_ping(), timeout=10.0)
        except Exception as e:
            logger.warning(f"⚠️ [mcp] server '{cfg.name}' 会话失效({e})，尝试重连")
            # Another concurrent reader may already have replaced this exact
            # failed session; do not tear down its healthy replacement.
            session = await self._get_connection(cfg, failed_session=session)
        return session

    async def close_all(self) -> None:
        """关闭全部连接（进程退出前调用）"""
        self._closed = True
        connections = list(self._connections.values())
        self._connections.clear()
        for connection in connections:
            connection.stop.set()
        await asyncio.gather(*(self._stop_connection(c) for c in connections))
        logger.info("[mcp] 全部连接已关闭")
