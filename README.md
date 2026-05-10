# Hub Cowork

> A local-first, single-user **Windows desktop AI agent** that runs an entire portfolio of Microsoft-stack workflows — engagement agendas, RFP intelligence briefs, meeting invites, retail price intelligence, M365 Q&A — through a unified **skills-based architecture** built on the **Azure OpenAI Responses API** and the **Model Context Protocol (MCP)**.
>
> Hub Cowork is intended as a **reference implementation** of the Anthropic [Claude Cowork](https://github.com/anthropics/knowledge-work-plugins) "skills + MCP" pattern, adapted to a Microsoft-cloud, single-Entra-identity, single-process desktop deployment.

---

## Table of contents

- [What it does](#what-it-does)
- [Why it exists — the design pattern](#why-it-exists--the-design-pattern)
- [The three layers](#the-three-layers)
- [Runtime architecture](#runtime-architecture)
- [How MCP is wired (the important nuance)](#how-mcp-is-wired-the-important-nuance)
- [Process and thread map](#process-and-thread-map)
- [Built-in skills](#built-in-skills)
- [The two clouds — M365 + Azure](#the-two-clouds--m365--azure)
- [Per-conversation concurrency](#per-conversation-concurrency)
- [Teams remote access (optional)](#teams-remote-access-optional)
- [Authentication](#authentication)
- [Configuration & Settings UI](#configuration--settings-ui)
- [Project structure](#project-structure)
- [Getting started](#getting-started)
- [WebSocket protocol](#websocket-protocol)
- [Service status monitor](#service-status-monitor)
- [Adding a skill or a tool](#adding-a-skill-or-a-tool)
- [Design docs](#design-docs)

---

## What it does

Hub Cowork is the daily driver of a Microsoft Innovation Hub Solution Engineer. From a single chat window (or from Microsoft Teams when you're away from your desk), the agent:

- Builds a **customer engagement agenda** end-to-end — finds the briefing call in your Outlook calendar, reads the meeting notes, classifies the engagement type (ADS, Hackathon, Business / Solution Envisioning, …), constructs a detailed agenda table with timings and speakers, and publishes it as a Word document into your OneDrive.
- Repurposes an existing agenda for a new customer.
- Sends **calendar invites** to the speakers on a published agenda.
- Compiles a **Bid Intelligence Brief** for an inbound RFP — pulls the RFP from your inbox, queries FoundryIQ (Azure AI Search over past customer testimonials), queries the Fabric Data Agent (Lakehouse-backed structured project data), synthesises the brief, saves it to OneDrive, and shares it with the team.
- Runs a **shelf-watch** — drives a real Chromium browser via Azure OpenAI's Computer-Use model to extract competitor pricing for SKUs across multiple retail websites, then compares against your last run.
- Answers **conversational Q&A** about any of your M365 data via WorkIQ.
- Reports **task status** instantly — even while a long-running task is in flight.

Everything runs on the user's laptop, under the user's Entra identity, with one sign-in.

---

## Why it exists — the design pattern

Production AI agents that try to encode workflow logic in one giant system prompt collapse under their own weight: the prompt grows past 5–10K tokens, every change risks a regression, the model starts hallucinating procedure, and human-in-the-loop turns devolve into brittle marker-string state machines (`[AWAITING_CONFIRMATION]`, `[STOP_CHAIN]`, …).

Anthropic's [Claude Cowork](https://github.com/anthropics/knowledge-work-plugins) fixed this with a clean three-layer split: the **model** orchestrates, **skills** carry domain expertise, **tools** do mechanical work. Hub Cowork is the same pattern, ported to:

- **Azure OpenAI** (Responses API) instead of Anthropic Claude
- **One shared Microsoft Entra identity** instead of per-tool OAuth
- **Local Python MCP servers** spawned by the host, calling Microsoft cloud services on the user's behalf (instead of cloud-resident MCP services)

The full charter is in [docs/architecture/SKILLS_DESIGN_PRINCIPLES.md](docs/architecture/SKILLS_DESIGN_PRINCIPLES.md). The 14 non-negotiables there are binding for every skill, tool, and runtime change in this repo. The condensed version follows.

---

## The three layers

| Layer | Owns | Lives in | What it must NOT do |
|---|---|---|---|
| **Model** (Azure OpenAI Responses API) | Tool selection, sequencing, conversation, HITL turns, error communication | The Responses API server-side loop | — |
| **Skill** (`SKILL.md`) | Domain expertise — when/why/what-if judgment, engagement-type heuristics, communication tone | `src/hub_cowork/skills/<name>/SKILL.md` | Numbered runbooks, string-formatting recipes, control-flow markers, anything that could be a Python function |
| **Tool** (`@mcp.tool`) | Mechanical work — fetch, parse, transform, write | An MCP server module under `mcp_servers/<name>/tools/` or `skills/<name>/mcp_server/tools/` | Decide what to tell the user, call other tools, construct credentials, hardcode user-facing prose |

The runtime in `core/agent_core.py` is **wiring, not intelligence**. It does not parse model output for control-flow markers. It does not chain skills. It does not interpret tool results. The canonical loop is exactly:

```python
while response.status == "requires_action":
    for tc in response.required_action.submit_tool_outputs.tool_calls:
        result = mcp_pool.call(server, tc.function.name, json.loads(tc.function.arguments),
                               on_progress=on_progress)
        outputs.append({"tool_call_id": tc.id, "output": result})
    response = client.responses.submit_tool_outputs(response_id=response.id,
                                                    tool_outputs=outputs)
```

That's it. ~30 lines. Everything else (skill discovery, model-tier routing, conversation persistence, HITL pause/resume, Teams bridging, UI broadcast) sits *around* this loop, never *inside* it.

### What this buys us

- **Adding a new workflow = drop in a folder.** A skill is `skill.yaml` + `SKILL.md` (+ optional `mcp_server/`). No registration, no manifest, no decorator.
- **Workflows stay single-skill.** The 4-phase agenda chain that used to exist (`hub_agenda_creation/{briefing,goals,build,publish}.yaml` with `next_skill` chaining and on-disk `engagement_context` JSON handoffs) collapsed into ONE `engagement_agenda` skill once procedure moved into tools and `previous_response_id` carried phase-to-phase context.
- **HITL is a conversation turn.** The model asks a question; the runtime sees a final text response with no pending tool calls and parks the thread at `awaiting_user`; the next message resumes via `previous_response_id`. No markers in instructions, no marker parsing in `agent_core`.
- **Tool composition is the model's job.** Tools never call other tools. The model orchestrates.

---

## Runtime architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Windows 11 desktop (one process)                   │
│                                                                             │
│  pywebview window  ◄── WebSocket ──►  desktop_host.py                       │
│  (chat_ui.html)        (port 18080)   • WS server + HTTP server (18081)     │
│                                       • System tray (Win32 ctypes)          │
│                                       • Optional Redis bridge               │
│                                            │                                │
│                                       ┌────▼─────┐                          │
│                                       │ Thread   │  one ConversationThread  │
│                                       │ Manager  │  per chat tab            │
│                                       └────┬─────┘                          │
│                                            │                                │
│                                       ┌────▼──────┐                         │
│                                       │ Executor  │  one daemon worker per  │
│                                       │ Pool      │  active thread          │
│                                       └────┬──────┘                         │
│                                            │                                │
│              ┌─────────────────────────────▼──────────────────────────┐     │
│              │             agent_core.run_agent_on_thread             │     │
│              │  1. Router (fast model)  →  pick skill or "none"       │     │
│              │  2. Load SKILL.md + skill.yaml                         │     │
│              │  3. List MCP tools from each server in skill.yaml      │     │
│              │  4. responses.create(...) with function-tool schemas   │     │
│              │  5. while requires_action: dispatch into MCPClientPool │     │
│              │  6. Persist previous_response_id on the thread         │     │
│              └────────────┬───────────────────────────────────────────┘     │
│                           │                                                 │
│                ┌──────────▼─────────────┐                                   │
│                │     MCPClientPool      │  one warm stdio subprocess        │
│                │  (host-scoped, lazy)   │  per server, multiplexed          │
│                └─────────┬──────────────┘  across all conversation threads  │
│                          │                                                  │
│         ┌────────────────┼────────────────────────────────────┐             │
│         ▼                ▼                ▼                   ▼             │
│   ┌──────────┐    ┌────────────┐   ┌────────────┐   ┌──────────────────┐    │
│   │ workiq   │    │   m365     │   │ utility    │   │ per-skill server │    │
│   │ MCP srv  │    │  MCP srv   │   │  MCP srv   │   │ (e.g. rfp_eval/  │    │
│   │ (shared) │    │  (shared)  │   │  (shared)  │   │  mcp_server/)    │    │
│   └────┬─────┘    └─────┬──────┘   └──────┬─────┘   └──────┬───────────┘    │
└────────┼────────────────┼─────────────────┼────────────────┼────────────────┘
         │                │                 │                │
         ▼                ▼                 ▼                ▼
   WorkIQ CLI        Graph + ACS        in-process    FoundryIQ + Fabric
   (M365 data)       (mail / docs)      utilities     (RFP knowledge)
```

### What sits where

| Component | Module | Role |
|---|---|---|
| Agent core | [`core/agent_core.py`](src/hub_cowork/core/agent_core.py) | Router, skill loader, `MCPClientPool` registration, the `requires_action` execution loop |
| MCP client pool | [`core/mcp_client_pool.py`](src/hub_cowork/core/mcp_client_pool.py) | Lazy spawn of MCP server subprocesses, multiplexed calls, progress-notification forwarding |
| MCP server runtime | [`mcp_servers/_runtime.py`](src/hub_cowork/mcp_servers/_runtime.py) | Shared stdio MCP server entrypoint used by every server module |
| Auth | [`core/auth_credential.py`](src/hub_cowork/core/auth_credential.py) | WAM-broker-first single Entra credential, parented to the pywebview HWND |
| Conversation state | [`core/conversation_thread.py`](src/hub_cowork/core/conversation_thread.py) | The `ConversationThread` dataclass — `previous_response_id`, status, progress/code logs, source, HITL correlation tag |
| Thread registry | [`core/thread_manager.py`](src/hub_cowork/core/thread_manager.py) | Thread-safe registry, observer pattern, `current_thread_id` ContextVar |
| Executor pool | [`core/thread_executor.py`](src/hub_cowork/core/thread_executor.py) | One daemon worker per active conversation, idle-shutdown |
| Persistence | [`core/thread_store.py`](src/hub_cowork/core/thread_store.py) | `LocalJsonThreadStore` with debounced atomic writes |
| Hub config | [`core/hub_config.py`](src/hub_cowork/core/hub_config.py) | Defaults ⊕ user overrides + `_env_overrides` env editor support |
| Service status | [`core/service_status.py`](src/hub_cowork/core/service_status.py) | Per-service reachability, passive + active probes |
| Computer-Use harness | [`core/computer_use.py`](src/hub_cowork/core/computer_use.py) | Generic Azure OpenAI gpt-5.4 + Playwright Chromium loop (used by `shelf_watch`) |
| Desktop host | [`host/desktop_host.py`](src/hub_cowork/host/desktop_host.py) | WS + HTTP servers, pywebview window, tray, Redis bridge wiring |
| Console host | [`host/console.py`](src/hub_cowork/host/console.py) | Terminal REPL — same agent core, no UI, no Redis bridge |
| Redis bridge | [`host/redis_bridge.py`](src/hub_cowork/host/redis_bridge.py) | Teams remote-message inbox/outbox, classifier, per-user gate, `#thread-xxxx` correlation |

---

## How MCP is wired (the important nuance)

Tools are not loose Python modules with a `SCHEMA` dict and a `handle()` function. They are **MCP servers** — the same Model Context Protocol used by Claude Cowork. Each server is a subprocess; each tool is a `@mcp.tool`-style function inside one of those subprocesses.

This buys us three things at once:

1. **Process isolation.** A buggy tool can crash its server without taking the agent down.
2. **A clean future migration to Azure Container Apps.** When a server moves to ACA, `skill.yaml` flips one line (stdio → streamable HTTP); skill code, tool code, and the agent loop are unchanged.
3. **Multi-language tools (eventually).** Any language that speaks MCP can host a Hub Cowork tool — today they're all Python because that's what we have.

### …but watch out for the subtlety

You will look at `src/hub_cowork/mcp_servers/` and see Python files that look like ordinary modules. Where is the protocol?

The protocol code lives in [`mcp_servers/_runtime.py`](src/hub_cowork/mcp_servers/_runtime.py). It imports `mcp.types`, `mcp.server.lowlevel.Server`, and `mcp.server.stdio.stdio_server` from the official `mcp` Python package and exposes a `serve(...)` helper. Every other folder under `mcp_servers/` (`workiq/`, `m365/`, `utility/`) and every per-skill `mcp_server/` folder is a thin manifest that lists its tools and calls `serve(...)`. That's the wire-level MCP server.

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

MCP server subprocesses do not receive credentials over the MCP transport. They call `get_credential()` from [`core/auth_credential.py`](src/hub_cowork/core/auth_credential.py) themselves and silently mint scope-specific tokens from the same `~/.hub-cowork/`-rooted MSAL cache the host wrote at sign-in. **Never construct credentials in a tool. Never accept tokens as arguments.**

### Inspecting the wire

You can run any one server module standalone and speak MCP at it:

```powershell
python -m hub_cowork.mcp_servers.utility
# pipe in an MCP `initialize` JSON-RPC frame; it responds on stdout
```

This is how you verify a tool's `tools/list` advertisement looks the way the model will see it.

---

## Process and thread map

A future-you cheat-sheet for what's actually running on the laptop when Hub Cowork is up. Hub Cowork is **single-process for the agent**, plus **N stdio MCP subprocesses** (lazy), plus **WebView2** (the UI), plus **optional Playwright Chromium** (only while a Computer-Use skill is running).

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

### Lifecycles at a glance

- **Cold start.** Tray launches `pythonw -m hub_cowork`. Host process boots: WS + HTTP + tray + pywebview window. Zero MCP subprocesses yet. Zero `_ThreadWorker`s yet.
- **First chat message.** `ThreadManager.create()` makes a `ConversationThread`. `ExecutorPool.submit()` spins up a `_ThreadWorker`. Worker calls `run_agent_on_thread`. Router picks a skill. `MCPClientPool` spawns the skill's MCP servers (one subprocess each, lazily). Tools run. Done.
- **Idle for 30 min on a thread.** Its `_ThreadWorker` exits. The conversation row stays in memory and on disk. The MCP servers stay warm.
- **Reply after an hour.** `ExecutorPool.submit()` lazily respawns a `_ThreadWorker` for that thread. MCP servers were never gone. Conversation resumes from `previous_response_id`.
- **HITL pause for a day.** Thread status flips to `awaiting_user`. The `_ThreadWorker` finishes its run and idle-shuts-down 30 min later. When the user replies, a fresh worker spins up. (See ["Per-conversation concurrency"](#per-conversation-concurrency) for the full HITL story.)
- **MCP server crashes.** Pool detects `owner_task.done()` on the next call, logs a warning, respawns the subprocess, retries. No host restart needed.
- **Host exit.** Tray "Exit" closes the pywebview window → `MainThread` returns → all daemon threads die → pool sends shutdown to each MCP server → subprocesses exit.

### Memory footprint, ballpark

| | Idle | Active |
|---|---|---|
| Host `pythonw` | ~80–150 MB | +20–50 MB during a run |
| WebView2 (all helpers) | ~100–200 MB | similar |
| Each warm MCP server | ~30–60 MB | +10–30 MB during a call |
| Playwright Chromium (CUA only) | n/a | ~300–800 MB while a `shelf_watch` run is active |
| **Total at rest, all 4 MCP servers warm** | **~400–600 MB** | comparable to a single VS Code window |

CPU at rest is ~0% across the whole tree — every loop is blocked on a socket, a stdin pipe, or a queue.

---

## Built-in skills

Each skill is a folder under `src/hub_cowork/skills/` containing `skill.yaml` (config) + `SKILL.md` (system prompt) + optional `mcp_server/` (skill-local tools).

| Skill | Tier | Queued | What it does |
|---|---|---|---|
| **`engagement_agenda`** | reasoning | yes | Single-skill, multi-phase workflow. Finds briefing calls, confirms with user (HITL), reads meeting notes, classifies engagement type, builds a detailed agenda table, publishes to Word in OneDrive. Replaces the legacy 4-phase chain. |
| **`agenda_repurpose`** | reasoning | yes | Retrieve an existing agenda, collect new customer details, produce a repurposed Word document. |
| **`meeting_invites`** | reasoning | yes | From a published agenda, filter speakers, resolve their emails, send calendar invites via ACS. |
| **`rfp_evaluation`** | reasoning | yes | Pull an RFP from email, parallel-fan-out to FoundryIQ (testimonials) and Fabric Data Agent (structured project history), synthesise a Bid Intelligence Brief, save to OneDrive, share with the team. |
| **`shelf_watch`** | reasoning | yes | Single-tool computer-use workflow. SKU plausibility check → discovery sweep across retailers (vision-LLM triage of result pages) → variant disambiguation HITL → deep scrape of confirmed PDPs → Word report with vs-Last-Run delta. |
| **`qa`** | fast | yes | Conversational Q&A about M365 data with per-thread history. |
| **`task_status`** | fast | no | Reports current thread progress and active-thread count — runs on the SYSTEM pseudo-thread so it answers instantly even while a real task is in flight. |
| *(router direct)* | fast | no | Greetings and small talk — classified as `"none"` and answered by the router itself. No skill is invoked. |

**Queued** = serializes on the conversation's own executor thread. Non-queued skills run on the SYSTEM pseudo-thread immediately.

### Where each skill's tools live

```
src/hub_cowork/
├── mcp_servers/                    # shared MCP servers (used by ≥2 skills)
│   ├── workiq/                     # query_workiq
│   ├── m365/                       # send_email, create_word_doc, resolve_speakers
│   ├── utility/                    # log_progress, get_hub_config, get_task_status
│   ├── _runtime.py                 # the actual MCP wire-protocol layer
│   └── _tool_result.py             # standard result envelope
└── skills/
    ├── engagement_agenda/          # tools live in shared servers — no local mcp_server
    ├── agenda_repurpose/
    ├── meeting_invites/
    │   └── mcp_server/tools/       # create_meeting_invites
    ├── rfp_evaluation/
    │   └── mcp_server/tools/       # search_foundryiq, query_fabric_agent,
    │                               # create_rfp_brief_doc, share_onedrive_document,
    │                               # create_calendar_reminder
    ├── shelf_watch/
    │   └── mcp_server/tools/       # shelf_watch_run + private _compare/_discover/_memory/_report/_session
    ├── qa/
    └── task_status/
```

---

## The two clouds — M365 + Azure

Hub Cowork bridges two Microsoft clouds with the user's **Microsoft Entra** identity flowing on-behalf-of across both:

**Microsoft 365 cloud — the user's work intelligence**

- **WorkIQ** runs as a CLI on the local computer and is the backbone of the user's identity for everything that follows. Once the user signs in (WAM broker, persistent token cache), the same Entra credential is shared with Azure OpenAI, ACS, Redis, FoundryIQ, Fabric, and every MCP server subprocess.
- Through WorkIQ, the agent reads the user's calendars, emails, OneDrive, SharePoint, and contacts with their own permissions.

**Azure cloud — reasoning, knowledge, and structured insight**

- **Azure OpenAI Responses API** — the autonomous reasoning core that orchestrates the multi-step agent loop via function calling. Two model deployments: a reasoning model (e.g. `gpt-5.2`) and a fast model (e.g. `gpt-5.4-mini`).
- **FoundryIQ** — Agentic RAG over customer testimonials from past engagements (Azure Blob Storage → Azure AI Search). Used by the RFP skill.
- **Fabric Data Agent** — natural-language interface over OneLake-resident structured project data (risks, costs, timelines, KPIs). Called directly via its OpenAI-compatible Assistants endpoint with a Fabric-scoped Entra bearer token.
- **Azure Communication Services** — calendar invite email, used by `meeting_invites`.
- **Azure Managed Redis (cluster mode, optional)** — Teams remote-message inbox/outbox bridge. Passwordless Entra ID via `redis-entraid` credential provider.

The user's Entra token issued at sign-in is propagated on-behalf-of to all of the above. The agent never holds a shared service principal.

---

## Per-conversation concurrency

Hub Cowork is single-process but multi-threaded. Every chat tab is its own `ConversationThread` with its own Responses-API context (its own `previous_response_id`). You can be running an RFP brief in tab A while asking a Q&A question in tab B — they execute in parallel.

| Layer | What happens |
|---|---|
| `ThreadManager` | Thread-safe registry of `ConversationThread` objects. Observer pattern for UI broadcast. Owns the `current_thread_id` `ContextVar` so logs and progress events route to the correct chat panel. |
| `ExecutorPool` | One daemon `_ThreadWorker` per active conversation. Workers idle-shut-down after a configurable interval. Each worker sets `current_thread_id` before dispatching. |
| `LocalJsonThreadStore` | Debounced atomic writes under `~/.hub-cowork/threads/{active,archive}/`. Threads survive restart and resume from `previous_response_id`. |
| `MCPClientPool` | **Host-scoped, not thread-scoped.** One subprocess per server name, multiplexed across all conversation threads. |

A SYSTEM pseudo-thread (`thread_id == "system"`) handles cross-task questions ("what threads do I have running?"). It is never persisted and never accumulates `previous_response_id` — every system query is one-shot. This is why `task_status` can answer instantly even while a real task is in flight.

---

## Teams remote access (optional)

When `AZ_REDIS_CACHE_ENDPOINT` is set, a separate **Azure Container App** ([workiq-agent-remote-client](https://github.com/sansri/workiq-agent-remote-client), built on the **Microsoft 365 Agents SDK** and fronting an **Azure Bot Service** channel) bridges Microsoft Teams to the agent via Azure Managed Redis streams.

The user fires off a workflow from Teams on their phone, closes the laptop lid, and gets the result back asynchronously when the workflow completes — because the local agent runs autonomously in the system tray.

Three mechanisms keep multi-thread remote traffic predictable:

1. **Inbox classifier** (`agent_core.classify_inbox`) — every inbound Teams message is classified `new`, `existing`, or `system`. Strong signals bias toward `existing` when the user is replying to a multi-field question, numbered options, or a yes/no confirmation. Fast-path: if the relay supplies a `thread_id` hint extracted from the `#thread-xxxx` correlation tag, the classifier verdict for that thread is honored without an LLM call.
2. **Per-Teams-user gate** — for `new` classifications only, if the same Teams user already has an in-flight thread (`running` or `awaiting_user`, `source=="remote"`), the new thread is rejected with an outbox message tagged to the blocking thread's correlation. `existing` and `system` bypass the gate so HITL replies and status checks are never blocked.
3. **`#thread-xxxx` correlation tags** — every outbound Teams reply is prefixed with the thread's `hitl_correlation_tag`. Users keep the tag in their Teams reply to deterministically route follow-ups; the relay strips the tag from user-visible text and forwards it as a structured hint.

Redis keys are namespaced by `REDIS_NAMESPACE` (default `hub-cowork`) so multiple Hub Cowork forks/deployments can share one Redis cluster without colliding.

---

## Authentication

Hub Cowork is a **multi-tenant** desktop agent: it talks to Azure OpenAI / ACS / WorkIQ in the user's home tenant (`AZURE_TENANT_ID`) and to FoundryIQ + Fabric Data Agent in a separate resource tenant (`RESOURCE_TENANT_ID`) where the user is a guest. To make this look and feel native — and silent on every restart — the auth stack is:

1. **WAM (Windows Account Manager) broker** via [`azure-identity-broker`](https://pypi.org/project/azure-identity-broker/) — the same native account picker Teams, Outlook, and Office show. **No browser opens.** The pywebview HWND is registered with `set_parent_window_handle()` so the picker is modal to our UI.
2. **Classic `InteractiveBrowserCredential` fallback** when `pymsalruntime` is missing (macOS / Linux / older Windows).
3. **`AuthenticationRecord` persisted to disk** for silent token refresh across restarts.
4. **One credential, shared everywhere.** Built once in `agent_core` startup; published via `set_credential()`; consumed via `get_credential()` by the OpenAI client, `redis-entraid` provider, ACS sender, every MCP server subprocess (which reads the same on-disk MSAL cache), and the `outlook_helper`.
5. **No `DefaultAzureCredential`. No `az` CLI subprocesses** (we run under `pythonw.exe` — no console).

Sign-in is triggered from the Settings UI. The token blob is stored under the cache name `hub_cowork` so this fork doesn't fight a sibling fork over the same cached secret.

---

## Configuration & Settings UI

There are two distinct stores of settings:

### 1. Hub config (JSON) — application data

Things the agent reads as structured data via the `get_hub_config` tool: hub name, default session start time, topic catalog, agenda output folder, agenda template path, shelf-watch retailer registry.

```
src/hub_cowork/assets/hub_config.default.json   ← Shipped defaults
~/.hub-cowork/hub_config.json                   ← User overrides (created on first Save)

hub_config.load() returns:  defaults  ⊕  user overrides   (user wins per-key)
```

### 2. Environment variables — endpoints, model names, secrets

Three precedence layers, highest first:

| # | Source | Where it lives | Wins when |
|---|---|---|---|
| 1 | `_env_overrides` (Settings UI) | `~/.hub-cowork/hub_config.json` under `_env_overrides` | Always wins if set to a non-empty string |
| 2 | User `.env` file | CWD at launch | Wins over packaged defaults |
| 3 | Packaged `.env.defaults` | `src/hub_cowork/assets/.env.defaults` | Last-resort fallback |

[`__main__.py`](src/hub_cowork/__main__.py) promotes `_env_overrides` into `os.environ` *before* importing `agent_core`, then calls `load_dotenv(.env, override=False)` and `load_dotenv(assets/.env.defaults, override=False)`. `override=False` is the key — once a value is in `os.environ` it cannot be downgraded by a lower-precedence source.

**Net result:** there is exactly one source of truth per env var, computed at boot, regardless of which layer set it. The same merged view is visible to Python `os.environ` reads and to skill instructions via the `get_hub_config` tool (which flattens non-empty `_env_overrides` on top of the hub-config JSON before returning).

### Settings UI

The kebab (⋮) menu in the top-right opens **Settings**, **Restart agent**, **Skills**, and **About Hub Cowork**. The Settings modal has two sections:

- **Hub settings** — top-level keys in `hub_config.json` (hub name, session start time, speakers by topic, agenda/RFP output folders).
- **Environment variables** — every value typed here is saved to `_env_overrides` and applied on the next launch (the UI offers a one-click restart). Hub-config edits are picked up live by the next `get_hub_config` call.

### Required env vars

| Variable | Description |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_CHAT_MODEL` | Reasoning model deployment name |
| `AZURE_OPENAI_CHAT_MODEL_SMALL` | Fast model deployment name |
| `AZURE_OPENAI_API_VERSION` | e.g. `2025-03-01-preview` |
| `AZURE_TENANT_ID` | Home tenant ID for sign-in |
| `ACS_ENDPOINT`, `ACS_SENDER_ADDRESS` | Azure Communication Services (meeting invites) |

### Optional env vars

| Variable | Used by |
|---|---|
| `AZ_REDIS_CACHE_ENDPOINT`, `REDIS_NAMESPACE`, `REDIS_SESSION_TTL_SECONDS` | Teams remote bridge |
| `WORKIQ_PATH` | If WorkIQ CLI is not on `PATH` |
| `FOUNDRYIQ_*`, `FABRIC_DATA_AGENT_URL`, `FABRIC_AUTH_MODE`, `RESOURCE_TENANT_ID` | RFP skill |
| `RFP_OUTPUT_FOLDER`, `RFP_SHARE_RECIPIENTS` | RFP skill |
| `GRAPH_*` | Optional app-cred fallback for OneDrive sharing |
| `AGENT_TIMEZONE` | IANA TZ override (auto-detected otherwise) |

---

## Project structure

```
hub-cowork/
├── pyproject.toml
├── requirements.txt
├── README.md                            ← you are here
├── docs/
│   ├── architecture.png
│   ├── ui-architecture.md
│   └── architecture/
│       ├── SKILLS_DESIGN_PRINCIPLES.md  ← authoritative spec (the 14 non-negotiables)
│       ├── AUTHORING_A_SKILL.md         ← practical recipe for adding a skill
│       └── REARCHITECTURE_PLAN.md       ← migration audit trail
│
├── scripts/
│   ├── start.ps1     restart.ps1     stop.ps1
│   └── autostart.ps1
│
├── test-client/
│   └── chat.py                          ← console REPL that simulates a Teams relay
│
└── src/hub_cowork/
    ├── __main__.py                      ← `python -m hub_cowork` entry; applies env overrides
    │
    ├── core/                            ← runtime, no I/O wiring
    │   ├── agent_core.py
    │   ├── auth_credential.py
    │   ├── conversation_thread.py
    │   ├── thread_manager.py
    │   ├── thread_executor.py
    │   ├── thread_store.py
    │   ├── mcp_client_pool.py
    │   ├── hub_config.py
    │   ├── service_status.py
    │   ├── computer_use.py
    │   ├── outlook_helper.py
    │   └── app_paths.py
    │
    ├── host/                            ← runtime hosts
    │   ├── desktop_host.py              ← WS + HTTP servers, pywebview, tray, Redis wiring
    │   ├── console.py                   ← terminal REPL, no UI, no Redis
    │   ├── redis_bridge.py              ← Teams inbox/outbox + classifier + per-user gate
    │   ├── tray_icon.py                 ← Win32 ctypes tray with red-dot badge
    │   └── ui_actions.py
    │
    ├── mcp_servers/                     ← shared MCP servers (the actual MCP wire layer)
    │   ├── _runtime.py                  ← `serve(...)` — the lowlevel `mcp.server.Server`
    │   ├── _tool_result.py              ← standard envelope helpers
    │   ├── workiq/                      ← query_workiq
    │   ├── m365/                        ← send_email, create_word_doc, resolve_speakers
    │   └── utility/                     ← log_progress, get_hub_config, get_task_status
    │
    ├── skills/                          ← one folder per skill, auto-discovered
    │   ├── engagement_agenda/   skill.yaml + SKILL.md
    │   ├── agenda_repurpose/    skill.yaml + SKILL.md
    │   ├── meeting_invites/     skill.yaml + SKILL.md + mcp_server/
    │   ├── rfp_evaluation/      skill.yaml + SKILL.md + mcp_server/
    │   ├── shelf_watch/         skill.yaml + SKILL.md + mcp_server/
    │   ├── qa/                  skill.yaml + SKILL.md
    │   └── task_status/         skill.yaml + SKILL.md
    │
    └── assets/
        ├── chat_ui.html, chat_ui.css, chat_ui.js
        ├── hub_config.default.json
        └── .env.defaults
```

---

## Getting started

### Prerequisites

- **Windows 11** (Mac/Linux work for the core but tray + WAM are Windows-only)
- **Python 3.12+**
- **WorkIQ CLI** on `PATH` (or `WORKIQ_PATH` set)
- **Azure OpenAI** with a reasoning + fast model deployment
- **Azure Communication Services** (for meeting invites)
- **Azure Managed Redis** (optional — for Teams remote)

### Install

```powershell
git clone <repo-url>
cd hub-cowork

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

copy .env.example .env
# Fill in Azure endpoints, model names, tenant id, ACS, optional Redis.
```

### Run

```powershell
# Headless (production) — no console window, runs under pythonw
.\scripts\start.ps1

# Stop / restart
.\scripts\stop.ps1
.\scripts\restart.ps1

# With console output (debug)
python -m hub_cowork

# Console REPL — same agent core, no UI, no Redis bridge
hub-cowork-console
```

`pip install -e .` registers two console scripts in `pyproject.toml`:

- `hub-cowork` — GUI launcher (no console window)
- `hub-cowork-console` — terminal REPL

### Auto-start at Windows login

```powershell
.\scripts\autostart.ps1 install
.\scripts\autostart.ps1 uninstall
```

---

## WebSocket protocol

All UI ↔ backend messages are JSON with a `type` field on `ws://localhost:18080`. Every invocation carries a `request_id` (`uuid.uuid4().hex[:8]`) used for correlation across WebSocket, UI, Redis outbox, and log entries.

**Client → server:** `create_thread`, `send_to_thread`, `cancel_thread`, `list_threads`, `get_thread`, `archive_thread`, `unarchive_thread`, `list_archived_threads`, `delete_thread`, `system_query`, `task`, `open_file`, `signin`, `clear_history`, `get_logs`, `get_config`, `save_config`, `validate_speakers`, `restart`.

**Server → client:** `threads_list`, `archived_threads_list`, `thread_created`, `thread_updated`, `thread_detail`, `thread_started`, `thread_progress`, `thread_completed`, `thread_error`, `thread_archived`, `thread_unarchived`, `thread_deleted`, `thread_unread`, `cancel_ack`, `log_entry`, `log_history`, `history_cleared`, `system_query_started`, `system_query_progress`, `system_query_complete`, `system_query_error`, `auth_status`, `signin_status`, `skills_list`, `service_status`, `config_data`, `config_saved`, `config_warning`, `restart_ack`, `progress`, `remote_message`, `error`.

### Redis streams (when Teams bridge is enabled)

| Key | Direction | Fields |
|---|---|---|
| `{ns}:inbox:{email}` | Remote → Agent | `sender`, `text`, `ts`, `msg_id`, optional `thread_id` hint |
| `{ns}:outbox:{email}` | Agent → Remote | `task_id`, `status`, `text` (prefixed with `#thread-xxxx`), `ts`, `in_reply_to` |
| `{ns}:agents:{email}` | Agent → Cloud | JSON: `{name, email, started_at, version}` with TTL refreshed every 30 min |

---

## Service status monitor

The top-bar pills (`MicrosoftIQ: WorkIQ • FoundryIQ • FabricIQ` and `Teams: Relay`) are driven by [`core/service_status.py`](src/hub_cowork/core/service_status.py), tracking reachability for the four external services with two update paths:

- **Passive — every tool call.** Tool result envelopes flow through `mark_from_envelope(...)`. `ok` / `no_data` → green; `error` with `kind=config` → grey; other errors → red.
- **Active — background probe thread.** Every 120s, services in `unknown` state or older than the interval are re-probed under a 6s budget. Probes never trigger interactive auth — they short-circuit to `unknown` until the user has signed in.

The Redis tile is updated directly by the bridge (it owns the connection and the presence-key TTL heartbeat) and is intentionally not in the active-probe rotation.

The Fabric probe is **token-acquisition only** — it doesn't make a real call to the Fabric Data Agent (which would spin up a Fabric thread + run and cost real compute). This is intentional but can false-positive in cross-tenant guest scenarios; if you ever see "FoundryIQ red, Fabric green, but actual Fabric calls fail", suspect a cross-tenant credential reuse problem.

---

## Adding a skill or a tool

The full recipe is in [docs/architecture/AUTHORING_A_SKILL.md](docs/architecture/AUTHORING_A_SKILL.md). The very short version:

### A new skill

```
src/hub_cowork/skills/<your_skill>/
├── skill.yaml      # config only — name, description, mcp_servers, tool_allowlist, model_tier, queued
├── SKILL.md        # the system prompt — domain expertise, NOT procedure
└── mcp_server/     # OPTIONAL — only if this skill needs new tools no other skill uses
    ├── __init__.py
    ├── __main__.py
    └── tools/
        └── <your_tool>.py
```

Restart. The skill is auto-discovered, indexed by the loader, included in the router prompt, and invokable. There is no registration, no manifest, no decorator.

**`skill.yaml` is config only.** No `instructions:`, no `next_skill:`, no `conversational:`, no prose. If you find yourself writing prose into YAML, you are doing it wrong — it goes in `SKILL.md`.

**`SKILL.md` carries judgment, not procedure.** A senior solution engineer should read it and nod. If it reads like a numbered runbook a robot follows, you are doing it wrong — push the procedural bits into a tool.

### A new tool

Pick the right server:

- **Skill-local** (used by one skill) → `skills/<skill>/mcp_server/tools/<tool>.py`
- **Shared** (used by ≥2 skills, or wraps a foundational service like WorkIQ / Graph / ACS) → `mcp_servers/<server>/tools/<tool>.py`

Don't preemptively share. Promote a tool to a shared server only when a second skill actually needs it.

Tool functions return **JSON-serializable structured facts** with a `status`/`found` discriminator. **Never user-facing prose.** Never call other tools (within or across servers — composition is the model's job). Never construct credentials — call `get_credential()` from inside the tool. Progress flows through MCP notifications, which the pool forwards to `on_progress`.

### The lint script

```powershell
python scripts/lint_skills.py
```

Run after any skill edit. The lint blocks the banned legacy patterns (`instructions:` in YAML, `[STOP_CHAIN]` / `[AWAITING_CONFIRMATION]` markers, `next_skill:`, `conversational:`) and confirms each skill folder is well-formed.

---

## Design docs

| Document | Status | Purpose |
|---|---|---|
| [SKILLS_DESIGN_PRINCIPLES.md](docs/architecture/SKILLS_DESIGN_PRINCIPLES.md) | **Authoritative spec** | The 14 non-negotiables. The §9 PR checklist. Part I (the three layers), Part II (runtime mechanics), Part III (deliberate divergences from Cowork). Read this before authoring or modifying any skill, tool, or runtime change. |
| [AUTHORING_A_SKILL.md](docs/architecture/AUTHORING_A_SKILL.md) | Practical guide | The recipe — `skill.yaml` shape, `SKILL.md` style guide, MCP tool packaging, HITL pattern, validation. |
| [REARCHITECTURE_PLAN.md](docs/architecture/REARCHITECTURE_PLAN.md) | Migration audit trail | The phased plan that brought the codebase from the legacy chained-skills + loose-Python-tools shape to the current MCP + single-skill shape. Useful as historical context. |
| [ui-architecture.md](docs/ui-architecture.md) | UI internals | Three-pane layout, breakpoint behaviour, in-chat step cards. |

### Reference

- [Anthropic knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins) — the original SKILL.md pattern and the Claude Cowork skills+MCP design Hub Cowork is modelled on.
- [Anthropic skills repo](https://github.com/anthropics/skills) — general-purpose skills.
- [Model Context Protocol spec](https://modelcontextprotocol.io/) and the [Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp` package).
- [Azure OpenAI Responses API](https://learn.microsoft.com/azure/ai-services/openai/concepts/responses) — the model orchestration layer.
- [`workiq-agent-remote-client`](https://github.com/sansri/workiq-agent-remote-client) — the Teams relay (Azure Container App on the Microsoft 365 Agents SDK).
