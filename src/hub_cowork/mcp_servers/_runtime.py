"""
Shared runtime for hub_cowork's MCP servers.

Each MCP server module under `hub_cowork.mcp_servers.<name>` (or
`hub_cowork.skills.<skill>.mcp_server`) is just a thin manifest that
declares which existing tool modules it owns, then calls `serve(...)`.

This keeps the MCP migration low-risk: tool *behavior* is unchanged
(we re-use the existing `handle()` functions and their JSON schemas);
only the *transport* changes (in-process call → stdio MCP).

Why we use the lowlevel `mcp.server.Server` rather than `FastMCP`:
FastMCP derives the JSON Schema from the Python function signature
via Pydantic, which would silently flatten our existing rich
parameter schemas (descriptions, enums, nested objects). The lowlevel
server lets us advertise the EXACT same `SCHEMA["parameters"]` dict
the legacy in-process loader used, so the model's tool-calling
contract is preserved bit-for-bit.

Progress callbacks (`on_progress(kind, message)`) are translated to
MCP `notifications/progress`, which the host's `MCPClientPool`
forwards back to whatever `on_progress` the agent loop passed in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from importlib import import_module
from typing import Any

import mcp.types as mtypes
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def _build_on_progress(loop: asyncio.AbstractEventLoop, session, progress_token):
    """Build a thread-safe `on_progress(kind, message)` for one tool call.

    The handler runs in `loop.run_in_executor` (a worker thread), where
    the MCP server's `request_context` ContextVar is NOT set and where
    `loop.create_task(...)` would be unsafe. We capture the loop, session,
    and progress token while we're still on the event-loop thread, then
    schedule sends from the worker thread via `run_coroutine_threadsafe`.

    Kind is encoded as a `[kind] ` prefix on the MCP progress message; the
    host's MCPClientPool parses it back into the original kind. Plain "step"
    is sent without the prefix because that's the legacy default the pool
    falls back to.
    """
    if session is None or progress_token is None:
        def _noop(_kind: str, _message: str) -> None:
            return
        return _noop

    def on_progress(kind: str, message: str) -> None:
        if not message:
            return
        text = (
            f"[{kind}] {message}"
            if kind and kind != "step"
            else message
        )
        coro = session.send_progress_notification(
            progress_token=progress_token,
            progress=0.0,
            total=None,
            message=text,
        )
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            # Loop already shutting down — drop silently.
            return
    return on_progress


def _resolve_workiq_cli() -> str | None:
    """Replicates agent_core._find_workiq for tools that need the CLI path.
    Lives here so MCP server subprocesses don't need to import agent_core
    (which would re-bootstrap the OpenAI client and skill loader)."""
    import os, shutil
    from pathlib import Path
    venv_dir = Path(sys.executable).parent
    for name in ("workiq", "workiq.exe"):
        c = venv_dir / name
        if c.exists():
            return str(c)
    env_path = os.environ.get("WORKIQ_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    return shutil.which("workiq")


def serve(server_name: str, tool_modules: list[str]) -> None:
    """Build and run an MCP server that exposes one tool per module.

    Each `tool_modules` entry is a dotted import path. The module must
    export `SCHEMA: dict` (with `name`, `description`, `parameters`) and
    `handle(arguments: dict, *, on_progress=None, **kwargs) -> str`.
    """
    logger = logging.getLogger(f"mcp.{server_name}")
    server = Server(server_name)

    # name -> (handler, module_path)
    tools: dict[str, tuple[Any, str]] = {}
    advertised: list[mtypes.Tool] = []

    for module_path in tool_modules:
        try:
            mod = import_module(module_path)
        except Exception as e:
            logger.error("Failed to import %s: %s", module_path, e)
            continue
        schema = getattr(mod, "SCHEMA", None)
        handler = getattr(mod, "handle", None)
        if schema is None or handler is None:
            logger.warning("%s missing SCHEMA or handle — skipping", module_path)
            continue
        name = schema["name"]
        if name in tools:
            logger.error("Tool name collision: %s (skipping %s)", name, module_path)
            continue
        tools[name] = (handler, module_path)
        advertised.append(mtypes.Tool(
            name=name,
            description=schema.get("description", "").strip(),
            inputSchema=schema.get("parameters")
                or {"type": "object", "properties": {}},
        ))
        logger.info("[%s] Loaded tool: %s (%s)", server_name, name, module_path)

    on_progress_outside = lambda *_a, **_kw: None  # used only outside a request
    workiq_cli = _resolve_workiq_cli()

    @server.list_tools()
    async def _list_tools() -> list[mtypes.Tool]:
        return advertised

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list[mtypes.TextContent]:
        entry = tools.get(name)
        if entry is None:
            payload = json.dumps({"status": "error", "error": f"unknown tool '{name}'"})
            return [mtypes.TextContent(type="text", text=payload)]
        handler, _ = entry

        # Capture the live loop, session, and progress token NOW, on the
        # event-loop thread where `request_context` is valid. The handler
        # runs in a worker thread and cannot read these via ContextVars.
        loop = asyncio.get_running_loop()
        session = None
        progress_token = None
        try:
            ctx = server.request_context
            session = getattr(ctx, "session", None)
            meta = getattr(ctx, "meta", None)
            progress_token = getattr(meta, "progressToken", None) if meta is not None else None
        except LookupError:
            pass
        on_progress = _build_on_progress(loop, session, progress_token)

        try:
            # Tool handlers are sync. Run in the default executor so we don't
            # block the server's event loop on long calls (subprocess, HTTP).
            result = await loop.run_in_executor(
                None,
                lambda: handler(
                    arguments or {},
                    on_progress=on_progress,
                    workiq_cli=workiq_cli,
                ),
            )
        except Exception as e:
            logger.exception("Tool %s raised", name)
            payload = json.dumps({"status": "error", "tool": name, "error": str(e)})
            return [mtypes.TextContent(type="text", text=payload)]
        if not isinstance(result, str):
            try:
                result = json.dumps(result)
            except Exception:
                result = str(result)
        return [mtypes.TextContent(type="text", text=result)]

    async def _run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_run())
