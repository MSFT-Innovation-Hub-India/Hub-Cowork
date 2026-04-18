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
