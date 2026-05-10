# Project Guidelines

## Design Principles (binding)

**Read [`docs/architecture/SKILLS_DESIGN_PRINCIPLES.md`](../docs/architecture/SKILLS_DESIGN_PRINCIPLES.md) before authoring or modifying any skill, tool, runtime code, or `agent_core` change.** It is the authoritative spec for **the entire Hub Cowork architecture** — skills, tools, tool packaging, skill discovery, routing, the agent loop, conversation state, auth sharing, and progress streaming. Hub Cowork is being built as a **reference implementation** of the Claude-Cowork-style "skills-based economy" pattern on Azure OpenAI Responses API.

The doc is split into three parts:

- **Part I (§1–§10)** — the three layers (model = orchestrator, skill = expertise, tool = mechanical work), banned patterns, and the §9 PR checklist.
- **Part II (§11–§17)** — runtime mechanics: tool packaging, skill loading, routing, the agent loop's exact shape, conversation state, auth sharing, progress streaming.
- **Part III (§18)** — the deliberate divergences from Cowork (per-skill tool scoping, model-tier routing, single Entra credential, local Python tools) and the rationale for each.

The non-negotiables (full rationale and runtime mechanics in the design doc):

1. **Three layers, no mixing.** Model = orchestrator. Skill = domain expertise (judgment). Tool = mechanical work (data + transformation + side effects). Runtime = wiring, no decisions.
2. **Skill instructions live in `SKILL.md` (markdown), not in `skill.yaml`.** YAML is runtime config only: `name`, `description`, `tools`, `model_tier`, `reasoning_effort`, `queued`. No `instructions:`, no `next_skill:`, no `conversational:`.
3. **Skills carry expertise, not procedure.** No numbered step-by-step runbooks. No string-formatting recipes. No "if X output exact-string Y" rules. If it can be a Python function, it must be.
4. **Tools return structured facts, not user-facing prose.** Return JSON like `{"found": false, "customer": "..."}` — let the model decide what to say. Never hardcode error sentences in a tool's return value.
5. **One skill per workflow, not one skill per phase.** Multi-step workflows (like the agenda creation chain) are a single skill with smarter tools, not chained skills with `next_skill` and on-disk JSON handoffs.
6. **HITL is a conversation turn, not a state machine.** No `[AWAITING_CONFIRMATION]` / `[STOP_CHAIN]` markers in instructions. The model asks; the runtime parks the thread at `awaiting_user`; the next message resumes via `previous_response_id`.
7. **`agent_core` is a mechanical bridge.** It does not parse model output for control flow, does not chain skills, does not duplicate the Responses API's tool-call loop. The canonical loop is `while requires_action: execute → submit_tool_outputs` and nothing more (§14.1).
8. **Tool packaging is folder-based and registry-free.** One file per tool, exporting `SCHEMA: dict` and `handle()`. Auto-discovered from `tools/` and `skills/<skill>/tools/`. No decorators, no manifests, no central enum (§11).
9. **Skills are folders, auto-discovered.** A folder with `skill.yaml` + `SKILL.md` is a skill — no registration, no manifest. The router prompt is built from each skill's `description` (§12).
10. **Routing is one cheap fast-model call** that returns a skill name or `"none"`. The router does not see tool definitions (§13).
11. **`previous_response_id` is the only conversation state.** No `thread.messages`, no per-skill scratchpads, no inter-phase JSON store (§15).
12. **One shared Entra credential.** Tools call `get_credential()` and trust it. Never re-instantiate, never `DefaultAzureCredential`, never `az` CLI (§16, also §9b/preserved).
13. **Progress is `on_progress(kind, message)` only.** Tool returns are JSON for the model, not prose for the UI. `log_progress` is the model's microphone (§17).
14. **Tools are MCP servers — stdio now, HTTP later.** Each skill owns a per-skill MCP server under its folder; cross-cutting tools live in shared servers under `src/hub_cowork/mcp_servers/`. The host's `MCPClientPool` spawns each server as a stdio subprocess on first use and keeps it warm for the host's lifetime — calls are multiplexed, never one-subprocess-per-call. Stdio MCP servers **cannot** be passed to the Azure OpenAI Responses API as native `mcp` tools (that mode only accepts remote HTTP URLs); they are registered as ordinary `type="function"` tools built from each server's `tools/list` advertisement, and the agent loop dispatches `requires_action` calls into the pool. Auth crosses the boundary via the shared on-disk MSAL cache; servers call `get_credential()` themselves. Future move to ACA is a transport swap (stdio → streamable HTTP) declared in `skill.yaml`; tools usually stay client-dispatched, with native Responses-API `mcp` registration as an opt-in per server (§8, §11, §11.4a, §11.7, §14.1, §18.5).

Run the §9 checklist in `SKILLS_DESIGN_PRINCIPLES.md` before declaring any change "done."

### Pre-change reasoning gate (mandatory)

Before writing or modifying **any** code, prompt, skill file, tool, or runtime change — pause and ask yourself:

> *"Does this change violate any of the 14 non-negotiables above, or anything in `SKILLS_DESIGN_PRINCIPLES.md` Part I, Part II, or Part III?"*

Run the change against this short check:

- Am I about to put procedure, string-formatting, validation logic, or numbered steps into a `SKILL.md`? → **Stop.** That belongs in a tool. Redesign the tool, then write a thin `SKILL.md` paragraph that describes the *judgment*, not the steps.
- Am I about to put `instructions:`, `next_skill:`, `conversational:`, or any prose into a `skill.yaml`? → **Stop.** YAML is config only. Move prose to `SKILL.md`.
- Am I about to make a tool return a user-facing sentence (`"No agenda found for X. Run phase 3 first."`)? → **Stop.** Return structured JSON facts (`{"found": false, "customer": "X"}`) and let the model write the sentence.
- Am I about to add control-flow markers (`[STOP_CHAIN]`, `[AWAITING_CONFIRMATION]`) or parse model text for them in `agent_core`? → **Stop.** HITL is a thread-status flag, not a marker.
- Am I about to make `agent_core` decide *what* to do next (which tool, which skill, when to stop, retry, mutate a tool result)? → **Stop.** That's the model's job. `agent_core` only executes what the Responses API requested.
- Am I about to chain skills via `next_skill`, or pass state between phases via on-disk JSON? → **Stop.** Collapse into one skill; let `previous_response_id` carry state.
- Am I about to add a tool registry, decorator-based registration outside of `@mcp.tool(...)`, or a central skill manifest? → **Stop.** Skills are discovered by folder shape; tools are registered with FastMCP's `@mcp.tool` inside their server module. No other registration mechanism.
- Am I about to make a tool call another tool internally (within the same MCP server *or* across servers)? → **Stop.** Composition is the model's job. Split into two tools the model invokes in sequence.
- Am I about to register a stdio MCP server with the Responses API as a native `mcp` tool (`type="mcp", server_url=...`)? → **Stop.** Responses' native MCP tool only accepts remote HTTP URLs. Stdio servers are exposed as ordinary `type="function"` tools built from `tools/list`, and dispatched client-side through `MCPClientPool`.
- Am I about to spawn an MCP server subprocess per tool call, or bypass the `MCPClientPool` to launch one ad hoc? → **Stop.** One subprocess per server name, lifted by the pool, multiplexed across all calls.
- Am I about to pass an Entra token as an MCP tool argument, or marshal credentials over the MCP transport? → **Stop.** MCP servers call `get_credential()` themselves; the shared on-disk MSAL cache makes silent refresh work across processes.
- Am I about to construct a credential inside a tool, call `DefaultAzureCredential`, or shell out to `az`? → **Stop.** Use `get_credential()`. Auth is shared, single-Entra, WAM-broker-first.
- Am I about to maintain conversation history outside `previous_response_id` (a `messages` list, a per-skill scratchpad, an inter-phase JSON file)? → **Stop.** The Responses API holds it. Don't duplicate.

If any answer is "yes," **change the approach before writing any code.** State the conflict explicitly, propose the compliant redesign, and only then implement. Do not ship a violation with a TODO to fix later.

### Things that must NOT be touched in any redesign

The following work today and are explicitly preserved. Do not refactor, replace, or "simplify" them unless the user asks:

- **Authentication and credential management** — `core/auth_credential.py` (WAM broker via `InteractiveBrowserBrokerCredential` parented to the pywebview HWND, with classic `InteractiveBrowserCredential` fallback), the shared-credential pattern via `set_credential()` / `get_credential()`, the `AuthenticationRecord` persistence for silent token refresh, the per-cache MSAL token blob naming (`hub_cowork`), and the Settings-UI Sign-In flow. This is battle-tested and works correctly across Entra scopes for Graph, Azure OpenAI, FoundryIQ, Fabric, and ACS. (§16, §9b, §18.4.)
- **Per-conversation concurrency model** — `core/thread_manager.py` (registry + `current_thread_id` ContextVar), `core/thread_executor.py` (`ExecutorPool`, one daemon worker per active thread, idle shutdown), `core/conversation_thread.py` (the dataclass with `previous_response_id`, status, `progress_log`, `code_log`, `hitl_correlation_tag`, `source`), and `core/thread_store.py` (`LocalJsonThreadStore` with debounced atomic writes under `~/.hub-cowork/threads/`). Multiple chat threads run independently and in parallel, each with its own Responses-API context.
- **Desktop host and UI integration** — `host/desktop_host.py` (WebSocket on 18080, HTTP on 18081, pywebview window, tray wire-up), `assets/chat_ui.{html,js,css}`, the full WebSocket protocol (`create_thread`, `send_to_thread`, `cancel_thread`, `system_query`, `thread_progress`, `thread_completed`, `service_status`, `auth_status`, …), and the request-id correlation scheme. Changes are **server-side only.**
- **Teams / Redis remote-message bridge** — `host/redis_bridge.py` with the per-Teams-user in-flight gate, `classify_inbox` 3-way classifier, and `#thread-xxxx` correlation tags. Orthogonal to skills.
- **Per-skill model-tier routing** (`reasoning` vs `fast`) — a deliberate strength over Cowork's single-model approach (§18.3); keep it.
- **Settings UI + env override mechanism** — `_env_overrides` in `~/.hub-cowork/hub_config.json`, applied in `__main__.py` before `agent_core` import; `restart` WS command relaunches the process.

**Where the MCP layer plugs in:** tool execution dispatches via `mcp_pool.call(server, name, args, on_progress=...)` inside the agent loop. `ExecutorPool`, `ThreadManager`, the WebSocket protocol, and the chat UI sit below the loop and never see how a tool is dispatched. The `MCPClientPool` is host-scoped (not thread-scoped) — one warm subprocess per server, multiplexed across every conversation thread. (§9b contract.)

## Architecture

Hub Cowork is a **single-process, multi-threaded Windows desktop agent** (Python 3.12+). It combines a WebSocket server, pywebview UI, Win32 system tray, a **per-conversation executor pool**, and an optional Azure Managed Redis bridge for Teams-based remote messaging.

All code lives under `src/hub_cowork/` and is packaged/installed as the `hub-cowork` distribution. The entry point is `python -m hub_cowork` (→ `src/hub_cowork/__main__.py` → `host.desktop_host.main`).

| Component | Module | Role |
|---|---|---|
| Agent core | `core/agent_core.py` | LLM router, inbox classifier, skill loader, tool loader, Azure OpenAI Responses API client, thread-scoped `run_agent_on_thread` |
| Credential factory | `core/auth_credential.py` | Builds the shared Entra credential — prefers WAM (`InteractiveBrowserBrokerCredential` from `azure-identity-broker`, parented to the pywebview HWND via `set_parent_window_handle`); falls back to classic `InteractiveBrowserCredential` when the broker / pymsalruntime is unavailable |
| Conversation state | `core/conversation_thread.py` | `ConversationThread` dataclass — id, status, messages, progress_log, code_log, `previous_response_id`, `active_session`, `hitl_correlation_tag`, `source`, `external_user` |
| Thread registry | `core/thread_manager.py` | Thread-safe singleton; observer pattern; exports `current_thread_id` ContextVar and `SYSTEM_THREAD_ID` constant |
| Executor pool | `core/thread_executor.py` | One daemon thread per active conversation (`_ThreadWorker`); idle-shutdown; sets `current_thread_id` so logs get tagged; invokes `on_thread_reply` for Redis outbox |
| Persistence | `core/thread_store.py` | `LocalJsonThreadStore` with debounced atomic writes under `~/.hub-cowork/threads/{active,archive}/`; `ThreadArchiveStore` Protocol reserved for future Cosmos DB backend |
| Hub config | `core/hub_config.py` | Merges shipped defaults (`assets/hub_config.default.json`) with user overrides (`~/.hub-cowork/hub_config.json`); also stores `_env_overrides` for the Settings UI env editor |
| Service status | `core/service_status.py` | Tracks reachability of `workiq`, `foundryiq`, `fabric_agent`, `redis_teams`; updated passively from `_tool_result` envelopes and actively by the Redis bridge; broadcast to UI as `service_status` events |
| App paths | `core/app_paths.py` | Central app-home + branding constants (`~/.hub-cowork/`, `"Hub Cowork"`) |
| Computer-Use harness | `core/computer_use.py` | Generic Azure OpenAI gpt-5.4 + Playwright Chromium loop. Use-case-agnostic: skills supply natural-language `instructions`, `start_url`, and `allow_domains`; harness owns the screenshot/action loop, key-mapping table, domain allow-list policing, and safety-check handling. Sync `run_computer_use_task(...)` wraps `asyncio.run` so tool handlers can call it directly. |
| Email/calendar | `core/outlook_helper.py` | ACS email + `.ics` invite builder |
| Desktop host | `host/desktop_host.py` | WebSocket server (18080), HTTP server (18081), pywebview three-pane UI, tray wire-up, wires `ThreadManager` observers + `ExecutorPool` + optional Redis bridge |
| Console host | `host/console.py` | Terminal REPL — no UI, no Redis bridge (exposed as `hub-cowork-console` script) |
| Remote bridge | `host/redis_bridge.py` | Azure Managed Redis inbox poller, 3-way classifier, **per-Teams-user in-flight gate**, outbox writer with `in_reply_to` + `#thread-xxxx` correlation, presence key with TTL heartbeat |
| Settings UI actions | `host/ui_actions.py` | Ad-hoc server-side actions triggered by the Settings modal (e.g. `validate_speakers`) — runs in a worker thread, broadcasts progress over the WebSocket |
| Tray icon | `host/tray_icon.py` | Raw Win32 ctypes tray with its own message-pump thread |
| Shared tools | `tools/*.py` | `query_workiq`, `log_progress`, `get_task_status`, `get_hub_config`, `create_word_doc`, `resolve_speakers`, `send_email` |
| Skill-local tools | `skills/<group>/tools/*.py` | Tools only available to one skill group (`create_meeting_invites`, `shelf_watch_run`, RFP tools) |
| Skills | `skills/**/*.yaml` | Declarative agents (`qa`, `task_status`, `agenda_repurpose`, `meeting_invites`, `rfp_evaluation`, `shelf_watch`, `engagement_agenda`) |
| Assets | `assets/` | `.env.defaults`, `chat_ui.html`, `hub_config.default.json`, icons — all shipped inside the wheel |

See [README.md](../README.md) for the full architecture diagram, skills, and protocol reference.

## Build and Run

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e .                              # editable install — src/ changes take effect immediately
cp .env.example .env                          # fill in values

# Debug (console window)
python -m hub_cowork

# Production (headless — no console)
.\scripts\start.ps1

# Stop / restart
.\scripts\stop.ps1
.\scripts\restart.ps1

# Console REPL (no UI)
hub-cowork-console                            # or: python -m hub_cowork.host.console
```

Required env vars: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_CHAT_MODEL`, `AZURE_OPENAI_CHAT_MODEL_SMALL`, `AZURE_OPENAI_API_VERSION`, `AZURE_TENANT_ID`, `ACS_ENDPOINT`, `ACS_SENDER_ADDRESS`.

Optional for remote/Teams: `AZ_REDIS_CACHE_ENDPOINT`, `REDIS_NAMESPACE` (default `hub-cowork`), `REDIS_SESSION_TTL_SECONDS`.

Env precedence (highest first): Settings UI `_env_overrides` in `~/.hub-cowork/hub_config.json` → user `.env` in CWD → packaged `src/hub_cowork/assets/.env.defaults`.

## Code Style

- Python 3.12+ type hints (`str | None`, `dict[str, str]`).
- Module-level private globals prefixed with `_`.
- Logging via `logging.getLogger("hub_se_agent")`.
- No linter/formatter configured — keep consistent with surrounding code.
- Prefer `replace_string_in_file` (one site at a time) for edits that touch long triple-quoted prompt constants. Batch edits (`multi_replace_string_in_file`) across large prompt blocks have historically corrupted those constants.

## Adding Skills and Tools

Follow [`SKILLS_DESIGN_PRINCIPLES.md`](../docs/architecture/SKILLS_DESIGN_PRINCIPLES.md) Part II §11 (MCP tool packaging), §11A (shared servers), and §12 (skill loading). The recipe below is the practical short version.

**New tool** (§11) — add it to an MCP server, never as a loose Python module:

- **Per-skill tool** (used by one skill): create `src/hub_cowork/skills/<skill>/mcp_server/tools/<name>.py` and register it in that server's `server.py` with `@mcp.tool(name=..., description=...)`. The description is the model's only hint about *when* to call this tool — write it MCP-style (intent verbs, trigger context).
- **Shared tool** (used by ≥2 skills, or wraps a foundational service like WorkIQ / Graph / ACS): add it under `src/hub_cowork/mcp_servers/<server>/tools/<name>.py`. Don't preemptively share — promote a tool to a shared server only when a second skill actually needs it.
- Tool functions return **JSON-serializable structured facts** with a `status`/`found` discriminator. Never user-facing prose. Never call other tools (within or across servers). Never construct credentials — call `get_credential()` from inside the tool function. Progress flows through MCP notifications, which the pool forwards to `on_progress`.
- No top-level side effects at module import; no spawn-per-call. The host's `MCPClientPool` owns subprocess lifecycle.

**New skill** (§12) — create a folder `src/hub_cowork/skills/<name>/` containing:

- `skill.yaml` — runtime config only. Fields: `name`, `description` (router contract — pack with intent verbs and trigger phrases), `mcp_servers` (list of shared server names plus `.` for the skill's own `mcp_server/`), `tool_allowlist` (optional subset filter), `model_tier` (`"reasoning"` | `"fast"`), `reasoning_effort` (when `reasoning`), `queued` (bool). **No `instructions`, no `next_skill`, no `conversational`, no loose `tools:` list.**
- `SKILL.md` — the system prompt as Markdown. Domain expertise and high-level workflow. No numbered runbooks, no control-flow markers, no procedural recipes.
- Optional `mcp_server/` subfolder for skill-local tools (auto-spawned by the pool when listed in `skill.yaml`).

Skills are auto-discovered recursively from folder shape — a folder with both `skill.yaml` and `SKILL.md` is a skill. The router prompt is rebuilt from each skill's `description`. Greetings/small talk are classified as `"none"` and answered by the router directly.

### Routing and the agent loop

Routing is one cheap fast-model call that returns a skill name or `"none"`. The router does not see tool definitions. (§13)

The agent loop in `agent_core` is the canonical Responses-API tool-execution bridge — `while response.status == "requires_action": execute tools → submit_tool_outputs`. Tool execution dispatches into the `MCPClientPool`, which routes the call to the right warm subprocess. No marker parsing, no chaining, no result mutation. (§14)

### Human-in-the-loop

Don't add control-flow markers. Write `SKILL.md` so the model naturally asks the user a question when it needs confirmation. The runtime treats any final text response as either a completion or an HITL pause based on thread context — no marker parsing. (§14.4)

### Multi-step workflows

Build them as a single skill with smarter tools. Phase-to-phase context flows via the Responses API's `previous_response_id` — no on-disk JSON handoff, no `thread.messages`. If you find yourself wanting `next_skill`, the right answer is "split a tool further" or "let the model orchestrate from richer instructions." (§5, §15)

### Future: moving an MCP server to Azure Container Apps

The MCP protocol is transport-agnostic. To move a server from local stdio to ACA-hosted streamable HTTP, change one entry in `skill.yaml` (`transport: streamable_http`, `url: ...`) and deploy the same server module behind a container. Skill code, tool code, and the agent loop are unchanged. (§11.7)

## Conventions

- **OpenAI Responses API** (not Chat Completions). Tool-call loop uses `previous_response_id`, **stored per `ConversationThread`**, so every thread has an independent LLM context. The token-refresh path on the OpenAI client is guarded by `_responses_client_lock`.
- **Single shared credential** — built by `core/auth_credential.py` (WAM broker preferred, classic `InteractiveBrowserCredential` fallback) and shared via `set_credential()` / `get_credential()` in `core/agent_core.py`. Used by OpenAI, WorkIQ helpers, ACS, and the Redis bridge (wrapped in `redis-entraid`'s `EntraIdCredentialsProvider`). No `DefaultAzureCredential` chain; no `az` CLI subprocesses under `pythonw.exe`. The desktop host calls `set_parent_window_handle(hwnd)` once pywebview creates its native window so the WAM account picker is parented to our UI.
- **Per-conversation state** — `ConversationThread` replaces any historical `_conversation_histories` dict. Threads are created by `ThreadManager.create(...)`, dispatched via `ExecutorPool.submit(thread_id, text)`, and persisted by `LocalJsonThreadStore`. A SYSTEM pseudo-thread (ID `"system"`) handles cross-task queries — it is never persisted as a real thread.
- **WebSocket protocol** — JSON with `type` field.
    - Client → server: `create_thread`, `send_to_thread`, `cancel_thread`, `list_threads`, `get_thread`, `archive_thread`, `unarchive_thread`, `list_archived_threads`, `delete_thread`, `system_query`, `signin`, `clear_history`, `get_logs`, `get_config`, `save_config`, `validate_speakers`, `restart`.
    - Server → client: `threads_list`, `thread_created`, `thread_updated`, `thread_detail`, `thread_started`, `thread_progress`, `thread_completed`, `thread_error`, `thread_archived`, `thread_unarchived`, `thread_deleted`, `cancel_ack`, `log_entry`, `log_history`, `system_query_started`, `system_query_progress`, `system_query_complete`, `system_query_error`, `auth_status`, `skills_list`, `config_warning`, `service_status`, `validate_speakers_*`, `remote_message`, `error`.
- **Request IDs** — every invocation gets `uuid.uuid4().hex[:8]`, used across WebSocket, UI, Redis outbox `in_reply_to`, and log correlation.
- **Progress callbacks** — `on_progress(kind, message)` flows `ExecutorPool` → `agent_core.run_agent_on_thread` → tools. Each call is also appended to the thread's `progress_log`. Tools should emit the **full** message (no truncation) — the UI handles display-side shortening and uses the full text for tooltips.
- **Log tagging** — the WebSocket log handler reads `thread_manager.current_thread_id` (a `ContextVar`) set by `_ThreadWorker`. Records are routed to per-thread `code_log` and broadcast with `thread_id`. Entries without thread context go to the `system` bucket.
- **Redis namespace** — every Redis key is prefixed with `REDIS_NAMESPACE` (default `hub-cowork`) so this fork cannot collide with other deployments. Streams: `{ns}:inbox:{email}`, `{ns}:outbox:{email}`; presence: `{ns}:agents:{email}`. The Teams relay container (`workiq-agent-remote-client`) honors the same env var.
- **Inbox routing** — `classify_inbox(text, active_summaries)` in `agent_core` returns `{kind: "new"|"existing"|"system", thread_id?}`. The classifier gets each thread's `last_user_excerpt` (120 chars) + `last_agent_excerpt` (240 chars). Strong `existing` signals include fact-listing replies to multi-field questions, numbered options, yes/no confirmations, and "exactly one thread is awaiting_user" as a tie-breaker. The bridge honors a relay-supplied `thread_id` hint (from the `#thread-xxxx` tag) as a fast path for HITL replies without an LLM call.
- **Per-Teams-user gate** (`host/redis_bridge.py`) — for `new` classifications only, check if the same external user already has an in-flight thread (`running` or `awaiting_user`, `source == "remote"`). If so, reject with an outbox message tagged to the blocker thread's correlation. `existing` and `system` classifications bypass the gate entirely.
- **HITL correlation tags** — every outbound Teams reply is prefixed with the thread's `hitl_correlation_tag` (`#thread-ab12cd`). Users/relays keep the tag to route follow-ups deterministically.

## Pitfalls

- Azure auth must complete (user clicks **Sign In**) before any LLM or tool calls work.
- `query_workiq` shells out to the `workiq` CLI binary — must be on `PATH` or set `WORKIQ_PATH`.
- Windows-specific stack: `pythonw.exe`, `winotify`, Win32 ctypes tray. Mac support exists but is untested.
- `scripts\stop.ps1` matches `pythonw` processes whose command line contains `-m hub_cowork` — safe by construction, but confirm the match before killing if there are multiple installs.
- Ports **18080** (WebSocket) and **18081** (HTTP) are hardcoded.
- `multi_replace_string_in_file` edits over long triple-quoted prompt constants (e.g., `_INBOX_CLASSIFIER_PROMPT`) have historically produced duplicated/misaligned text. Prefer one-edit-at-a-time `replace_string_in_file` on those blocks.
- No automated tests — verification is manual via the UI, the Teams bot, or `test-client/chat.py`.
