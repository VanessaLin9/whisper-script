"""Optional MCP 2.x stdio adapter for Meeting Desk."""
from __future__ import annotations

import json
import sys

from src.desktop.service import DesktopService, dispatch
from .registry import action_tool_specs, validate_arguments


def create_server():
    """Create a low-level MCP server; importing this function needs mcp."""
    try:
        import mcp_types as types
        from mcp.server.lowlevel import Server
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Meeting Desk MCP server 需要選用的 mcp SDK；請在虛擬環境執行 "
            "python3 -m pip install -r requirements-mcp.txt。"
        ) from exc

    specs = action_tool_specs()
    tools = [types.Tool(
        name=spec["name"], description=spec["description"],
        inputSchema=spec["inputSchema"],
        annotations=types.ToolAnnotations(**spec["annotations"]),
    ) for spec in specs]
    by_name = {spec["name"]: spec for spec in specs}

    async def list_tools(_ctx, _params):
        return types.ListToolsResult(tools=tools)

    async def call_tool(_ctx, params):
        try:
            spec = by_name.get(params.name)
            if spec is None:
                raise ValueError(f"不允許的 MCP tool：{params.name}。")
            arguments = params.arguments
            if isinstance(arguments, dict) and "action" in arguments:
                raise ValueError("arguments 不得包含 action；工具名稱會決定 action。")
            validate_arguments(spec["inputSchema"], arguments)
            # Construct per call so Desk setting changes are observed immediately.
            result = dispatch(DesktopService(), {"action": params.name, **arguments})
        except Exception as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))], isError=True,
            )
        encoded = json.dumps(result, ensure_ascii=False)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=encoded)],
            structuredContent=result,
        )

    # The low-level server's list handler is authoritative; the closure keeps
    # the explicit allowlist and registry snapshot together.
    return Server(
        "meeting-desk", version="1", on_list_tools=list_tools,
        on_call_tool=call_tool,
        get_tool_input_schema=lambda name: by_name.get(name, {}).get("inputSchema"),
    )


def main() -> int:
    try:
        from mcp.server.stdio import stdio_server
    except ModuleNotFoundError:
        print(
            "Meeting Desk MCP server 需要 mcp SDK；請先執行 "
            "python3 -m pip install -r requirements-mcp.txt（建議使用 venv）。",
            file=sys.stderr, flush=True,
        )
        return 1

    import asyncio

    async def run():
        server = create_server()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())
    return 0
