"""Real local SDK server for connection-death/ownership integration tests."""

import argparse
import os
from contextlib import asynccontextmanager

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=[
        types.Tool(name="pid", description="Owned test server process", inputSchema={"type": "object"}),
        types.Tool(name="add", description="Add two numbers", inputSchema={
            "type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        }),
    ])


async def on_call_tool(ctx, params):
    args = params.arguments or {}
    text = str(os.getpid()) if params.name == "pid" else str(float(args["a"]) + float(args["b"]))
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


server = Server("lifetime", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def run_stdio():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_http(port):
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Route

    manager = StreamableHTTPSessionManager(server, json_response=True)

    class MCPTransport:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    app = Starlette(routes=[Route('/mcp', MCPTransport(), methods=['GET', 'POST', 'DELETE'])], lifespan=lifespan)
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='error')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--http-port', type=int)
    args = parser.parse_args()
    if args.http_port:
        run_http(args.http_port)
    else:
        anyio.run(run_stdio)
