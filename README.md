# Hub Cowork

**An always-on Windows desktop AI agent for Microsoft 365 engagement workflows.**

Hub Cowork runs quietly on a Hub Solution Engineer's laptop, orchestrating multi-step workflows against Microsoft 365 (calendars, email, SharePoint, contacts, OneDrive) via WorkIQ and Azure OpenAI. It is reachable three ways:

1. **Locally** — pywebview chat window summoned from the Windows system tray.
2. **Remotely from Microsoft Teams** — a companion cloud relay (see [workiq-agent-remote-client](https://github.com/sansri/workiq-agent-remote-client)) bridges Teams messages into the agent via Azure Managed Redis.
3. **Programmatically** — any client that can read/write the agent's Redis streams (a console test client is included).

Hub Cowork exhibits the design traits of emerging local‑agent platforms (Claude CoWork, OpenClaw): **always‑on local execution, skills‑driven autonomy, and remote reachability** — applied to Microsoft 365 workflows that remain painful to do manually (resolving speakers, cross‑referencing briefing notes with calendars, drafting agendas, sending batched invites, analysing RFPs).

---

## Functional Features

| Feature | Description |
|---|---|
| **Autonomous agentic execution** | State your intent in plain language. The agent orchestrates multi-step workflows end-to-end — deciding what data to fetch, what actions to take, and how to present the outcome — without further human input. |
| **Remote access via Microsoft Teams** | Send and receive messages from your phone through Teams. The agent processes work locally on your machine and delivers the result back through Azure Managed Redis. |
| **Multi-thread conversation model** | Every request is its own `ConversationThread` with an independent LLM context, executor, progress stream, and UI pane. Local and remote threads run in parallel — no cross-talk, no head-of-line blocking. |
| **Thread persistence & archive** | Threads are persisted to `~/.hub-cowork/threads/active/` and can be archived, unarchived, or deleted from the UI. Survives app restarts. |
| **Per-user Teams serialization** | At most one remote Teams task per user is "in flight" at a time. Follow-up replies to awaiting threads always go through; brand-new tasks that would stack are rejected politely with a pointer to the blocker thread. |
| **Three-way inbox classifier** | Incoming Teams messages are LLM-classified as `new` (start a thread), `existing` (continue a running thread, with `#thread-xxxx` tag as fast-path), or `system` (instant non-queued reply). |
| **Human-in-the-loop confirmation** | Skills can pause mid-flow with `[AWAITING_CONFIRMATION]`. The thread parks at status `awaiting_user`, persists state, and resumes on the user's next message (local click or Teams reply). |
| **Real-time status** | Ask "what's the status of my request?" any time — a non-queued system skill reports progress milestones without interrupting running work. |
| **Skills-driven extensibility** | Each capability is a declarative YAML file. Add a new skill by dropping a YAML file into `src/hub_cowork/skills/` — no code changes. |
| **Settings UI with env editor** | Gear icon in the chat header opens a modal to edit hub config (speakers, agenda folder) and environment variables (endpoints, model names, Redis), then restart. |
| **Background operation** | Runs invisibly via `pythonw.exe` — no console window, no taskbar clutter until you summon it. |
| **System tray icon** | Pure Win32 (zero extra deps). Left-click to show/hide, right-click for context menu. |
| **Toast notifications** | Native Windows toasts on task start/complete. Click a toast to open the UI. |
| **Persistent authentication** | Sign in once; `InteractiveBrowserCredential` with persistent token cache refreshes silently across restarts. |
| **Auto-start at Windows login** | Install script registers the assistant to launch at startup. |

---

## Key Technical Capabilities

| Capability | Implementation |
|---|---|
| **Azure OpenAI Responses API** | The agentic core. Tool definitions + natural-language instructions drive autonomous tool-call orchestration. `previous_response_id` is **stored per ConversationThread** so each thread has an independent LLM context. |
| **Per-conversation executor pool** | `ExecutorPool` spawns one daemon thread per active conversation (`_ThreadWorker`); idle workers self-shut down. Each worker sets a `current_thread_id` ContextVar so logs get tagged correctly. |
| **Azure Managed Redis (cluster mode)** | Inbox/outbox streams keyed by user email. Passwordless Entra ID via `redis-entraid` credential provider with automatic token refresh. |
| **Namespaced Redis keys** | Every key is prefixed with `REDIS_NAMESPACE` (default `hub-cowork`) so this fork cannot collide with deployments sharing the same Redis instance. |
| **Composable tool system** | Tools are self-contained Python modules discovered at startup via `importlib`. Shared tools live in `src/hub_cowork/tools/`; skill-local tools live beside the skill under `skills/<group>/tools/`. |
| **Composable skill system** | Skills are YAML files discovered recursively from `src/hub_cowork/skills/**/*.yaml`. The router prompt is auto-generated from skill descriptions. Internal chained skills are excluded from routing. |
| **Shared credential architecture** | A single `InteractiveBrowserCredential` is shared across OpenAI, WorkIQ, ACS, and Redis — one sign-in, zero `az` CLI subprocesses on Windows. |

---

## The Two-Part Architecture

![Solution Architecture](docs/architecture.png)

**Part 1** (this repo) is the agent itself — running on a Windows 11 laptop, processing tasks locally with full access to the user's Microsoft 365 data via WorkIQ. It registers its presence in Azure Managed Redis and polls an inbox stream for remote requests.

**Part 2** ([workiq-agent-remote-client](https://github.com/sansri/workiq-agent-remote-client)) is an Azure Container App that bridges Microsoft Teams to the Redis streams. It extracts `#thread-xxxx` correlation tags from Teams replies and passes them as a fast-path hint so the agent knows which thread the message continues. The container app respects the `REDIS_NAMESPACE` env var so the same relay can point at different agent deployments.

The user experience: send a message from your phone in Teams → the agent on your laptop picks it up, runs the full agentic workflow (retrieving M365 data, calling tools, orchestrating multi-step actions) → the result appears in your Teams chat.

> **Note:** `docs/architecture.png` is currently a legacy image from the single-queue era. See the [Architecture](#architecture) section below for the accurate current diagram; the image should be regenerated from that diagram at a later pass.

---

## A Heterogeneous Agentic Solution

Hub Cowork bridges two pillars of the Microsoft AI stack:

- **Microsoft 365 Copilot & WorkIQ** — the productivity platform that surfaces enterprise knowledge from calendars, emails, documents, contacts, and SharePoint.
- **Azure AI Foundry with Azure OpenAI Responses API** — the code-first agentic platform for autonomous, tool-calling agents built from tool definitions and natural-language instructions.

WorkIQ provides the **data and enterprise context**. Azure OpenAI provides the **autonomous reasoning and orchestration**. The RFP evaluation skill additionally consults **FoundryIQ** (Azure AI Search knowledge store) and a **Fabric Data Agent** in a cross-tenant resource subscription.

---

## Built-in Skills

Hub Cowork is **skills-driven** — each capability is a declarative YAML file rather than hardcoded logic.

| Skill | Model | Queued | What it does |
|---|---|---|---|
| **Meeting Invites** (`meeting_invites`) | full | yes | Autonomous workflow: retrieve agenda → filter speakers → resolve emails → send calendar invites via ACS |
| **Engagement Briefing** (`engagement_briefing`) | full | yes | Phase 1 of the agenda pipeline: locate briefing calls, **confirm selection with user** (HITL), retrieve notes, extract metadata. Auto-chains → Goals |
| **Engagement Goals** (`engagement_goals`) | full | yes | Phase 2: extract and segment customer goals from briefing notes. Auto-chains → Agenda Build |
| **Engagement Agenda Build** (`engagement_agenda_build`) | full | yes | Phase 3: build a detailed agenda markdown table with time slots, speakers, descriptions. Auto-chains → Publish |
| **Engagement Agenda Publish** (`engagement_agenda_publish`) | full | yes | Phase 4: create a Word document from the agenda and save to the configured output folder (OneDrive-synced) |
| **Agenda Repurpose** (`agenda_repurpose`) | full | yes | Conversational: retrieve an existing agenda, collect new customer details (name, date, venue), produce a repurposed Word document |
| **RFP Evaluation** (`rfp_evaluation`) | full | yes | Retrieve an RFP via WorkIQ, consult FoundryIQ + Fabric Data Agent, synthesise a Bid Intelligence Brief, save to OneDrive, share with the team |
| **Q&A** (`qa`) | mini | yes | Conversational Q&A about M365 data with per-thread history |
| **Task Status** (`task_status`) | mini | no | Report current thread progress and active-thread count — responds instantly even while a task is running |
| *(Router direct)* | mini | no | Greetings and small talk — handled by the router (`"none"` classification) without invoking a skill |

**Queued** refers to per-thread serialization: a queued skill runs on its conversation's own executor thread (so it doesn't block other conversations); a non-queued skill runs immediately on the SYSTEM pseudo-thread.

### Engagement Agenda Workflow — Autonomous 4-Phase Skill Chain

```
  User: "create an agenda for Contoso"
    │
    ▼
  Phase 1: engagement_briefing  (conversational, HITL)
    │  Turn 1: Find briefing calls → present → [AWAITING_CONFIRMATION]
    │          ↳ thread status → awaiting_user, executor idles, state persisted
    │  Turn 2+: User confirms/corrects  (can arrive locally OR via Teams reply)
    │           Retrieve notes → extract metadata
    │  next_skill: engagement_goals
    ▼
  Phase 2: engagement_goals
    │  Extract & segment customer goals from notes
    │  next_skill: engagement_agenda_build
    ▼
  Phase 3: engagement_agenda_build
    │  Load goals + hub config → build agenda table
    │  Map goals to sessions, assign speakers, compute time slots
    │  next_skill: engagement_agenda_publish
    ▼
  Phase 4: engagement_agenda_publish
    │  Create Word doc via python-docx, save to agenda_output_folder
    ▼
  Complete agenda displayed in UI + .docx on disk
```

**Skill chaining** — Driven by the `next_skill` field in each YAML. On normal completion, `agent_core` invokes the next phase with the completion text as input. Control flow markers in the final text alter this:

| Marker | Effect |
|---|---|
| *(none)* | Chain to `next_skill` if configured |
| `[STOP_CHAIN]` | Halt chaining, clear thread's active session, return text as-is (used to gate on errors — e.g., no briefing calls found) |
| `[AWAITING_CONFIRMATION]` | Pause for user input. Thread status → `awaiting_user`, marker stripped, no chaining. Next message to the same thread resumes the skill. |

**Active session lives on the ConversationThread** (`thread.active_session`), not a global — so multiple parallel threads can each be awaiting confirmation on different skills without interfering.

**Inter-phase context** — Passed via the `engagement_context` tool, which reads/writes JSON under `~/.hub-cowork/engagement_context/<customer>.json`. Each phase appends its output (metadata, goals, agenda) to the shared file.

**Hub configuration** — Phase 3 reads default session start time and speaker-by-topic mapping via `get_hub_config`. Users edit these in the ⚙ Settings UI.

**Engagement type detection** — Phase 1 classifies as `ADS`, `RAPID_PROTOTYPE`, `BUSINESS_ENVISIONING`, `SOLUTION_ENVISIONING`, `HACKATHON`, or `CONSULT`. Phase 3 applies type-specific agenda patterns.

---

## Classifier, Gate, and HITL Correlation

Three mechanisms keep multi-thread remote traffic predictable.

**1. Inbox classifier** (`agent_core.classify_inbox`)

Every inbound Teams message is classified as `new`, `existing`, or `system`. The classifier receives a summary of every active thread, including both `last_user_excerpt` (120 chars) and `last_agent_excerpt` (240 chars). Strong signals bias toward `existing` when:

- The user is replying to a question that listed multiple fields (e.g., "Customer is Texmaco, date 21 Apr, venue Teams virtual")
- The user is replying to numbered options or a yes/no confirmation
- Exactly one thread is `awaiting_user` (tie-breaker)

Fast-path: if the Teams relay supplies a `thread_id` hint (extracted from the `#thread-xxxx` tag the agent prefixes to every outbound reply), the classifier verdict for that thread is honored without an LLM call.

**2. Per-Teams-user gate** (`host/redis_bridge.py`)

For any inbound message classified as `new`, the bridge checks whether the same Teams user already has an in-flight thread (`running` or `awaiting_user`, `source=="remote"`). If so, the new thread is rejected with an outbox message tagged to the blocking thread's correlation, asking the user to finish or cancel the active task first. `existing` and `system` classifications bypass the gate entirely — so HITL replies and status checks are never blocked.

**3. `#thread-xxxx` correlation tags**

Every outbound Teams reply is prefixed with the thread's `hitl_correlation_tag` (e.g., `#thread-ab12cd`). Users can keep this tag in their Teams reply to deterministically route follow-ups to the same thread. The Teams relay strips the tag from the user-visible text and forwards it as a structured hint.

---

## Adding Skills and Tools

### New tool

Create `src/hub_cowork/tools/<name>.py` (shared) or `src/hub_cowork/skills/<group>/tools/<name>.py` (skill-local) exporting:

```python
SCHEMA: dict = {
    "type": "function",
    "name": "my_tool",
    "description": "...",
    "parameters": { ... },  # JSON Schema
}

def handle(arguments: dict, *, on_progress=None, workiq_cli=None, **kwargs) -> str:
    ...
```

Files starting with `_` are skipped. Restart to pick up a new tool.

### New skill

Create `src/hub_cowork/skills/<name>.yaml` (standalone) or `src/hub_cowork/skills/<group>/<name>.yaml` (grouped chain) with these fields:

| Field | Required | Type | Description |
|---|---|---|---|
| `name` | ✓ | `string` | Unique identifier — what the router emits when it classifies a request |
| `description` | ✓ | `string` | Natural-language description used by the router. Prefix with `[INTERNAL` to exclude from routing (chain-only) |
| `model` | ✓ | `"full"` \| `"mini"` | `full` → complex reasoning; `mini` → faster/cheaper |
| `conversational` | ✓ | `bool` | `true` → retains per-thread history; required for HITL |
| `queued` | ✓ | `bool` | `true` → runs on the conversation's executor thread; `false` → runs on SYSTEM pseudo-thread immediately |
| `tools` | ✓ | `list[string]` | Tool names this skill can call |
| `instructions` | ✓ | `string` | System prompt |
| `next_skill` | — | `string` | Name of skill to chain to on normal completion |

YAML-only edits (instructions, etc.) are picked up without restart. New files require a restart.

Greetings and small talk are handled directly by the router (classified as `"none"`) without invoking any skill.

---

## Hub Configuration & Settings UI

Hub-specific settings are stored as JSON and editable through the chat UI.

```
src/hub_cowork/assets/hub_config.default.json   ← Shipped defaults
          │
          │  hub_config.load() merges:
          │    defaults  ← hub_config.default.json (inside the package)
          │    overrides ← ~/.hub-cowork/hub_config.json (user edits)
          │
          ▼
       Merged config returned to caller
```

The ⚙ gear icon in the chat header opens a settings modal with two sections:

- **Hub settings** — hub name, default session start time, speakers by topic, agenda output folder, optional agenda template `.docx` path.
- **Environment variables** — any value from the app's environment (endpoints, model names, Redis, feature toggles). Saved values are written to the `_env_overrides` map in `~/.hub-cowork/hub_config.json` and applied on restart. Precedence (highest first): `_env_overrides` → user `.env` in the working dir → packaged `src/hub_cowork/assets/.env.defaults`.

After changing env values the UI offers a one-click restart.

---

## Architecture

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                              Windows 11 Desktop                               │
│                                                                               │
│  ┌──────────────────────┐     ┌───────────────────────────────────────────┐   │
│  │  pywebview window    │◄───►│   WebSocket server  (ws://127.0.0.1:18080)│   │
│  │  (chat_ui.html)      │     │   HTTP server       (http://127.0.0.1:18081) │
│  │                      │     │                                           │   │
│  │ • Thread list pane   │     │  ┌─────────────┐   ┌────────────────────┐ │   │
│  │ • Chat pane          │     │  │ Tool loader │   │  Skill loader      │ │   │
│  │ • Progress/Logs pane │     │  │ tools/*.py  │   │  skills/**/*.yaml  │ │   │
│  │ • Settings + env UI  │     │  └──────┬──────┘   └──────────┬─────────┘ │   │
│  │ • Auth banner        │     │         │                     │           │   │
│  └──────────────────────┘     │  ┌──────▼─────────────────────▼────────┐  │   │
│                               │  │       Router (master agent)         │  │   │
│  ┌────────────────────┐       │  │  — classifies local requests into   │  │   │
│  │ System tray icon   │       │  │    skill | "none" (greeting)         │  │   │
│  │ (Win32 ctypes)     │       │  └─────────────────┬───────────────────┘  │   │
│  └────────────────────┘       │                    │                      │   │
│                               │              ┌─────▼─────────┐            │   │
│  ┌─────────────────────┐      │              │ ThreadManager │            │   │
│  │ Toast notifications │      │              │   observers,  │            │   │
│  │ (winotify)          │      │              │  ContextVar   │            │   │
│  └─────────────────────┘      │              └─────┬─────────┘            │   │
│                               │                    │                      │   │
│                               │           ┌────────▼─────────┐            │   │
│                               │           │   ExecutorPool   │            │   │
│                               │           │  one _ThreadWorker per       │ │   │
│                               │           │  active conversation; idle-  │ │   │
│                               │           │  shutdown; tags logs via     │ │   │
│                               │           │  current_thread_id CV        │ │   │
│                               │           └────────┬─────────┘            │   │
│                               │                    │                      │   │
│                               │   ┌────────────────▼──────────────────┐   │   │
│                               │   │   Skill sub-agent execution       │   │   │
│                               │   │   Azure OpenAI Responses API      │   │   │
│                               │   │   — per-thread previous_response_id│  │   │
│                               │   │   — autonomous tool-call loop     │   │   │
│                               │   └─────────┬─────────┬───────────────┘   │   │
│                               │             │         │                   │   │
│                               │   ┌─────────▼─┐   ┌───▼──────────────┐    │   │
│                               │   │ Tool layer│   │ Progress stream  │    │   │
│                               │   │ ...       │   │ → UI (WS)        │    │   │
│                               │   │           │   │ → Toast          │    │   │
│                               │   │           │   │ → thread.progress│    │   │
│                               │   └─────────┬─┘   │ → Redis outbox   │    │   │
│                               │             │     └──────────────────┘    │   │
│                               │   ┌─────────▼─────────────────────────┐   │   │
│                               │   │    LocalJsonThreadStore           │   │   │
│                               │   │   ~/.hub-cowork/threads/          │   │   │
│                               │   │       active/   archive/          │   │   │
│                               │   └───────────────────────────────────┘   │   │
│                               └───────────────┬───────────────────────────┘   │
│                                               │                               │
│  ┌────────────────────────────────────────────▼─────────────────────────────┐ │
│  │                      Redis bridge (optional)                             │ │
│  │  • Polls {ns}:inbox:{email} via XREAD (blocking)                         │ │
│  │  • classify_inbox → new | existing | system                              │ │
│  │  • Per-user single-in-flight gate on "new"                               │ │
│  │  • Writes {ns}:outbox:{email} with in_reply_to + #thread-xxxx prefix     │ │
│  │  • {ns}:agents:{email} presence with TTL heartbeat                       │ │
│  │  • redis-entraid credential provider, shared InteractiveBrowserCredential│ │
│  └───────────────────────┬──────────────────────────────────────────────────┘ │
└──────────────────────────┼────────────────────────────────────────────────────┘
                           │
          ┌────────────────▼──────────────────┐
          │  Azure Managed Redis (cluster)    │
          │  streams keyed by user email      │
          └────────────────┬──────────────────┘
                           │
          ┌────────────────▼──────────────────┐
          │  Part 2: workiq-agent-remote-     │
          │          client (Teams relay)     │
          │  — REDIS_NAMESPACE-aware          │
          │  — extracts #thread-xxxx          │
          └───────────────────────────────────┘

          ┌───────────────────────────────────┐
          │  WorkIQ CLI → M365 Graph          │
          │  (Calendar / Email / Files /      │
          │   Contacts / SharePoint)          │
          └───────────────────────────────────┘

          ┌───────────────────────────────────┐
          │  Azure Communication Services     │
          │  (calendar invite email)          │
          └───────────────────────────────────┘

          ┌───────────────────────────────────┐
          │  RFP skill only (cross-tenant):   │
          │   FoundryIQ · Fabric Data Agent   │
          └───────────────────────────────────┘
```

### How it all fits together

1. **Single-process launcher** (`python -m hub_cowork` → `hub_cowork/__main__.py` → `host/meeting_agent.py::main`) — applies `_env_overrides` from the Settings UI, loads `.env` and packaged `.env.defaults`, starts the WebSocket/HTTP servers, system tray, optional Redis bridge, shows a startup toast, and enters the pywebview event loop.

2. **WebSocket server (port 18080)** — JSON protocol, typed messages for threads, progress, logs, config, and remote-message notifications. See [WebSocket protocol](#websocket-protocol) below.

3. **HTTP server (port 18081)** — Handles toast notification clicks: `GET /show` brings up the pywebview window.

4. **Three-pane pywebview UI** — Thread list (active + archived), chat, and a details/progress/logs pane per thread. Each thread has its own progress stream and code log.

5. **Tool loader** — Imports all shared `*.py` in `src/hub_cowork/tools/` and all skill-local `skills/*/tools/*.py` at startup.

6. **Skill loader** — Walks `src/hub_cowork/skills/**/*.yaml` and builds the router prompt from non-`[INTERNAL` descriptions.

7. **Router (master agent)** — Classifies local requests. `"none"` → answered directly; otherwise → skill selection.

8. **ThreadManager** — Thread-safe singleton. Stores `ConversationThread` objects in memory, exposes observer hooks, and owns the `current_thread_id` ContextVar and the `SYSTEM_THREAD_ID` constant.

9. **ExecutorPool** — One daemon `_ThreadWorker` per active conversation. Each worker sets `current_thread_id` before dispatching, calls `agent_core.run_agent_on_thread(...)`, emits progress, and calls `on_thread_reply` for Redis outbox delivery. Workers shut down after a configurable idle period.

10. **Skill sub-agents** — Azure OpenAI Responses API. Tool definitions + instructions drive the autonomous tool-call loop. `previous_response_id` is stored on the `ConversationThread` so every thread has its own LLM context.

11. **Tool execution layer** — `query_workiq`, `log_progress`, `get_task_status`, `get_hub_config`, `create_word_doc`, `resolve_speakers`, `send_email` are shared; `engagement_context` (agenda chain), `create_meeting_invites` (meeting invites), and `create_rfp_brief_doc` / `query_fabric_agent` / `search_foundryiq` / `share_onedrive_document` (RFP) are skill-local.

12. **LocalJsonThreadStore** — Debounced atomic JSON writes under `~/.hub-cowork/threads/{active,archive}/`. A `ThreadArchiveStore` Protocol is reserved for a future Cosmos DB backend.

13. **Redis bridge** (optional) — Inbox poller, 3-way classifier, per-user gate, outbox writer with `in_reply_to` + `#thread-xxxx` correlation, presence key with TTL heartbeat. Shares the agent's credential — no `DefaultAzureCredential` chain and no `az` CLI subprocesses under `pythonw.exe`.

---

## Project Structure

```
hub-cowork/
├── pyproject.toml               # Package definition, dependencies, console + gui scripts
├── requirements.txt             # Pin list for editable dev installs
├── README.md                    # You are here
├── .env.example                 # Starter environment file
├── favicon.svg
│
├── src/hub_cowork/
│   ├── __init__.py
│   ├── __main__.py              # `python -m hub_cowork` entry — applies env overrides
│   │
│   ├── core/                    # Pure logic, no I/O wiring
│   │   ├── agent_core.py            # Router, classifier, skill/tool loaders, run_agent_on_thread / run_skill_on_thread, shared credential
│   │   ├── conversation_thread.py   # ConversationThread dataclass (id, status, messages, progress_log, code_log, previous_response_id, active_session, hitl_correlation_tag, source, external_user)
│   │   ├── thread_manager.py        # Registry singleton, observer pattern, current_thread_id ContextVar, SYSTEM_THREAD_ID
│   │   ├── thread_executor.py       # ExecutorPool + _ThreadWorker (per-thread daemon, idle shutdown, thread_id tagging, on_thread_reply)
│   │   ├── thread_store.py          # LocalJsonThreadStore (debounced atomic writes); ThreadArchiveStore Protocol
│   │   ├── hub_config.py            # Config loader — merges shipped defaults with ~/.hub-cowork/hub_config.json
│   │   ├── app_paths.py             # Central app-home + branding constants ("Hub Cowork", ~/.hub-cowork/)
│   │   └── outlook_helper.py        # ACS email + .ics invite builder
│   │
│   ├── host/                    # Runtime hosts (UI, console, remote bridge, tray)
│   │   ├── meeting_agent.py         # WS+HTTP servers, pywebview UI, tray wire-up, ExecutorPool + Redis wiring
│   │   ├── console.py               # Terminal REPL — no UI, no background mode (hub-cowork-console script)
│   │   ├── redis_bridge.py          # Redis inbox poller, classifier, per-user gate, outbox writer, presence
│   │   └── tray_icon.py             # Pure Win32 tray via ctypes (own message pump thread)
│   │
│   ├── tools/                   # Shared tools (auto-discovered)
│   │   ├── query_workiq.py
│   │   ├── log_progress.py
│   │   ├── get_task_status.py
│   │   ├── get_hub_config.py
│   │   ├── create_word_doc.py
│   │   ├── resolve_speakers.py
│   │   └── send_email.py
│   │
│   ├── skills/                  # YAML skills + optional skill-local tools
│   │   ├── qa.yaml
│   │   ├── task_status.yaml
│   │   ├── agenda_repurpose.yaml
│   │   ├── hub_agenda_creation/
│   │   │   ├── engagement_briefing.yaml     # Phase 1 (HITL)
│   │   │   ├── engagement_goals.yaml        # Phase 2
│   │   │   ├── engagement_agenda_build.yaml # Phase 3
│   │   │   ├── engagement_agenda_publish.yaml # Phase 4
│   │   │   └── tools/engagement_context.py
│   │   ├── meeting_invites/
│   │   │   ├── meeting_invites.yaml
│   │   │   └── tools/create_meeting_invites.py
│   │   └── rfp_evaluation/
│   │       ├── rfp_evaluation.yaml
│   │       └── tools/
│   │           ├── create_rfp_brief_doc.py
│   │           ├── query_fabric_agent.py
│   │           ├── search_foundryiq.py
│   │           └── share_onedrive_document.py
│   │
│   └── assets/                  # Shipped inside the wheel
│       ├── .env.defaults            # Lowest-precedence env defaults
│       ├── chat_ui.html             # Three-pane chat UI
│       ├── hub_config.default.json  # Default hub settings
│       ├── agent_icon.png
│       └── agent_icon.ico
│
├── scripts/
│   ├── start.ps1                # Launch detached via pythonw
│   ├── stop.ps1                 # Kill running instance(s)
│   ├── restart.ps1              # Stop + start
│   └── autostart.ps1            # Install/uninstall Windows login auto-start
│
├── test-client/                 # Console REPL test client (simulates a Teams relay)
│   ├── chat.py
│   └── requirements.txt
│
├── docs/
│   └── architecture.png         # (legacy — see diagram above for the current model)
│
└── user-stories/                # Planning notes for past and future features
```

---

## Getting Started

### Prerequisites

- **Windows 11** (Mac support exists but is untested)
- **Python 3.12+**
- **WorkIQ CLI** installed and on `PATH` (or `WORKIQ_PATH` set in `.env`)
- **Azure OpenAI** resource with a full model (e.g., `gpt-5.2`) and a mini model (e.g., `gpt-5.4-mini`) deployed
- **Azure Communication Services** resource (for meeting invites)
- **Azure Managed Redis** (optional) — enables Teams remote access. Entra ID auth only (no API keys).

### Installation

```powershell
# Clone
git clone <repo-url>
cd hub-cowork

# Virtual env
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install (editable, so edits to src/ take effect immediately)
pip install -e .

# Or: pin-for-pin install
# pip install -r requirements.txt
# $env:PYTHONPATH = "$PWD\src"

# Configure
copy .env.example .env
# Edit .env with Azure endpoints, model names, tenant id, ACS, and (optional) Redis.
```

### Running

```powershell
# Headless production (no console window)
.\scripts\start.ps1

# Force-restart
.\scripts\restart.ps1

# Stop
.\scripts\stop.ps1

# Debug with console output
python -m hub_cowork

# Console REPL (no UI, no Redis bridge)
hub-cowork-console     # or: python -m hub_cowork.host.console
```

When installed via `pip install -e .`, two console scripts are registered (see `pyproject.toml`):

- `hub-cowork` — GUI launcher (no console window, equivalent to `pythonw -m hub_cowork`)
- `hub-cowork-console` — terminal REPL

### Auto-start at Windows login

```powershell
.\scripts\autostart.ps1 install     # creates a VBScript launcher in the Startup folder
.\scripts\autostart.ps1 uninstall
```

---

## Testing Remote Task Delivery

The `test-client/` folder contains a console REPL that simulates a remote sender by reading/writing the same Redis streams the Teams relay uses.

### Prerequisites

- Agent is running (`.\scripts\start.ps1`)
- `AZ_REDIS_CACHE_ENDPOINT` set in `.env`
- The test client reuses the agent's saved auth record at `~/.hub-cowork/auth_record.json`

### Running

```powershell
.\.venv\Scripts\Activate.ps1
python test-client\chat.py
```

On startup the client authenticates, connects to Redis using the **same `REDIS_NAMESPACE`** as the agent, reads the presence key `{ns}:agents:{email}` to confirm the agent is online, then prompts `You >`.

### What to test

| Test | What happens |
|---|---|
| Type `hello` | Classifier → `system` → router handles as small talk → purple "remote" bubble in the UI + reply in the test client |
| Type a business query | Classifier → `new` → gate check → new thread created → skill runs → outbox reply arrives with `#thread-xxxx` prefix |
| Type another business query immediately | Gate rejects: "You already have a task in progress — reply in that thread or wait for it to finish" |
| Reply to an `awaiting_user` thread (keep the `#thread-xxxx` tag) | Classifier fast-path via `thread_id` hint → `existing` → resumes the paused skill |
| Ask `what's the status?` from the local UI while a remote task runs | Non-queued `task_status` skill reports live progress without interrupting |

---

## WebSocket Protocol

All messages are JSON with a `type` field. The UI and backend share the same protocol.

**Client → server:** `create_thread`, `send_to_thread`, `list_threads`, `get_thread`, `archive_thread`, `unarchive_thread`, `list_archived_threads`, `delete_thread`, `system_query`, `signin`, `clear_history`, `get_logs`, `get_config`, `save_config`.

**Server → client:** `threads_list`, `thread_created`, `thread_updated`, `thread_detail`, `thread_started`, `thread_progress`, `thread_completed`, `thread_error`, `thread_archived`, `thread_unarchived`, `thread_deleted`, `log_entry`, `log_history`, `system_query_*`, `auth_status`, `skills_list`, `remote_message`.

Every invocation carries a `request_id` (`uuid.uuid4().hex[:8]`) used for correlation across WebSocket, UI, Redis outbox, and log entries.

### Redis streams schema

| Key | Direction | Fields |
|---|---|---|
| `{ns}:inbox:{email}`   | Remote → Agent | `sender`, `text`, `ts`, `msg_id`, optional `thread_id` hint |
| `{ns}:outbox:{email}`  | Agent → Remote | `task_id`, `status`, `text` (prefixed with `#thread-xxxx`), `ts`, `in_reply_to` |
| `{ns}:agents:{email}`  | Agent → Cloud  | JSON: `{name, email, started_at, version}` with TTL refreshed every 30 min |

`{ns}` is `REDIS_NAMESPACE` (default: `hub-cowork`).

---

## Configuration

Set in `.env` at the repo root, or in `src/hub_cowork/assets/.env.defaults` for shipped defaults, or via the Settings UI (which writes `_env_overrides` into `~/.hub-cowork/hub_config.json`).

| Variable | Description |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_CHAT_MODEL` | Full model (e.g., `gpt-5.2`) |
| `AZURE_OPENAI_CHAT_MODEL_SMALL` | Mini model (e.g., `gpt-5.4-mini`) |
| `AZURE_OPENAI_API_VERSION` | e.g., `2025-03-01-preview` |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | (optional) Azure subscription ID |
| `ACS_ENDPOINT` | Azure Communication Services endpoint |
| `ACS_SENDER_ADDRESS` | Verified sender for ACS email |
| `AZ_REDIS_CACHE_ENDPOINT` | (optional) `host:port` — enables remote delivery |
| `REDIS_NAMESPACE` | (optional, default `hub-cowork`) — key prefix |
| `REDIS_SESSION_TTL_SECONDS` | (optional, default `86400`) — presence key TTL |
| `AGENT_TIMEZONE` | (optional) IANA override; auto-detected otherwise |
| `WORKIQ_PATH` | (optional) Full path to WorkIQ CLI |
| `FOUNDRYIQ_ENDPOINT`, `FOUNDRYIQ_KB_NAME`, `FOUNDRYIQ_AUTH_MODE`, `FOUNDRYIQ_API_VERSION` | RFP skill only — Azure AI Search knowledge store |
| `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME`, `FOUNDRY_AUTH_MODE` | RFP skill only — Fabric Data Agent |
| `RESOURCE_TENANT_ID` | RFP skill only — cross-tenant guest subscription |
| `RFP_OUTPUT_FOLDER`, `RFP_SHARE_RECIPIENTS` | RFP skill — OneDrive output + share list |
| `GRAPH_*` | (optional) Microsoft Graph app creds for document sharing; falls back to WorkIQ |

**Redis is optional.** Without `AZ_REDIS_CACHE_ENDPOINT`, the agent runs local-only — all features work except Teams remote access.

---

## Authentication

A single `InteractiveBrowserCredential` (created in `core/agent_core.py`) is shared across all components via `set_credential()` / `get_credential()`:

1. **First launch** — The UI shows a "Not signed in" banner. Click **Sign In** for Entra ID browser auth.
2. **Token caching** — The `AuthenticationRecord` is serialised to `~/.hub-cowork/auth_record.json`; the token cache is persisted via Windows Credential Manager.
3. **Subsequent launches** — The saved record enables silent token refresh — no browser prompt.
4. **Token refresh** — The OpenAI client checks expiry with a 5-minute buffer, with fallback to interactive login if silent refresh fails. The token-refresh path on the OpenAI client is guarded by a lock.
5. **Shared credential** — Used by OpenAI, WorkIQ helpers, ACS, and the Redis bridge (wrapped in `redis-entraid`'s `EntraIdCredentialsProvider`). No `DefaultAzureCredential` chain — no `az` CLI subprocesses under `pythonw.exe`.

---

## Dependencies

| Package | Purpose |
|---|---|
| `openai` | Azure OpenAI Responses API client |
| `azure-identity[persistent-cache]` | Persistent token cache |
| `azure-communication-email` | ACS email (calendar invites) |
| `python-dotenv` | `.env` loading |
| `pywebview` | Desktop window for the chat UI |
| `websockets` | UI ↔ backend WebSocket |
| `winotify` | Windows toasts |
| `pyyaml` | Skill YAML parsing |
| `tzlocal` | Auto-detect system timezone |
| `python-docx` | Word document creation |
| `redis`, `redis-entraid` | Azure Managed Redis (cluster mode, passwordless) |
| `azure-ai-projects` | RFP skill — Fabric Data Agent client |
| `requests` | RFP skill — FoundryIQ REST |

---

## Logging

All logs are written to `~/.hub-cowork/agent.log` — routing decisions, tool calls, thread executor events, Redis bridge events, classifier verdicts, and authentication. The WebSocket log handler reads `current_thread_id` from a ContextVar set by `_ThreadWorker`, so log records are routed to the correct per-thread `code_log` in the UI. Entries with no thread context fall into the `system` bucket.

---

## Pitfalls

- Azure auth must complete (user clicks **Sign In**) before any LLM or tool calls work.
- `query_workiq` shells out to the `workiq` CLI binary — must be on `PATH` or set `WORKIQ_PATH`.
- Windows-specific stack: `pythonw.exe`, `winotify`, Win32 ctypes tray. Mac support exists but is untested.
- `scripts\stop.ps1` matches `pythonw.exe` processes whose command line contains `-m hub_cowork` — it will NOT kill unrelated `pythonw` processes.
- Ports **18080** (WebSocket) and **18081** (HTTP) are hardcoded.
- No automated tests — verification is manual via the UI or `test-client/chat.py`.

---

## License

See the repository's license file.
# WorkIQ-Hub-SE-Agent

**Part 1 of 2** — This is the always-on desktop agent. It works in tandem with a companion cloud application (Part 2) that lets users interact with this agent **remotely from their mobile phones via Microsoft Teams**. Users can leave their computer, send requests from Teams, and receive completed results — including multi-step agentic workflows — without being at their desk.

Platforms like Claude CoWork and OpenClaw are defining the next wave of AI — autonomous agents that live on the user's local computer, act on their behalf, and integrate deeply with the tools they already use. The WorkIQ-Hub-SE-Agent exhibits a few of their key design traits — **always-on local execution, skills-driven autonomy, and remote reachability** — applied to a specific class of Microsoft 365 workflow tasks that are commonly performed today and remain painstaking and cumbersome to do manually (resolving contacts across documents, cross-referencing agendas with email directories, sending batches of calendar invites). Like those platforms, the agent runs on the user's computer but can be reached from other channels: a user can message it from **Microsoft Teams on their phone**, ask questions, and have multi-step workflows executed autonomously — no manual intervention, no need to be at the desk.

---

## Functional Features

| Feature | Description |
|---|---|
| **Autonomous agentic execution** | State your intent in plain language. The agent orchestrates multi-step workflows end-to-end — deciding what data to fetch, what actions to take, and how to present the outcome — without further human input. |
| **Remote access via Microsoft Teams** | Send requests and receive responses from your phone through Teams. The agent processes the work locally on your machine and delivers the result back through Azure Managed Redis. |
| **FIFO task queue** | All business tasks are queued and processed one at a time. Queue multiple requests — they execute sequentially without interrupting each other. System queries (status checks, greetings) bypass the queue and respond instantly. |
| **Real-time task status** | Ask "what's the status of my request?" at any time — even while a long-running task is executing. The agent summarizes progress milestones from the live execution log. |
| **Concurrent request isolation** | Multiple requests (local + remote) are tracked independently. Each gets its own UI bubble and progress stream — no cross-talk. |
| **Skills-driven extensibility** | Each capability is a declarative YAML file. Add a new skill by dropping a YAML file into `skills/` — no code changes, no redeployment. |
| **Background operation** | Runs invisibly via `pythonw.exe` — no console window, no taskbar clutter until you summon it. |
| **System tray icon** | Left-click the tray icon to show/hide the chat UI. Right-click for a context menu (Show / Hide, Quit). |
| **Toast notifications** | Native Windows 10/11 toasts for task progress and completion. Click a toast to open the UI. |
| **Intelligent routing** | A master router classifies every request and delegates to the appropriate skill-specific sub-agent. |
| **Adaptive model selection** | Complex workflows use a full LLM (`gpt-5.2`); Q&A and general responses use a smaller, faster model (`gpt-5.4-mini`) for cost-efficient responsiveness. |
| **Markdown-rendered responses** | Tables, code blocks, lists, and headings rendered natively in the chat UI. Progress updates render as formatted markdown with structured step indicators. |
| **Persistent authentication** | Sign in once; tokens are cached and silently refreshed across restarts via Azure Identity with persistent token cache. |
| **Auto-start at Windows login** | An install script registers the assistant to launch at startup. |

---

## Key Technical Capabilities

| Capability | Implementation |
|---|---|
| **Azure OpenAI Responses API** | The agentic core — tool definitions and natural-language instructions drive autonomous tool-call orchestration. No custom workflow code or state machines. |
| **Azure Managed Redis (cluster mode)** | Inbox/outbox streams keyed by user email. Passwordless Entra ID authentication via `redis-entraid` credential provider with automatic token refresh. |
| **Task queue with request classification** | Skills declare `queued: true/false`. Business tasks queue in FIFO; system tasks (status, greetings) execute immediately. Each task carries full progress logs for status reporting. |
| **Composable tool system** | Tools are self-contained Python modules in `tools/` — discovered and registered at startup via `importlib`. Add a tool by dropping a `.py` file. |
| **Composable skill system** | Skills are YAML files in `skills/` (and subdirectories) — discovered recursively at startup. The router prompt is auto-generated from skill descriptions. Internal chained skills are excluded from routing. Add a skill by dropping a `.yaml` file. |
| **Request-ID based concurrency** | Every request gets a unique ID. All WebSocket messages, UI bubbles, and Redis correlation use this ID for complete task isolation. |
| **Shared credential architecture** | A single `InteractiveBrowserCredential` instance (with cached `AuthenticationRecord`) is shared across OpenAI, WorkIQ, ACS, and Redis — one sign-in, zero command prompts. |

---

## The Two-Part Architecture

![Solution Architecture](docs/architecture.png)

**Part 1** (this repo) is the agent itself — running on a Windows 11 laptop, processing tasks locally with full access to the user's Microsoft 365 data via WorkIQ. It registers its presence in Azure Managed Redis and polls an inbox stream for remote requests.

**Part 2** (separate repo) is a cloud service that bridges Microsoft Teams to the Redis streams. When a user sends a message in Teams, the relay service pushes it to the agent's Redis inbox. When the agent writes a result to the outbox, the relay delivers it back to the Teams conversation.

The user experience: send a message from your phone in Teams → the agent on your laptop picks it up, executes the full agentic workflow (retrieving M365 data, calling tools, orchestrating multi-step actions) → the result appears in your Teams chat.

---

## A Heterogeneous Agentic Solution

This agent bridges two distinct pillars of the Microsoft AI stack:

- **Microsoft 365 Copilot & WorkIQ** (part of the [Microsoft Intelligence](https://www.microsoft.com/en-us/microsoft-365) suite) — the productivity platform that surfaces enterprise knowledge from calendars, emails, documents, contacts, and SharePoint.
- **Azure AI Foundry with Azure OpenAI Responses API** — the code-first agentic platform that builds autonomous, tool-calling agents with nothing more than tool definitions and natural-language instructions.

WorkIQ provides the **data and enterprise context**. Azure OpenAI Responses API provides the **autonomous reasoning and orchestration**. The result is an agent that understands intent, retrieves live Microsoft 365 data, and acts on it through multi-step tool-calling workflows — without custom orchestration code.

WorkIQ alone answers questions but cannot execute multi-step actions. Azure OpenAI alone can reason but has no access to enterprise data. Together, they form an agent that both *knows* and *acts*.

---

## Built-in Skills

WorkIQ-Hub-SE-Agent is **skills-driven** — each capability is a declarative YAML file rather than hardcoded logic. Skills are discovered at startup; the router prompt is auto-built from their descriptions.

| Skill | Model | Queued | Tools | What it does |
|---|---|---|---|---|
| **Meeting Invites** | full (`gpt-5.2`) | Yes | `query_workiq`, `log_progress`, `create_meeting_invites` | Autonomous workflow: retrieve agenda → filter speakers → resolve emails → send calendar invites |
| **Engagement Briefing** | full (`gpt-5.2`) | Yes | `query_workiq`, `log_progress`, `engagement_context` | Phase 1: locate briefing calls, **confirm selection with user** (human-in-the-loop), retrieve notes, extract metadata. Auto-chains → Engagement Goals |
| **Engagement Goals** | full (`gpt-5.2`) | Yes | `log_progress`, `engagement_context` | Phase 2: extract and segment customer goals from briefing notes. Auto-chains → Agenda Build |
| **Engagement Agenda Build** | full (`gpt-5.2`) | Yes | `log_progress`, `engagement_context`, `get_hub_config` | Phase 3: build a detailed agenda markdown table with time slots, speakers, descriptions. Auto-chains → Agenda Publish |
| **Engagement Agenda Publish** | full (`gpt-5.2`) | Yes | `log_progress`, `engagement_context`, `create_word_doc` | Phase 4: create a Word document from the agenda using python-docx and save to the configured output folder |
| **Agenda Repurpose** | full (`gpt-5.2`) | Yes | `query_workiq`, `log_progress`, `create_word_doc`, `get_hub_config` | Conversational: retrieve an existing agenda, collect new customer details (name, date, venue), create a repurposed Word document |
| **Q&A** | mini (`gpt-5.4-mini`) | Yes | `query_workiq`, `log_progress` | Conversational Q&A about M365 data with session history |
| **Task Status** | mini (`gpt-5.4-mini`) | No | `get_task_status` | Report current task progress and queue depth — responds instantly even while a task is running |
| *(Router direct)* | mini (`gpt-5.4-mini`) | No | *(none)* | Greetings and small talk — the router handles these directly without invoking a skill |

**Queued = Yes**: task enters the FIFO queue and executes when its turn comes.
**Queued = No**: task executes immediately, bypassing the queue.

### Engagement Agenda Workflow — Autonomous 4-Phase Skill Chain

The engagement agenda workflow demonstrates how multiple skills chain together autonomously. The user provides only a **customer name** — the agent then executes four phases in sequence, each passing structured context to the next:

```
  User: "create an agenda for Contoso"
    │
    ▼
  Phase 1: engagement_briefing (conversational, multi-turn)
    │  Turn 1: Find briefing calls → present candidates to user
    │          → [AWAITING_CONFIRMATION] → pause for user input
    │
    │  User confirms or requests corrections
    │
    │  Turn 2+: If confirmed → retrieve notes → extract metadata
    │           If corrections → re-search → re-present → wait again
    │  Saves: metadata, participants, meeting notes
    │  next_skill: engagement_goals
    ▼
  Phase 2: engagement_goals
    │  Reason over notes → extract & segment customer goals
    │  Saves: goals with source excerpts for traceability
    │  next_skill: engagement_agenda_build
    ▼
  Phase 3: engagement_agenda_build
    │  Load goals + hub config → build agenda table
    │  Maps goals to sessions, assigns speakers, computes time slots
    │  Saves: agenda_markdown
    │  next_skill: engagement_agenda_publish
    ▼
  Phase 4: engagement_agenda_publish
    │  Load agenda → create Word document locally using python-docx
    │  Optional template support (header image from .docx template)
    │  Document name: Agenda-<Customer>-<Month-Year>.docx
    │  Saved to configured output folder (default: OneDrive)
    ▼
  Result: Complete agenda displayed in UI + Word doc saved to disk
```

**Skill chaining** is driven by the `next_skill` field in each YAML definition. When a skill completes, `agent_core._run_skill()` checks for `next_skill` and immediately invokes the next phase with the completion text as input. If the completion text contains `[STOP_CHAIN]`, chaining is halted — this allows skills to gate on errors (e.g., no briefing calls found) and prevent subsequent phases from running with missing data.

**Human-in-the-loop confirmation** — Skills can pause for user input by including `[AWAITING_CONFIRMATION]` in their final text. When detected, `agent_core` sets an `_active_session` (tracking the skill name and stage), strips the marker from the response, and returns it to the user without chaining. On the user's next message, the router recognizes the active session and routes the response back to the same skill. The skill must be `conversational: true` so it retains conversation history — it checks prior assistant messages to determine it is on Turn 2+ and handles the user's confirmation or corrections. Once the skill completes normally (no markers), the active session is cleared and `next_skill` chaining proceeds as usual. Phase 1 (`engagement_briefing`) uses this pattern to confirm selected briefing calls before extracting notes.

**Inter-phase context** is passed via the `engagement_context` tool, which saves/loads structured JSON to `~/.hub-cowork/engagement_context/<customer>.json`. Each phase adds its output (metadata, goals, agenda) to the shared context file.

**Hub configuration** provides the default session start time and speaker-by-topic mapping. Phase 3 loads this via the `get_hub_config` tool to assign speakers and set time slots. Users can edit the configuration through the ⚙ Settings UI in the chat window.

**Engagement type detection** — Phase 1 classifies the engagement as one of: `ADS`, `RAPID_PROTOTYPE`, `BUSINESS_ENVISIONING`, `SOLUTION_ENVISIONING`, `HACKATHON`, or `CONSULT`. Phase 3 applies type-specific rules for agenda construction:

| Type | Agenda pattern |
|---|---|
| ADS | For each goal: customer presents requirements → Hub SE leads architecture discussion |
| Rapid Prototype | For each goal: customer walks through requirements → hands-on prototyping (parallel tracks supported) |
| Business Envisioning | Customer perspective, industry advisor, use-case showcase, trends — business-level descriptions |
| Solution Envisioning | All business envisioning types + technical depth, architecture, demos, open discussions |

**Description composition** — Each session description has two parts: (1) a capability-rich narrative written by the LLM describing what the Hub SE will present/demo, and (2) relevant customer goal details in italics for traceability.

**Local Word document generation** — Phase 4 uses the `create_word_doc` tool (powered by `python-docx`) to create Word documents locally. It parses the agenda markdown table, renders formatted tables with borders, bold/italic text, and cell-level styling. If an `agenda_template_path` is configured, the tool opens the template document (which can contain a header image or branding) and appends the agenda content after it. Documents are saved to the `agenda_output_folder` path from hub config.

**WorkIQ stdin mode** — The `query_workiq` tool automatically detects long questions (>7000 chars) and switches from CLI argument (`-q`) to interactive stdin mode, which has no length limit.

### Adding a new skill

For skills using existing tools — **no Python code required**:

1. Create a `.yaml` file in `skills/` (or a subdirectory for grouped skill chains)
2. Define the required fields: `name`, `description`, `model`, `conversational`, `queued`, `tools`, and `instructions`
3. Mark chained internal skills with `[INTERNAL` in their description to exclude them from the router
4. Restart the agent — auto-discovered recursively, router starts routing matching requests

For skills needing a new tool:

1. Create a `.py` file in `tools/` with `SCHEMA` dict and `handle()` function
2. Reference the tool by name in the skill's `tools:` list
3. Restart — both are auto-discovered

#### Skill YAML field reference

| Field | Required | Type | Description |
|---|---|---|---|
| `name` | Yes | `string` | Unique identifier — what the router returns when it classifies a request |
| `description` | Yes | `string` | Natural-language description used by the router to match user intent. Prefix with `[INTERNAL` to exclude from routing (only reachable via `next_skill` chaining) |
| `model` | Yes | `"full"` \| `"mini"` | `full` → complex reasoning model (e.g., `gpt-5.2`); `mini` → faster/cheaper model (e.g., `gpt-5.4-mini`) |
| `conversational` | Yes | `bool` | `true` → maintains session history across turns; `false` → each invocation is stateless |
| `queued` | Yes | `bool` | `true` → enters FIFO task queue; `false` → executes immediately (use for lightweight system tasks) |
| `tools` | Yes | `list[string]` | Tool names this skill can call (must exist in `tools/`) |
| `instructions` | Yes | `string` | System prompt — all the Responses API needs to orchestrate the workflow |
| `next_skill` | No | `string` | Name of the skill to automatically chain to on completion. Chaining is skipped if the output contains `[STOP_CHAIN]` or `[AWAITING_CONFIRMATION]` |

#### Enabling conversation (multi-turn skills)

Set `conversational: true` when the skill needs to:
- Maintain context across multiple user messages (e.g., follow-up Q&A)
- Implement human-in-the-loop confirmation patterns (i.e., use `[AWAITING_CONFIRMATION]`)

When `conversational: true`:
- The skill's conversation history (`user` + `assistant` messages) is stored in `_conversation_histories[skill.name]` and passed to the Responses API as prior conversation context on each invocation.
- History is bounded to the last 20 messages to keep context windows manageable.
- History is automatically cleared when a **fresh invocation** of the skill begins (i.e., a new engagement, not a continuation of an active session). This prevents stale context from a previous engagement leaking into a new one.

When `conversational: false` (default for most skills):
- Each invocation is stateless — no history is stored or passed.
- Suitable for skills that execute autonomously in a single turn.

#### Adding human-in-the-loop confirmation

For skills that need to pause, present results to the user, and wait for explicit confirmation before proceeding:

1. **Set `conversational: true`** in the skill YAML — the skill needs conversation history to distinguish Turn 1 from Turn 2+.

2. **Structure instructions as multi-turn**:
   - **Turn 1**: Execute initial steps (e.g., search, gather data), present results to the user, ask for confirmation. The skill's final text must include `[AWAITING_CONFIRMATION]` at the end.
   - **Turn 2+**: Check conversation history to determine the turn. If the user confirmed → proceed with remaining steps. If the user provided corrections → re-do the search and ask again (with `[AWAITING_CONFIRMATION]`). If ambiguous → re-ask (with `[AWAITING_CONFIRMATION]`).

3. **Marker behavior** — When `agent_core._run_skill()` detects `[AWAITING_CONFIRMATION]` in the skill's final text:
   - Sets `_active_session = {"skill_name": <name>, "stage": "awaiting_confirmation"}`
   - Strips the marker from the response text
   - Returns the text to the user **without chaining** to `next_skill`
   - On the user's next message, the router detects the active session and routes the response back to the same skill
   - The skill (via its conversation history) knows it is on Turn 2+ and handles the response
   - Once the skill completes normally (no markers), `_active_session` is cleared and `next_skill` chaining proceeds

4. **Important rules**:
   - `[AWAITING_CONFIRMATION]` must ONLY appear when the skill is genuinely waiting — never in the final completion response
   - The skill must handle all three cases: confirmation, corrections, and ambiguous/unrelated responses
   - `[STOP_CHAIN]` takes priority and also clears the active session

**Example** — see `skills/hub-agenda-creation/engagement_briefing.yaml` for a complete implementation of this pattern.

#### Skill chaining (multi-phase workflows)

To create a multi-phase autonomous workflow:

1. Create one YAML per phase in a subdirectory (e.g., `skills/my-workflow/`)
2. Set `next_skill: <next_phase_name>` on each phase except the last
3. Mark phases 2+ with `[INTERNAL` in their description so only phase 1 is routable
4. Use the `engagement_context` tool (or a similar shared storage tool) to pass structured data between phases
5. Use `[STOP_CHAIN]` in any phase to halt the chain on errors
6. Use `[AWAITING_CONFIRMATION]` in any phase to pause for user input before continuing

**Control flow markers** (emitted by skills in their final text):

| Marker | Effect |
|---|---|
| *(none)* | Normal completion — chain to `next_skill` if configured |
| `[STOP_CHAIN]` | Halt chaining, clear active session, return text as-is |
| `[AWAITING_CONFIRMATION]` | Pause for user input, set active session, strip marker, do NOT chain |

### Skills-Driven Architecture

The meeting invites skill illustrates how a complex multi-step autonomous workflow is defined entirely in YAML — the full five-step sequence (retrieve agenda → filter speakers → resolve emails → send invites → report results) is expressed as natural-language instructions with zero Python orchestration code:

```yaml
# skills/meeting_invites.yaml
name: meeting_invites
description: >
  Send or create calendar invites and meeting invitations to speakers or
  presenters from an agenda document or event. Keywords: invite, calendar,
  schedule speakers, send invites, agenda, engagement.
model: full              # "full" → gpt-5.2 (complex reasoning)
conversational: false    # no follow-up context needed

tools:
  - query_workiq
  - log_progress
  - create_meeting_invites

instructions: |
  You are an autonomous Hub Engagement Speaker Schedule Management Agent.

  Given a user request about a customer engagement event, you MUST complete
  ALL of the following steps using tool calls — do NOT stop or return text
  to the user until every step is done.

  STEP 1: Call query_workiq to retrieve the COMPLETE agenda document. Ask
  for: EVERY row in the agenda table including topic names, speaker names,
  and time slots for each session. ...

  STEP 2: From the COMPLETE list of rows, identify ALL Microsoft employee
  speakers. Apply these rules:
  DISCARD rows that are:
  - Lunch breaks, tea breaks, coffee breaks, or any kind of break
  - Rows with no topic or no speaker assigned
  - Rows where the speaker is ONLY a team name or company name
  KEEP rows where:
  - The speaker is a clearly identifiable individual person's name
  ...

  STEP 3: Call query_workiq ONCE to look up the Microsoft corporate email
  addresses of ALL the individual speakers identified in Step 2.
  ...

  STEP 4: Call create_meeting_invites with the curated list of sessions,
  including each speaker's email address.
  ...

  STEP 5: After the invites are created, present the user with a final
  summary table showing: Topic, Speaker, Time Slot, Email, and Status.
  ...

  IMPORTANT:
  - Complete ALL steps autonomously in a single turn.
  - Always call log_progress after each query_workiq call.
  - If a speaker appears in multiple sessions, create a separate invite
    for each session.
```

The **entire five-step workflow is expressed as natural-language instructions**. No Python code for step sequencing, conditional logic, or state management. The Responses API reads these instructions and autonomously orchestrates the tool calls.

| Field | Purpose |
|---|---|
| `name` | Unique identifier — what the router returns when it classifies a request |
| `description` | Natural-language description used by the router to match user intent. Prefix with `[INTERNAL` to exclude from routing |
| `model` | `full` for complex reasoning (e.g., meeting invites), `mini` for Q&A and summarization |
| `queued` | `true` → enters FIFO task queue; `false` → executes immediately (system tasks) |
| `conversational` | `true` → maintains session history for follow-up questions and multi-turn flows (e.g., human-in-the-loop confirmation) |
| `tools` | List of tool names this skill can use (must exist in the tool registry) |
| `instructions` | The complete system prompt — all the Responses API needs to orchestrate the workflow |
| `next_skill` | *(optional)* Name of skill to chain to on normal completion. Skipped if output contains `[STOP_CHAIN]` or `[AWAITING_CONFIRMATION]` |

> **Note on calendar invite delivery:** This sample uses **Azure Communication Services (ACS)** to send meeting invites via email with `.ics` attachments. Replacing the ACS-based delivery with the **WorkIQ Outlook MCP Server** (for creating events directly in Outlook) would require only swapping the `create_meeting_invites` tool implementation — no changes to agent instructions or orchestration logic.

---

## Hub Configuration & Settings UI

Hub-specific settings — speaker assignments, default session start times, and hub identity — are stored as JSON configuration and editable through the chat UI.

### Configuration architecture

```
hub_config.default.json    ← Checked into repo (defaults for Innovation Hub India)
     │
     │  hub_config.load() merges:
     │    defaults ← hub_config.default.json
     │    overrides ← ~/.hub-cowork/hub_config.json (user edits)
     │
     ▼
  Merged config returned to caller
```

- **`hub_config.default.json`** — Ships with the app. Contains default hub name, session start time, and speaker-by-topic mapping. Checked into version control.
- **`~/.hub-cowork/hub_config.json`** — User-specific overrides. Created when the user saves settings. Git-ignored. Only changed fields are stored.
- **`hub_config.py`** — `load()` merges both files (user overrides win). `save(config)` writes the full config as user overrides.

### Settings UI

The ⚙ gear icon in the chat header opens a settings modal with:

- **Innovation Hub Name** — text field
- **Default Session Start Time** — time picker (stored as "09:00 AM" format)
- **Speakers by Topic** — editable table with Topic, Speaker 1, Speaker 2 columns, add/remove row buttons
- **Agenda Output Folder** — file path where generated Word documents are saved
- **Agenda Template Document** — optional `.docx` template path (e.g., with header image/branding) prepended to generated agendas

Settings are read/written via WebSocket messages (`get_config` / `save_config`) handled in `meeting_agent.py`.

### How skills use the configuration

The `get_hub_config` tool returns the merged configuration as JSON. Skills (like `engagement_agenda_build`) call this tool to:
- Set the first session start time
- Match session topics to speakers
- Prefer speakers who were on the briefing call

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│                           Windows 11 Desktop                               │
│                                                                            │
│  ┌─────────────────────┐    ┌────────────────────────────────────────────┐ │
│  │  pywebview Window   │◄──►│  WebSocket Server (ws://18080)             │ │
│  │  (chat_ui.html)     │    │  HTTP Server     (http://18081)            │ │
│  │                     │    │                                            │ │
│  │ • Markdown rendering│    │  ┌──────────────────┐  ┌────────────────┐  │ │
│  │ • Progress steps    │    │  │  Tool Loader     │  │  Skill Loader  │  │ │
│  │ • Remote msg bubbles│    │  │  tools/*.py      │  │  skills/*.yaml │  │ │
│  │ • Queue status      │    │  └────────┬─────────┘  └───────┬────────┘  │ │
│  │ • Auth banner       │    │           │                    │           │ │
│  └─────────────────────┘    │  ┌────────▼────────────────────▼─────────┐ │ │
│                             │  │          Router (Master Agent)        │ │ │
│  ┌────────────────────┐     │  │          Azure OpenAI gpt-5.2         │ │ │
│  │ System Tray Icon   │     │  │   (prompt auto-built from skill       │ │ │
│  │ (left/right-click) │     │  │    descriptions)                      │ │ │
│  └────────────────────┘     │  └───────────────┬───────────────────────┘ │ │
│                             │                  │ classifies intent       │ │
│  ┌─────────────────────┐    │         ┌────────▼────────┐                │ │
│  │ Toast Notifications │    │         │   Request       │                │ │
│  │ (winotify)          │    │         │   Classifier    │                │ │
│  └─────────────────────┘    │         │  queued: true?  │                │ │
│                             │         └───┬─────────┬───┘                │ │
│                             │             │         │                    │ │
│                             │     ┌───────▼──┐  ┌───▼──────────────┐     │ │
│                             │     │   FIFO   │  │ Immediate exec   │     │ │
│                             │     │   Task   │  │ (general, status)│     │ │
│                             │     │   Queue  │  └──────────────────┘     │ │
│                             │     └───┬──────┘                           │ │
│                             │         │ one at a time                    │ │
│                             │  ┌──────▼───────────────────────────────┐  │ │
│                             │  │  Skill Sub-Agent Execution           │  │ │
│                             │  │  (model + tools + instructions)      │  │ │
│                             │  │  Azure OpenAI Responses API          │  │ │
│                             │  │  • Autonomous tool-call orchestration│  │ │
│                             │  │  • No custom workflow code           │  │ │
│                             │  └──────┬───────────────┬───────────────┘  │ │
│                             │         │               │                  │ │
│                             │  ┌──────▼──────┐ ┌──────▼──────────────┐   │ │
│                             │  │ Tool Layer  │ │ Progress Broadcast  │   │ │
│                             │  │ query_workiq│ │ → UI (WebSocket)    │   │ │
│                             │  │ log_progress│ │ → Toast notification│   │ │
│                             │  │ create_mtg  │ │ → Progress log      │   │ │
│                             │  │ get_status  │ │                     │   │ │
│                             │  └──────┬──────┘ └─────────────────────┘   │ │
│                             └─────────┼──────────────────────────────────┘ │
│                                       │                                    │
│  ┌────────────────────────────────────▼──────────────────────────────────┐ │
│  │                      Redis Bridge (optional)                          │ │
│  │  • Polls workiq:inbox:{email} for remote messages                     │ │
│  │  • Writes results to workiq:outbox:{email}                            │ │
│  │  • Registers workiq:agents:{email} with TTL heartbeat                 │ │
│  │  • Entra ID auth via shared InteractiveBrowserCredential              │ │
│  │  • RedisCluster with redis-entraid credential_provider                │ │
│  └───────────────────────────┬───────────────────────────────────────────┘ │
└──────────────────────────────┼─────────────────────────────────────────────┘
                               │
              ┌────────────────▼─────────────────┐
              │    Azure Managed Redis           │
              │    (cluster mode, Entra ID)      │
              │    inbox / outbox / agents keys  │
              └────────────────┬─────────────────┘
                               │
              ┌────────────────▼─────────────────┐
              │    Part 2: Teams Relay Service   │
              │    (companion cloud app)         │
              └──────────────────────────────────┘

              ┌──────────────────────────────────┐
              │    WorkIQ CLI → M365 Graph API   │
              │    Calendar · Email · Files ·    │
              │    Contacts · SharePoint         │
              └──────────────────────────────────┘

              ┌─────────────────────────────────┐
              │    Azure Communication Services │
              │    (calendar invite email)      │
              └─────────────────────────────────┘
```

### How It All Fits Together

1. **Single-process launcher** (`meeting_agent.py`) — Entry point. Starts WebSocket/HTTP servers, starts the system tray icon, configures the task queue, optionally starts the Redis bridge, shows a startup toast, and enters the pywebview event loop.

2. **WebSocket server** (port `18080`) — Communication backbone between the chat UI and the Python backend. User messages, agent responses, progress updates, auth status, queue notifications, and remote message alerts all flow over this channel as JSON.

3. **HTTP server** (port `18081`) — Handles toast notification clicks. When the user clicks a toast, Windows opens `http://127.0.0.1:18081/show`, which brings up the pywebview window.

4. **pywebview window** — Renders `chat_ui.html`. Starts hidden; close hides rather than quits. The `activeBubbles` Map tracks each concurrent request by `request_id` for complete isolation. The welcome screen shows **suggested prompt chips** — clickable examples that populate the input field, helping users discover available skills and phrasing patterns.

5. **Tool Loader** — Discovers all `.py` files in `tools/` via `importlib` at startup. Each module exports a `SCHEMA` dict and `handle()` function. Adding a tool requires only dropping a Python file.

6. **Skill Loader** — Recursively discovers all `.yaml` files in `skills/` and its subdirectories at startup. Parses each into a runtime `Skill` object and auto-builds the router prompt from their descriptions. Skills with `[INTERNAL` in their description are excluded from the router and cannot be invoked directly by users — they are only reachable via skill chaining.

7. **Router (Master Agent)** — Classifies every request into a skill name via LLM call. Includes a `"none"` category for greetings and small talk, which the agent handles with a direct lightweight LLM reply without invoking any skill. Also resolves the `queued` flag to determine whether the request enters the task queue or executes immediately.

8. **Task Queue** — In-memory FIFO queue with a dedicated worker thread. Business tasks (`queued: true`) execute one at a time. System tasks (`queued: false` — status queries, greetings) bypass the queue and respond instantly. Each task carries a full progress log for status reporting.

9. **Skill Sub-Agents** — Each skill operates with its own system prompt, tool set, and model tier:
   - **Meeting Invites** — `gpt-5.2`. Autonomous five-step workflow.
   - **Engagement Agenda** — `gpt-5.2`. Four-phase chained workflow (briefing → goals → agenda build → publish).
   - **Q&A** — `gpt-5.4-mini` with conversation history.
   - **Task Status** — `gpt-5.4-mini`. Reports live progress from execution logs.
   - **Small talk** — Handled directly by the router with a lightweight LLM call — no skill invocation.

10. **Azure OpenAI Responses API** — The agentic core. Tool definitions and natural-language instructions drive autonomous tool-call orchestration. No custom workflow code — multi-step behavior emerges from the instructions alone.

11. **Tool execution layer** — Self-contained Python modules in `tools/`:
    - `query_workiq` — Runs the WorkIQ CLI to query Microsoft 365 data. Auto-switches to stdin mode for long prompts (>7000 chars) to avoid Windows command line limits.
    - `log_progress` — Sends structured progress updates (rendered as markdown in the UI).
    - `create_meeting_invites` — Constructs `.ics` calendar invites, delivers via ACS.
    - `get_task_status` — Report current task progress and queue depth.
    - `engagement_context` — Save/load structured JSON between skill phases (stored in `~/.hub-cowork/engagement_context/`).
    - `get_hub_config` — Return hub configuration (speakers, start time) to skills.
    - `create_word_doc` — Create Word documents from agenda markdown using `python-docx`. Supports template documents with header images, formatted tables with borders, bold/italic text.

12. **Redis Bridge** (optional) — Connects the desktop agent to Azure Managed Redis for remote task delivery:
    - **Inbox poller** — Background thread polls `workiq:inbox:{email}` via `XREAD` (5s blocking). Remote messages are submitted to the task queue and shown in the UI as purple "remote" bubbles.
    - **Outbox writer** — On task completion, writes results to `workiq:outbox:{email}` with `in_reply_to` correlation for request-response matching.
    - **Agent registration** — Sets `workiq:agents:{email}` with TTL, refreshed by a heartbeat every 30 minutes. Remote clients check this key to verify the agent is online.
    - **Authentication** — Shares the agent's `InteractiveBrowserCredential` (with cached auth record for silent refresh), wrapped in `redis-entraid`'s `EntraIdCredentialsProvider`. No `DefaultAzureCredential` chain — no command windows on Windows.

---

## Technical Details

### Authentication Flow

A single `InteractiveBrowserCredential` from Azure Identity SDK is shared across all components — OpenAI, WorkIQ, ACS, and Redis:

1. **First launch** — The UI shows a "Not signed in" banner. Click **Sign In** to open a browser for Entra ID authentication.
2. **Token caching** — The `AuthenticationRecord` is serialized to `~/.hub-cowork/auth_record.json`. The token cache is persisted via Windows Credential Manager.
3. **Subsequent launches** — The saved record enables silent token refresh — no browser prompt.
4. **Token refresh** — The OpenAI client checks expiry with a 5-minute buffer. If silent refresh fails, it falls back to interactive browser login.
5. **Shared credential** — The same credential instance is shared with `outlook_helper.py` (via `set_credential()`) and with the Redis bridge (via `get_credential()`). This avoids duplicate browser prompts and prevents `DefaultAzureCredential` from spawning `az` CLI subprocesses under `pythonw.exe`.

### WebSocket Communication Protocol

| Direction | Message Type | Purpose |
|---|---|---|
| Server → Client | `auth_status` | Sign-in state and user identity |
| Client → Server | `task` | User submits a request |
| Server → Client | `task_started` | Processing has begun (includes `request_id` and `source`) |
| Server → Client | `progress` | Real-time updates (kind: `step`, `tool`, `progress`, `agent`) |
| Server → Client | `task_complete` | Final agent response with Markdown content |
| Server → Client | `task_error` | Error message |
| Server → Client | `remote_message` | Remote message arrived (sender + text, shown as purple bubble) |
| Client → Server | `signin` | User clicks Sign In |
| Server → Client | `signin_status` | Result of sign-in attempt |
| Client → Server | `clear_history` | Reset Q&A conversation history |
| Server → Client | `skills_list` | Loaded skills for the UI skills panel |

All messages include a `request_id` field for concurrent task isolation.

### Redis Streams Schema

| Stream | Direction | Fields |
|---|---|---|
| `workiq:inbox:{email}` | Remote → Agent | `sender`, `text`, `ts`, `msg_id` |
| `workiq:outbox:{email}` | Agent → Remote | `task_id`, `status`, `text`, `ts`, `in_reply_to` |
| `workiq:agents:{email}` | Agent → Cloud | JSON: `{name, email, started_at, version}` with TTL |

The `in_reply_to` field correlates outbox responses to inbox `msg_id` values, enabling request-response matching for remote clients.

### Window Management

- pywebview window starts **hidden**. Close hides rather than quits.
- **System tray icon** — Pure Win32 implementation via `ctypes` in `tray_icon.py`. Left-click to show/hide; right-click for a context menu (Show / Hide, Quit). Runs its own message pump in a background thread, independent of pywebview.
- **Toast click** opens `http://127.0.0.1:18081/show` to bring up the window.
- **Custom taskbar icon** via `SetCurrentProcessExplicitAppUserModelID` + `WM_SETICON` to override default `pythonw.exe` grouping.

### Subprocess Handling

All subprocess calls use `subprocess.CREATE_NO_WINDOW` on Windows to prevent `cmd.exe` windows from flashing during WorkIQ CLI invocations.

### Logging

All logs are written to `~/.hub-cowork/agent.log` — routing decisions, tool calls, thread executor events, Redis bridge events, and authentication.

---

## Project Structure

```
hub-cowork/
├── meeting_agent.py       # Main entry point — launcher, WebSocket/HTTP servers,
│                          #   pywebview window, tray icon, toast, ExecutorPool + Redis wiring
├── agent_core.py          # Core agent logic — router, skill loader, tool loader,
│                          #   auth helpers, shared credential, thread-scoped run funcs
├── thread_manager.py      # In-memory ConversationThread registry + observer pattern
├── thread_store.py        # LocalJsonThreadStore — per-thread JSON under ~/.hub-cowork/threads/
├── thread_executor.py     # Per-thread worker pool — one daemon thread per active conversation
├── conversation_thread.py # ConversationThread dataclass (id, status, messages, logs)
├── app_paths.py           # Central app-home + branding constants (~/.hub-cowork/, "Hub Cowork")
├── redis_bridge.py        # Azure Managed Redis bridge — inbox poller, outbox writer,
│                          #   agent presence registration, Entra ID credential_provider
├── agent.py               # Console entry point — terminal-based interaction for
│                          #   development and debugging (no UI, no background mode)
├── tray_icon.py           # System tray icon — pure Win32 ctypes, own message pump
│                          #   thread. Left-click show/hide, right-click context menu.
├── outlook_helper.py      # Azure Communication Services — .ics calendar invite
│                          #   construction, email delivery, organizer resolution
├── chat_ui.html           # Three-pane chat UI — thread list, chat, details/progress/logs
├── .env / .env.example    # Environment configuration (Azure endpoints, models, Redis)
├── requirements.txt       # Python dependencies
├── hub_config.py          # Hub configuration loader — merges defaults + user overrides
├── hub_config.default.json # Default config (speakers, start time) — checked into repo
├── tools/                 # Tool modules (Python) — loaded dynamically at startup
│   ├── query_workiq.py       # Query M365 data via WorkIQ CLI (auto stdin for long prompts)
│   ├── log_progress.py       # Real-time progress updates (rendered as markdown)
│   ├── create_meeting_invites.py  # Build .ics invites, send via ACS
│   ├── create_word_doc.py    # Create Word documents from agenda markdown (python-docx)
│   ├── get_task_status.py    # Report current task progress and queue depth
│   ├── engagement_context.py # Save/load structured context between skill phases
│   └── get_hub_config.py     # Return hub config (speakers, start time) to skills
├── skills/                # Skill definitions (YAML) — loaded recursively from skills/**/*.yaml
│   ├── meeting_invites.yaml  # Autonomous meeting invite workflow (full model, queued)
│   ├── qa.yaml               # Conversational Q&A via WorkIQ (mini model, queued)
│   ├── task_status.yaml      # Task/queue status reporting (mini model, immediate)
│   └── hub-agenda-creation/  # Grouped skill chain — 4-phase engagement agenda pipeline
│       ├── engagement_briefing.yaml   # Phase 1: briefing calls, notes, metadata extraction
│       ├── engagement_goals.yaml      # Phase 2: goal extraction and segmentation
│       ├── engagement_agenda_build.yaml  # Phase 3: agenda table with speakers and time slots
│       └── engagement_agenda_publish.yaml # Phase 4: Word doc creation via python-docx
├── test-client/           # Console REPL test client — simulates remote sender via Redis
│   ├── chat.py               # Push to inbox, read from outbox, request-response correlation
│   └── requirements.txt      # redis, redis-entraid, azure-identity, python-dotenv
├── scripts/
│   ├── start.ps1          # Start the assistant (detached, via pythonw.exe)
│   ├── stop.ps1           # Stop all running instances
│   └── autostart.ps1      # Install/uninstall auto-start at Windows login
├── experimental/
│   └── test_graph_calendar.py  # Microsoft Graph calendar API test script
├── user-stories/          # Planning documents for task queue and Redis bridge features
├── favicon.svg            # App icon (SVG) — inline in HTML
├── agent_icon.png         # App icon (PNG) — toast notifications
└── agent_icon.ico         # App icon (ICO) — taskbar
```

---

## Getting Started

### Prerequisites

- **Windows 11** laptop
- **Python 3.12+** with a virtual environment
- **WorkIQ CLI** installed and on PATH (or path set in `.env`)
- **Azure OpenAI** resource with `gpt-5.2` and `gpt-5.4-mini` model deployments
- **Azure Communication Services** resource for sending email invites
- **Azure Managed Redis** (optional) — for remote task delivery via Teams. Requires Entra ID authentication (passwordless, no API keys).

### Installation

```powershell
# Clone the repository
git clone <repo-url>
cd hub-cowork

# Create virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Configure environment
copy .env.example .env
# Edit .env with your Azure endpoints, model names, tenant ID, and ACS settings
```

### Running the App

#### From PowerShell (recommended)

```powershell
# Start (runs invisibly in the background)
.\scripts\start.ps1

# Stop
.\scripts\stop.ps1
```

#### Without VS Code

The app does not require VS Code. To run it directly from any PowerShell or Command Prompt:

```powershell
# Start invisibly (no console window)
Start-Process -FilePath .\.venv\Scripts\pythonw.exe -ArgumentList "meeting_agent.py" -WorkingDirectory (Get-Location) -WindowStyle Hidden

# Or for debugging (with console output)
.\.venv\Scripts\python.exe meeting_agent.py
```

#### Auto-Start at Windows Login

```powershell
# Install auto-start (creates a VBScript in the Windows Startup folder)
.\scripts\autostart.ps1 install

# Remove auto-start
.\scripts\autostart.ps1 uninstall
```

This places a `HubCowork.vbs` launcher in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`, which starts the agent silently at every Windows login.

---

## Testing Remote Task Delivery with the Test Client

The `test-client/` folder contains a **console REPL** that simulates a remote sender (like a Teams relay service) by talking to the agent through the same Azure Managed Redis streams. This lets you validate the full remote-task pipeline — inbox delivery, task queue processing, outbox response — without deploying the companion cloud application.

### Prerequisites

- The agent must be **running** (via `.\scripts\start.ps1`)
- `AZ_REDIS_CACHE_ENDPOINT` must be set in `.env`
- The test client reuses the agent's `.env` (loaded from the parent directory) and its saved auth record from `~/.hub-cowork/auth_record.json`

### Running the test client

```powershell
# From the project root (uses the same .venv as the agent)
.\.venv\Scripts\Activate.ps1
python test-client\chat.py
```

On startup, the test client:

1. **Authenticates** — Reuses the agent's cached Entra ID auth record for silent token acquisition
2. **Connects to Redis** — Same Azure Managed Redis cluster as the agent, with `redis-entraid` credential provider
3. **Checks agent status** — Reads `workiq:agents:{email}` to verify the agent is online and shows agent info
4. **Enters the REPL** — Prompts `You >` for input

### What to test

| Test | What happens |
|---|---|
| Type `hello` | Message pushed to `workiq:inbox:{email}` → agent picks it up → router handles directly as small talk (non-queued) → response appears in the test client console AND the agent's local chat UI shows a purple "remote" bubble |
| Type a business query (e.g., `summarize my recent emails`) | Message queued as a business task → agent processes it → response written to `workiq:outbox:{email}` → test client displays the result |
| Send a second request while the first is running | The second task queues at position 2. The test client blocks waiting for its specific `in_reply_to` correlation match. |
| Ask `what is the status of my request?` from the **local chat UI** while a remote task runs | Responds immediately with progress milestones (bypasses queue via `task_status` skill) |

### How it works

```
  test-client (console)              Azure Managed Redis              WorkIQ-Hub-SE-Agent
  ──────────────────────             ────────────────────             ──────────────
        │                                    │                              │
        │── XADD inbox:{email} ─────────────►│                              │
        │   {sender, text, msg_id}           │                              │
        │                                    │◄──── XREAD inbox:{email} ────│
        │                                    │      (5s blocking poll)      │
        │                                    │                              │
        │                                    │      ExecutorPool.submit()   │
        │                                    │      skill execution...      │
        │                                    │                              │
        │                                    │◄──── XADD outbox:{email} ────│
        │                                    │      {task_id, status, text, │
        │◄── XREAD outbox:{email} ────────── │       in_reply_to: msg_id}   │
        │    match in_reply_to == msg_id     │                              │
        │                                    │                              │
        │    print response                  │                              │
```

The `msg_id` → `in_reply_to` correlation ensures the test client matches each response to its original request, even when multiple messages are in flight.

### Exiting

Press **Ctrl+C** or type `exit` to disconnect cleanly.

---

## Configuration

All configuration is in the `.env` file:

| Variable | Description |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_CHAT_MODEL` | Full model for router + complex workflows (e.g., `gpt-5.2`) |
| `AZURE_OPENAI_CHAT_MODEL_SMALL` | Mini model for Q&A + general responses (e.g., `gpt-5.4-mini`) |
| `AZURE_OPENAI_API_VERSION` | API version (e.g., `2025-03-01-preview`) |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |
| `ACS_ENDPOINT` | Azure Communication Services endpoint |
| `ACS_SENDER_ADDRESS` | Verified sender email address for ACS |
| `AZ_REDIS_CACHE_ENDPOINT` | (Optional) Azure Managed Redis endpoint (`host:port`). Enables remote task delivery. |
| `REDIS_SESSION_TTL_SECONDS` | (Optional) Agent presence TTL in seconds (default: `86400`) |
| `AGENT_TIMEZONE` | (Optional) IANA timezone override (auto-detected if omitted) |
| `WORKIQ_PATH` | (Optional) Full path to WorkIQ CLI if not on PATH |

**Redis is optional.** If `AZ_REDIS_CACHE_ENDPOINT` is not set, the agent runs in local-only mode — all features work except remote task delivery.

---

## Dependencies

| Package | Purpose |
|---|---|
| `openai` | Azure OpenAI Responses API client |
| `azure-identity` | Azure AD authentication with persistent token cache |
| `azure-communication-email` | Sending calendar invites via ACS |
| `python-dotenv` | Loading `.env` configuration |
| `pywebview` | Native desktop window for the chat UI |
| `websockets` | WebSocket server for UI ↔ backend communication |
| `winotify` | Windows 10/11 native toast notifications |
| `pyyaml` | YAML parsing for skill definitions |
| `tzlocal` | Auto-detection of the system timezone |
| `python-docx` | Word document creation for agenda publishing |
| `redis` | Redis client (cluster mode support) |
| `redis-entraid` | Entra ID credential provider for passwordless Redis authentication |

---

## System Tray Icon

The agent places a persistent icon in the Windows system tray (notification area) so it can be summoned without remembering a keyboard shortcut.

### User interaction

| Action | Result |
|---|---|
| **Left-click** the tray icon | Show or hide the chat window |
| **Right-click** the tray icon | Context menu: **Show / Hide**, **Quit** |
| **Click a toast notification** | Opens the chat window (via the local HTTP endpoint) |
| **Remote message from Teams** | Task completes → window is shown automatically |

### Why not `pystray` or `pynput`?

- **`pystray`** requires the main thread's message loop and conflicts with `pywebview`, which also requires the main thread on Windows. In testing the tray icon never appeared.
- **`pynput`** installs a low-level global keyboard hook (`SetWindowsHookEx`) that intercepts every keystroke. If the Python process is slow (GIL contention from multiple threads), the hook stalls the Windows input pipeline — freezing both keyboard and mouse system-wide. This was the primary cause of severe input lag observed in earlier versions.

### Implementation: raw Win32 via `ctypes`

The system tray is implemented in `tray_icon.py` using direct Win32 API calls through Python's built-in `ctypes` module — **zero extra dependencies**.

**How it works:**

1. **Background thread** — The tray icon runs in its own daemon thread (`tray-icon`) with its own Win32 message pump (`GetMessageW` loop). This avoids conflicts with pywebview's main-thread event loop.

2. **Hidden window** — A hidden message-only window (`CreateWindowExW`) is created to receive tray icon callback messages (`WM_TRAYICON`). This is standard Win32 practice — the tray icon needs an `HWND` to send notifications to.

3. **`NOTIFYICONDATAW` struct** — The full Vista+ layout of the structure is defined (976 bytes), including all fields through `hBalloonIcon`. Earlier attempts with a minimal struct (`cbSize` too small) caused `Shell_NotifyIconW` to silently fail on modern Windows.

4. **`WNDCLASSW` struct** — Defined locally because `ctypes.wintypes` does not include it. A `WNDPROC` callback handles `WM_TRAYICON` (left/right-click), `WM_COMMAND` (menu selections), and `WM_DESTROY` (cleanup).

5. **Prevent callback GC** — The `WNDPROC` C function pointer is stored as `self._wndproc_ref` on the `TrayIcon` instance to prevent garbage collection. Without this pin, Python's GC would free the callback while the Win32 message loop still references it, causing a crash.

6. **Icon loading** — Uses `LoadImageW` with `LR_LOADFROMFILE` to load `agent_icon.ico` directly. Falls back to the default application icon (`IDI_APPLICATION`) if the `.ico` file is missing.

7. **Context menu** — `CreatePopupMenu` + `TrackPopupMenu` for the right-click menu. `SetForegroundWindow` is called first (required by Windows so the menu dismisses when clicking elsewhere).

### Key Win32 APIs used

| API | Purpose |
|---|---|
| `Shell_NotifyIconW(NIM_ADD, ...)` | Add the icon to the system tray |
| `Shell_NotifyIconW(NIM_DELETE, ...)` | Remove it on shutdown |
| `RegisterClassW` / `CreateWindowExW` | Hidden window for message routing |
| `GetMessageW` / `DispatchMessageW` | Message pump in the background thread |
| `LoadImageW` | Load `.ico` from disk |
| `CreatePopupMenu` / `TrackPopupMenu` | Right-click context menu |
