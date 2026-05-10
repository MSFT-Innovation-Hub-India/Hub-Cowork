"""
MCPClientPool — host-scoped, lazy stdio MCP client pool.

Per SKILLS_DESIGN_PRINCIPLES.md §11.4a / §11.7 / §14.1:

  * Tools are MCP servers. Today they run as stdio subprocesses; tomorrow
    they may move to ACA over streamable HTTP. The agent loop shouldn't care.
  * One subprocess per server NAME. Calls from every conversation thread
    are multiplexed onto that one warm subprocess. We never spawn a new
    process per tool call.
  * stdio MCP servers cannot be passed to the Azure OpenAI Responses API
    as native `mcp` tools (that mode only accepts remote HTTPS URLs). We
    expose them as ordinary `type="function"` tools synthesized from each
    server's `tools/list` and dispatch `requires_action` calls client-side
    via this pool.
  * Auth crosses the boundary via the shared on-disk MSAL cache. Servers
    call `get_credential()` themselves — no tokens marshaled over MCP.
  * Progress: MCP `notifications/progress` from the server is forwarded to
    the per-call `on_progress(kind, message)` callback so the existing UI
    progress stream stays unchanged.

Implementation notes:
  * The MCP Python SDK is asyncio-only. agent_core is sync. We bridge by
    running ONE persistent asyncio event loop on a dedicated daemon thread
    and submitting coroutines to it via `asyncio.run_coroutine_threadsafe`.
  * Each server has a long-lived OWNER TASK on that loop. The owner enters
    `stdio_client(...)` and `ClientSession(...)` and holds them open for
    the host's lifetime. Calls and shutdown are routed to the owner via
    an `asyncio.Queue` so context-manager enter/exit always happen on the
    SAME task (anyio task-group rule).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("hub_se_agent")

ProgressCb = Callable[[str, str], None] | None


@dataclass
class _Request:
    """A unit of work submitted to a server's owner task."""
    kind: str  # "call" | "shutdown"
    name: str = ""
    arguments: dict = field(default_factory=dict)
    on_progress: ProgressCb = None
    future: asyncio.Future | None = None


@dataclass
class _ServerHandle:
    name: str
    params: StdioServerParameters
    queue: asyncio.Queue | None = None
    owner_task: asyncio.Task | None = None
    ready: asyncio.Event | None = None
    start_error: BaseException | None = None
    tools: list[dict] = field(default_factory=list)


class MCPClientPool:
    """Host-scoped pool: one stdio subprocess per server name, multiplexed.

    Lifetime is the host process. Construct once at startup, share with all
    threads. Call `shutdown()` on graceful exit.
    """

    def __init__(self) -> None:
        self._servers: dict[str, _ServerHandle] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._registry_lock = threading.Lock()
        self._start_loop()

    # -- event loop plumbing ------------------------------------------------

    def _start_loop(self) -> None:
        ready = threading.Event()

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run, name="mcp-pool-loop", daemon=True
        )
        self._loop_thread.start()
        ready.wait(timeout=5.0)
        if self._loop is None:
            raise RuntimeError("MCPClientPool event loop failed to start")

    def _submit(self, coro) -> Any:
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    # -- registration -------------------------------------------------------

    def register(self, name: str, params: StdioServerParameters) -> None:
        """Declare a server. Subprocess is NOT spawned here — first use
        triggers it. Idempotent."""
        with self._registry_lock:
            if name in self._servers:
                return
            self._servers[name] = _ServerHandle(name=name, params=params)
            logger.info("[mcp-pool] Registered server '%s' (cmd=%s %s)",
                        name, params.command, " ".join(params.args or []))

    def is_registered(self, name: str) -> bool:
        with self._registry_lock:
            return name in self._servers

    # -- public API ---------------------------------------------------------

    def list_tools(self, server: str) -> list[dict]:
        """Return Responses-API function-style schemas for every tool the
        server advertises. Lazily starts the server on first call."""
        h = self._get_handle(server)
        self._submit(self._ensure_started(h))
        return list(h.tools)

    def call(
        self,
        server: str,
        name: str,
        arguments: dict,
        on_progress: ProgressCb = None,
    ) -> str:
        h = self._get_handle(server)
        try:
            return self._submit(self._do_call(h, name, arguments, on_progress))
        except Exception as e:
            logger.exception("[mcp-pool] call %s.%s failed", server, name)
            return json.dumps({"status": "error", "tool": name,
                               "server": server, "error": str(e)})

    def shutdown(self) -> None:
        """Stop all server subprocesses and the event loop. Best-effort.
        Idempotent — safe to call from both explicit cleanup and atexit."""
        if self._loop is None:
            return
        loop = self._loop
        self._loop = None  # mark down so reentrant calls bail immediately
        try:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown_all(), loop)
            fut.result(timeout=10.0)
        except Exception:
            logger.exception("[mcp-pool] shutdown failed")
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass

    # -- internals ----------------------------------------------------------

    def _get_handle(self, server: str) -> _ServerHandle:
        with self._registry_lock:
            h = self._servers.get(server)
        if h is None:
            raise KeyError(f"MCP server '{server}' not registered")
        return h

    async def _ensure_started(self, h: _ServerHandle) -> None:
        if h.owner_task is not None:
            assert h.ready is not None
            await h.ready.wait()
            if h.start_error is not None:
                raise h.start_error
            return
        h.queue = asyncio.Queue()
        h.ready = asyncio.Event()
        h.owner_task = asyncio.create_task(
            self._owner(h), name=f"mcp-owner-{h.name}"
        )
        await h.ready.wait()
        if h.start_error is not None:
            raise h.start_error

    async def _owner(self, h: _ServerHandle) -> None:
        """Long-running task: owns the subprocess context managers for this
        server's lifetime. Reads requests off `h.queue` and serves them on
        the SAME task that entered the contexts (anyio task-group rule)."""
        assert h.queue is not None and h.ready is not None
        try:
            async with stdio_client(h.params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tool_list = await session.list_tools()
                    h.tools = [_to_function_schema(t) for t in tool_list.tools]
                    logger.info(
                        "[mcp-pool] Started server '%s' — %d tool(s): %s",
                        h.name, len(h.tools), [t["name"] for t in h.tools]
                    )
                    h.ready.set()
                    while True:
                        req: _Request = await h.queue.get()
                        if req.kind == "shutdown":
                            if req.future is not None and not req.future.done():
                                req.future.set_result(None)
                            return
                        try:
                            result = await self._invoke(
                                session, req.name, req.arguments, req.on_progress
                            )
                            if req.future is not None and not req.future.done():
                                req.future.set_result(result)
                        except BaseException as e:
                            if req.future is not None and not req.future.done():
                                req.future.set_exception(e)
        except BaseException as e:
            h.start_error = e
            if not h.ready.is_set():
                h.ready.set()
            logger.exception("[mcp-pool] owner task for '%s' crashed", h.name)
            if h.queue is not None:
                while not h.queue.empty():
                    try:
                        pending: _Request = h.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if pending.future is not None and not pending.future.done():
                        pending.future.set_exception(e)

    async def _invoke(
        self,
        session: ClientSession,
        name: str,
        arguments: dict,
        on_progress: ProgressCb,
    ) -> str:
        async def _progress(progress: float, total: float | None,
                             message: str | None):
            if not (on_progress and message):
                return
            # The mcp_servers runtime encodes the kind as a `[kind] ` prefix
            # so we can recover it here and forward the original kind to the
            # UI. Plain messages (no prefix) are treated as transient "step"
            # updates per the legacy contract.
            kind = "step"
            text = message
            if message.startswith("[") and "] " in message:
                end = message.index("] ")
                candidate = message[1:end]
                if candidate and " " not in candidate:
                    kind = candidate
                    text = message[end + 2:]
            try:
                on_progress(kind, text)
            except Exception:
                logger.debug("[mcp-pool] on_progress raised", exc_info=True)

        result = await session.call_tool(
            name, arguments=arguments, progress_callback=_progress
        )
        parts: list[str] = []
        for block in (result.content or []):
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        text_out = "\n".join(parts) if parts else ""
        if getattr(result, "isError", False):
            return text_out or json.dumps(
                {"status": "error", "tool": name,
                 "error": "tool reported error with no message"}
            )
        return text_out

    async def _do_call(
        self,
        h: _ServerHandle,
        name: str,
        arguments: dict,
        on_progress: ProgressCb,
    ) -> str:
        await self._ensure_started(h)
        assert h.queue is not None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await h.queue.put(_Request(
            kind="call", name=name, arguments=arguments,
            on_progress=on_progress, future=fut,
        ))
        return await fut

    async def _shutdown_all(self) -> None:
        with self._registry_lock:
            handles = list(self._servers.values())
        loop = asyncio.get_running_loop()
        for h in handles:
            if h.owner_task is None or h.queue is None:
                continue
            fut: asyncio.Future = loop.create_future()
            await h.queue.put(_Request(kind="shutdown", future=fut))
            try:
                await asyncio.wait_for(fut, timeout=2.0)
            except Exception:
                pass
            try:
                await asyncio.wait_for(h.owner_task, timeout=3.0)
            except Exception:
                pass


def _to_function_schema(tool: Any) -> dict:
    """Convert an MCP `Tool` advertisement to a Responses-API function tool.

    Responses-API function tool shape:
        {"type": "function", "name": ..., "description": ...,
         "parameters": <JSON Schema>}
    """
    return {
        "type": "function",
        "name": tool.name,
        "description": (tool.description or "").strip(),
        "parameters": tool.inputSchema or {"type": "object", "properties": {}},
    }


def python_module_server(module: str) -> StdioServerParameters:
    """Build StdioServerParameters that run `python -m <module>` in this
    interpreter, inheriting the environment (so PYTHONPATH, the editable
    install, .env loading, and the on-disk MSAL token cache all carry over).
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", module],
        env=os.environ.copy(),
    )
