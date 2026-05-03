# Project Guidelines

## Architecture

Hub Cowork is a **single-process, multi-threaded Windows desktop agent** (Python 3.12+). It combines a WebSocket server, pywebview UI, Win32 system tray, a **per-conversation executor pool**, and an optional Azure Managed Redis bridge for Teams-based remote messaging.

All code lives under `src/hub_cowork/` and is packaged/installed as the `hub-cowork` distribution. The entry point is `python -m hub_cowork` (→ `src/hub_cowork/__main__.py` → `host.desktop_host.main`).

| Component | Module | Role |
|---|---|---|
| Agent core | `core/agent_core.py` | LLM router, inbox classifier, skill loader, tool loader, Azure OpenAI Responses API client, thread-scoped `run_agent_on_thread` / `run_skill_on_thread` |
| Credential factory | `core/auth_credential.py` | Builds the shared Entra credential — prefers WAM (`InteractiveBrowserBrokerCredential` from `azure-identity-broker`, parented to the pywebview HWND via `set_parent_window_handle`); falls back to classic `InteractiveBrowserCredential` when the broker / pymsalruntime is unavailable |
| Conversation state | `core/conversation_thread.py` | `ConversationThread` dataclass — id, status, messages, progress_log, code_log, `previous_response_id`, `active_session`, `hitl_correlation_tag`, `source`, `external_user` |
| Thread registry | `core/thread_manager.py` | Thread-safe singleton; observer pattern; exports `current_thread_id` ContextVar and `SYSTEM_THREAD_ID` constant |
| Executor pool | `core/thread_executor.py` | One daemon thread per active conversation (`_ThreadWorker`); idle-shutdown; sets `current_thread_id` so logs get tagged; invokes `on_thread_reply` for Redis outbox |
| Persistence | `core/thread_store.py` | `LocalJsonThreadStore` with debounced atomic writes under `~/.hub-cowork/threads/{active,archive}/`; `ThreadArchiveStore` Protocol reserved for future Cosmos DB backend |
| Hub config | `core/hub_config.py` | Merges shipped defaults (`assets/hub_config.default.json`) with user overrides (`~/.hub-cowork/hub_config.json`); also stores `_env_overrides` for the Settings UI env editor |
| Service status | `core/service_status.py` | Tracks reachability of `workiq`, `foundryiq`, `fabric_agent`, `redis_teams`; updated passively from `_tool_result` envelopes and actively by the Redis bridge; broadcast to UI as `service_status` events |
| App paths | `core/app_paths.py` | Central app-home + branding constants (`~/.hub-cowork/`, `"Hub Cowork"`) |
| Email/calendar | `core/outlook_helper.py` | ACS email + `.ics` invite builder |
| Desktop host | `host/desktop_host.py` | WebSocket server (18080), HTTP server (18081), pywebview three-pane UI, tray wire-up, wires `ThreadManager` observers + `ExecutorPool` + optional Redis bridge |
| Console host | `host/console.py` | Terminal REPL — no UI, no Redis bridge (exposed as `hub-cowork-console` script) |
| Remote bridge | `host/redis_bridge.py` | Azure Managed Redis inbox poller, 3-way classifier, **per-Teams-user in-flight gate**, outbox writer with `in_reply_to` + `#thread-xxxx` correlation, presence key with TTL heartbeat |
| Settings UI actions | `host/ui_actions.py` | Ad-hoc server-side actions triggered by the Settings modal (e.g. `validate_speakers`) — runs in a worker thread, broadcasts progress over the WebSocket |
| Tray icon | `host/tray_icon.py` | Raw Win32 ctypes tray with its own message-pump thread |
| Shared tools | `tools/*.py` | `query_workiq`, `log_progress`, `get_task_status`, `get_hub_config`, `create_word_doc`, `resolve_speakers`, `send_email` |
| Skill-local tools | `skills/<group>/tools/*.py` | Tools only available to one skill group (`engagement_context`, `create_meeting_invites`, RFP tools) |
| Skills | `skills/**/*.yaml` | Declarative agents (`qa`, `task_status`, `agenda_repurpose`, `meeting_invites`, `rfp_evaluation`, and the 4-phase `hub_agenda_creation` chain) |
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

**New tool** — create `src/hub_cowork/tools/<name>.py` (shared) or `src/hub_cowork/skills/<group>/tools/<name>.py` (skill-local) exporting:

- `SCHEMA: dict` — OpenAI function JSON schema with `name`, `description`, `parameters`
- `handle(arguments: dict, *, on_progress=None, workiq_cli=None, **kwargs) -> str`

Files starting with `_` are skipped. Restart to pick up a new tool file.

**New skill** — create `src/hub_cowork/skills/<name>.yaml` (standalone) or `src/hub_cowork/skills/<group>/<name>.yaml` (grouped chain) with fields: `name`, `description`, `model` (`"full"` | `"mini"`), `conversational` (bool), `queued` (bool), `tools` (list of tool names), `instructions` (str). Optional: `next_skill` (str).

Mark chained internal skills with `[INTERNAL` at the start of `description` to exclude them from the router (reachable only via `next_skill`).

Skills are auto-discovered recursively from `skills/**/*.yaml`. The router prompt is rebuilt automatically from all non-internal descriptions. Greetings/small talk are handled directly by the router (classified as `"none"`) without invoking any skill.

YAML-only edits (instructions, etc.) are picked up without restart. New files require a restart.

### Conversational skills

Set `conversational: true` when the skill needs multi-turn context (follow-up Q&A, HITL). Per-thread conversation history is stored on the `ConversationThread` itself (`thread.messages`), bounded and reused across turns. Non-conversational skills are stateless per invocation.

### Human-in-the-loop confirmation

1. Set `conversational: true` — needed for turn detection via message history.
2. Structure instructions as multi-turn: Turn 1 presents candidates and emits `[AWAITING_CONFIRMATION]`; Turn 2+ handles confirmation, corrections, or re-asks.
3. On `[AWAITING_CONFIRMATION]`, `agent_core` sets `thread.active_session` (keyed by skill name), strips the marker, parks the thread at `status = "awaiting_user"`, and returns the text to the user without chaining.
4. The user's next message to the same thread resumes the skill. Teams replies use the `#thread-xxxx` correlation tag (extracted by the relay as a `thread_id` hint) so routing is deterministic.
5. Normal completion (no markers) clears `active_session` and chains to `next_skill` if configured.
6. Reference implementation: `skills/hub_agenda_creation/engagement_briefing.yaml`.

### Skill chaining

Set `next_skill: <skill_name>` to auto-chain on normal completion. Control flow markers:

- `[STOP_CHAIN]` — halt chain on errors, clear `active_session`.
- `[AWAITING_CONFIRMATION]` — pause for user input, do NOT chain.

Inter-phase context (used by the 4-phase agenda chain) is passed through the `engagement_context` tool, which reads/writes JSON under `~/.hub-cowork/engagement_context/<customer>.json`.

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
