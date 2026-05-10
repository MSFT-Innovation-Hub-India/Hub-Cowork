# Runtime internals

Reference material for what actually runs on the laptop when Hub Cowork is up — process tree, thread populations, MCP wire mechanics, lifecycles, and rough memory footprint. The high-level overview is in the [README](../../README.md); this document is the deeper companion.

- [How MCP is wired](#how-mcp-is-wired)
- [Process and thread map](#process-and-thread-map)
- [Lifecycles at a glance](#lifecycles-at-a-glance)
- [Memory footprint, ballpark](#memory-footprint-ballpark)

---

## How MCP is wired

Tools are not loose Python modules with a `SCHEMA` dict and a `handle()` function. They are **MCP servers** — the same Model Context Protocol used by Claude Cowork. Each server is a subprocess; each tool is a `@mcp.tool`-style function inside one of those subprocesses.

This buys us three things at once:

1. **Process isolation.** A buggy tool can crash its server without taking the agent down.
2. **A clean future migration to Azure Container Apps.** When a server moves to ACA, `skill.yaml` flips one line (stdio → streamable HTTP); skill code, tool code, and the agent loop are unchanged.
3. **Multi-language tools (eventually).** Any language that speaks MCP can host a Hub Cowork tool — today they're all Python because that's what we have.

### …but watch out for the subtlety

You will look at `src/hub_cowork/mcp_servers/` and see Python files that look like ordinary modules. Where is the protocol?

The protocol code lives in [`mcp_servers/_runtime.py`](../../src/hub_cowork/mcp_servers/_runtime.py). It imports `mcp.types`, `mcp.server.lowlevel.Server`, and `mcp.server.stdio.stdio_server` from the official `mcp` Python package and exposes a `serve(...)` helper. Every other folder under `mcp_servers/` (`workiq/`, `m365/`, `utility/`) and every per-skill `mcp_server/` folder is a thin manifest that lists its tools and calls `serve(...)`. That's the wire-level MCP server.

We use the **lowlevel** `mcp.server.lowlevel.Server` rather than `FastMCP` on purpose: FastMCP derives the JSON Schema from Python type hints via Pydantic, which would silently flatten our existing rich parameter schemas (descriptions, enums, nested objects). The lowlevel server lets us advertise the EXACT same `SCHEMA["parameters"]` dict the model has been working with — bit-for-bit contract preservation.

### The most important runtime fact about MCP here

**Stdio MCP servers are NOT registered with the Azure OpenAI Responses API as native `mcp` tools.** The Responses API's native `mcp` tool type only accepts **remote** servers reachable by URL (streamable HTTP / SSE). A local stdio subprocess on the user's machine is unreachable from Azure cloud, full stop.

So the wiring is:

1. **At skill-start**, `agent_core` calls MCP `tools/list` on every server the skill connects to. The pool collects every tool's name, description, and input schema.
2. **Each MCP tool is registered with the Responses API as an ordinary function tool** (`type: "function"`) in the `tools=[...]` payload of `responses.create(...)`. Description and parameters come straight from the MCP advertisement.
3. **When the model returns `requires_action`**, the agent loop looks up which server owns the requested tool, calls `pool.call(server, tool_name, args)`, awaits the JSON result, and submits it as a tool output. The Responses API never knows MCP exists on the wire — from its perspective every call is a function call.
4. **MCP `notifications/progress`** received from the server during the call are forwarded to `on_progress(...)` in real time; they do not flow through the Responses API.

When a server eventually moves to ACA-hosted streamable HTTP, we'll have a per-server choice: keep it client-dispatched (the pool grows an HTTP branch), or hand it to the Responses API as a native `{type: "mcp", server_url: ...}`. Until then, the pool is the only path tool calls take.

### The pool is host-scoped, not thread-scoped

`MCPClientPool` lives in `agent_core` and is shared by every conversation thread. **One subprocess per server name, multiplexed across all calls.** A long-running RFP brief in thread A and a fast Q&A in thread B share the same warm `workiq` server subprocess concurrently. There is never one-subprocess-per-call.

### Auth crosses the boundary via the on-disk MSAL cache, not via the wire

MCP server subprocesses do not receive credentials over the MCP transport. They call `get_credential()` from [`core/auth_credential.py`](../../src/hub_cowork/core/auth_credential.py) themselves and silently mint scope-specific tokens from the same `~/.hub-cowork/`-rooted MSAL cache the host wrote at sign-in. **Never construct credentials in a tool. Never accept tokens as arguments.**

### Inspecting the wire

You can run any one server module standalone and speak MCP at it:

```powershell
python -m hub_cowork.mcp_servers.utility
# pipe in an MCP `initialize` JSON-RPC frame; it responds on stdout
```

This is how you verify a tool's `tools/list` advertisement looks the way the model will see it.

---

## Process and thread map

Hub Cowork is **single-process for the agent**, plus **N stdio MCP subprocesses** (lazy), plus **WebView2** (the UI), plus **optional Playwright Chromium** (only while a Computer-Use skill is running).

### Process inventory (steady state)

| OS process | How many | Spawned by | Lives until | Purpose |
|---|---|---|---|---|
| `pythonw.exe` — **host** | 1 | `python -m hub_cowork` (or `start.ps1`) | User exits via tray | Hosts the agent, WebSocket, HTTP, pywebview window, tray, executor pool, MCP client pool, optional Redis bridge |
| `msedgewebview2.exe` — UI | 1 + 2–3 helpers | pywebview, on first window create | Host exits | Renders `chat_ui.html`. Same Chromium engine VS Code uses |
| `pythonw.exe` — **MCP server** | 0–N (lazy) | `MCPClientPool._ensure_started` on first tool call to that server | Host exits (or server crashes; pool respawns on next call) | Runs ONE MCP server (`workiq` / `m365` / `utility` / per-skill `rfp_evaluation/mcp_server` / etc.). N today maxes at 4. Multiplexes calls from every conversation thread |
| `chrome.exe` (Playwright) | 0 or 1 | `core/computer_use.py` when a CUA skill runs | When the CUA run finishes | Headed Chromium for `shelf_watch` and any future Computer-Use skill |
| External: `workiq.exe` | 0 or 1 per call | `tools/query_workiq.py` via `subprocess.run` | The single CLI invocation | Short-lived M365 query CLI; not an MCP server |
| External: ACA `workiq-agent-remote-client` | 1 (separate machine) | Azure Container App | Independently | Teams ↔ Redis bridge. Not on your laptop. |

### Thread inventory (inside the host process)

The host is one Python process with several thread populations. Daemon threads die when the host exits.

| Thread name / pool | Count | Owner | What it does |
|---|---|---|---|
| MainThread | 1 | `host/desktop_host.py` | Boots the WS server, HTTP server, tray, then runs the **pywebview event loop** (the GUI message pump). Blocks here until window close |
| WebSocket server thread | 1 | `websockets.serve` (asyncio) | Accepts UI / test-client connections on `localhost:18080`; one asyncio task per connected client |
| HTTP server thread | 1 | `http.server` | Serves `chat_ui.{html,css,js}` on `localhost:18081` |
| Tray pump thread | 1 | `host/tray_icon.py` | Win32 message loop for the system-tray icon (`ctypes` + `winotify`) |
| `_ThreadWorker` daemons | **0..M** (one per active conversation) | `core/thread_executor.ExecutorPool` | Drains a per-thread queue of user messages, sets `current_thread_id` ContextVar, calls `agent_core.run_agent_on_thread`. **Idle-shuts-down after `IDLE_SHUTDOWN_SECONDS = 30 min`.** Lazily respawned on the next `submit()` |
| `MCPClientPool` owner tasks | 1 per warm MCP server | `MCPClientPool._owner_loop` running on a dedicated asyncio loop thread | Holds the `mcp.ClientSession` open for the server's lifetime. Pool dispatches calls onto these via per-server queues. **Crash → marked dead → respawned on next call** |
| `MCPClientPool` asyncio loop thread | 1 | `MCPClientPool` | Single dedicated asyncio loop that runs every owner task; survives the entire host lifetime |
| Redis bridge poll thread | 0 or 1 | `host/redis_bridge.py` (only if `AZ_REDIS_CACHE_ENDPOINT` set) | Long-poll on `XREAD` for inbound Teams messages; classifies and routes to threads |
| WS log handler thread | 1 | `logging` | Background flush of log records to UI as `log_entry` events (broadcasts read `current_thread_id`) |
| `LocalJsonThreadStore` writer | (no separate thread) | Inline | Debounced atomic writes from whichever thread mutates the conversation. Just a debounce timer |
| `_ThreadWorker` → `asyncio.run` (per call) | Transient | The worker thread | Each tool call invocation creates a short-lived asyncio loop on the worker thread to await the MCP pool's `call(...)` future bridged back to the pool's loop |

### Inside each MCP server subprocess

Tiny and boring — that's the design. One process per server, one main thread, one asyncio loop blocking on stdin.

| Thread | Count | What it does |
|---|---|---|
| MainThread | 1 | `anyio` event loop driving `mcp.server.lowlevel.Server.run()` over stdio. Reads JSON-RPC frames from stdin, dispatches to the tool function, writes results to stdout |
| Tool-spawned threads | Usually 0 | Most tools are synchronous (`requests`, `python-docx`, `subprocess.run`). A tool *may* spawn its own threads (e.g. `search_foundryiq` shares a `requests.Session` with a lock; `query_fabric_agent` polls the Assistants API). Process-local, dies with the server |

Idle MCP server CPU = 0% (blocked on stdin). Idle RSS ≈ 30–60 MB depending on imports.

### Who is shared, who is per-X

| Thing | Scope | Why |
|---|---|---|
| Host pythonw process | **One per machine** (per user session) | The product is a single-user desktop app |
| MCP server subprocess | **One per server name**, shared across **all conversation threads** AND **all conversations over time** | Lazy + warm + multiplexed via `MCPClientPool`. Not per-call, not per-thread, not per-skill |
| `MCPClientPool` | **One per host process** (host-scoped) | Lives in `agent_core` module-global state |
| `_ThreadWorker` | **One per active `ConversationThread`** | Conversations execute in parallel without blocking each other |
| `ConversationThread` object + on-disk JSON | **One per chat tab**, persisted to `~/.hub-cowork/threads/active/<id>.json` | Survives restart; `previous_response_id` is the only conversation state |
| `previous_response_id` | **Per `ConversationThread`** | Owned by Azure OpenAI server-side; we only hold the id |
| Entra credential (MSAL cache) | **One per user**, on-disk at `~/.hub-cowork/` | Shared by host AND every MCP subprocess via filesystem; silent token refresh works across processes |
| Azure OpenAI client object | **One per host** (lock-guarded for token rotation) | Reused across every Responses API call |
| WebView2 process tree | **One per host** | Single chat window |
| Playwright Chromium | **One per active CUA run** | Spawned only while running, torn down at end |

---

## Lifecycles at a glance

- **Cold start.** Tray launches `pythonw -m hub_cowork`. Host process boots: WS + HTTP + tray + pywebview window. Zero MCP subprocesses yet. Zero `_ThreadWorker`s yet.
- **First chat message.** `ThreadManager.create()` makes a `ConversationThread`. `ExecutorPool.submit()` spins up a `_ThreadWorker`. Worker calls `run_agent_on_thread`. Router picks a skill. `MCPClientPool` spawns the skill's MCP servers (one subprocess each, lazily). Tools run. Done.
- **Idle for 30 min on a thread.** Its `_ThreadWorker` exits. The conversation row stays in memory and on disk. The MCP servers stay warm.
- **Reply after an hour.** `ExecutorPool.submit()` lazily respawns a `_ThreadWorker` for that thread. MCP servers were never gone. Conversation resumes from `previous_response_id`.
- **HITL pause for a day.** Thread status flips to `awaiting_user`. The `_ThreadWorker` finishes its run and idle-shuts-down 30 min later. When the user replies, a fresh worker spins up.
- **MCP server crashes.** Pool detects `owner_task.done()` on the next call, logs a warning, respawns the subprocess, retries. No host restart needed.
- **Host exit.** Tray "Exit" closes the pywebview window → `MainThread` returns → all daemon threads die → pool sends shutdown to each MCP server → subprocesses exit.

---

## Memory footprint, ballpark

| | Idle | Active |
|---|---|---|
| Host `pythonw` | ~80–150 MB | +20–50 MB during a run |
| WebView2 (all helpers) | ~100–200 MB | similar |
| Each warm MCP server | ~30–60 MB | +10–30 MB during a call |
| Playwright Chromium (CUA only) | n/a | ~300–800 MB while a `shelf_watch` run is active |
| **Total at rest, all 4 MCP servers warm** | **~400–600 MB** | comparable to a single VS Code window |

CPU at rest is ~0% across the whole tree — every loop is blocked on a socket, a stdin pipe, or a queue.
