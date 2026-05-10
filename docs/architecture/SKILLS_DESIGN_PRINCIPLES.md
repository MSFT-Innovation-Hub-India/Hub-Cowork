# Hub Cowork — Architecture & Skills Design Principles

> **Status:** Authoritative. This document defines the non-negotiable design principles for **the entire Hub Cowork architecture** — skills, tools, tool packaging, skill discovery, routing, the agent loop, conversation state, auth sharing, and progress streaming. Every new feature, skill, tool, or runtime change MUST honor these principles in spirit. If a proposed change conflicts with this document, fix the change — don't bend the rules.
>
> This is Hub Cowork's "skills-based economy" charter, adapted from the Anthropic Claude Cowork philosophy ([anthropics/knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins)) to a **local-first, Azure-OpenAI-Responses-API-driven Python runtime.** Where we deliberately diverge from Cowork (per-skill tool scoping, per-skill model tier, single-Entra credential, two-call routing), §18 documents each divergence and why.

The doc is structured in three parts:

- **Part I (§1–§10) — The three layers.** What goes where: model vs. skill vs. tool. The non-negotiables, banned patterns, and the §9 checklist.
- **Part II (§11–§17) — Runtime mechanics.** How tools are packaged and discovered, how skills are loaded, how routing works, the exact shape of the agent loop, how conversation state flows, how auth is shared, how progress streams.
- **Part III (§18) — Deliberate divergences from Cowork.** What we do differently and why each divergence is justified.

---

# Part I — The three layers

## 1. Core philosophy — three roles, three layers

Every capability in Hub Cowork is split across three layers. Mixing concerns across layers is the single most common design failure, and the one this document exists to prevent.

| Layer | Owns | Lives in |
|---|---|---|
| **The model** (Azure OpenAI Responses API) | All orchestration — tool selection, sequencing, conversation, HITL turns, error communication | The Responses API server-side loop |
| **Skills** (domain expertise) | Judgment, heuristics, "when / why / what-if" — the senior-engineer knowledge that makes the skill valuable | `SKILL.md` (markdown) |
| **Tools** (mechanical work) | Data retrieval, transformation, side effects — deterministic, testable Python | `tools/<name>.py` |

The runtime (`agent_core.py`) is **wiring**, not intelligence. It loads skills, configures the API call, bridges local tool execution, and manages thread state. It makes zero decisions about workflow flow.

---

## 2. Skills carry expertise, not procedure

A skill's `SKILL.md` describes **what makes the work valuable** — the institutional knowledge a senior solution engineer has and a junior one doesn't. It does **not** describe the mechanical steps to run a workflow.

### ✅ Belongs in `SKILL.md`

- Domain heuristics: *"For ADS engagements, always include a Day 2 Recap session."*
- Conditional judgment: *"If the customer mentioned compliance concerns in the briefing, add a security architecture deep-dive."*
- Engagement-type rules: *"BUSINESS_ENVISIONING vs SOLUTION_ENVISIONING is decided by whether technical depth is in scope."*
- Communication tone and confirmation patterns: *"Always confirm the briefing call selection with the user before publishing."*
- High-level workflow phases (3–6 short bullets, not numbered procedure).

### ❌ Does NOT belong in `SKILL.md`

- Numbered step-by-step procedures (`STEP 1 — call tool X with arg Y`).
- String-formatting recipes (`build filename as Agenda-<Customer>-<Month>-<Timestamp>.docx`).
- Control-flow markers as state machines (`[STOP_CHAIN]`, `[AWAITING_CONFIRMATION]`) — these are runtime hacks substituting for natural conversation.
- Tool-result parsing instructions (`extract the link after "Open link (markdown):"`).
- Validation logic (`if X is missing, output exact string Y`).
- Anything that would make sense as code in a Python function.

**The test:** if you can replace a paragraph of `SKILL.md` with a deterministic Python function and the workflow still produces the same outcome, that paragraph belongs in a tool — not the skill.

---

## 3. Tools return facts, not interpretations

A tool does **one thing**, returns **structured facts**, and does **not** decide how to communicate with the user. The model decides communication.

### ✅ A good tool

```python
def handle(arguments: dict, **kwargs) -> str:
    customer = arguments["customer_name"]
    path = _context_path(customer)
    if not path.exists():
        return json.dumps({"found": False, "customer": customer})
    data = json.loads(path.read_text())
    return json.dumps({
        "found": True,
        "customer": customer,
        "has_agenda": "agenda_markdown" in data,
        "metadata": data.get("metadata", {}),
        "agenda_markdown": data.get("agenda_markdown", ""),
    })
```

### ❌ A bad tool (interpretation in the tool)

```python
if not path.exists():
    return f"No agenda context found for '{customer}'. Run the agenda build first."
```

The bad version hardcodes a user-facing sentence. The model can no longer say *"I didn't find context for 'Contso' — did you mean 'Contoso'?"* because the tool already chose the response. Tools surface **what is true**; the model decides **what to say**.

### Tool responsibilities

| ✅ In tools | ❌ Not in tools |
|---|---|
| Fetch from disk / API / CLI / DB | Decide what to tell the user |
| Parse / format / transform data | Decide whether to ask the user vs. retry |
| Write file, send email, create invite | Decide order of operations |
| Filename generation, time-slot math, table building | Map intent to tool selection |
| Return structured JSON with `status`, `found`, `path`, etc. | Hardcode error sentences for the user |

---

## 4. The model is the orchestrator — runtime is wiring

The Azure OpenAI Responses API natively supports multi-step tool-call orchestration. **`agent_core.py` MUST NOT duplicate this.** Its loop is mechanical, not intelligent.

### ✅ What `agent_core.py` does

1. Route the user's message to a skill (cheap mini-model classification call).
2. Load that skill's config (`skill.yaml`) and instructions (`SKILL.md`).
3. Call the Responses API with `instructions = SKILL.md`, `tools = skill's tool subset`, `previous_response_id = thread state`.
4. While the API requires action, execute the named local tool, send the result back. **No interpretation. No decisions.**
5. When the API returns a final text response (or pauses for user input), surface it to the UI.
6. Persist `previous_response_id` on the thread for the next turn.

### ❌ What `agent_core.py` MUST NOT do

- Parse the model's text response for control-flow markers (`[STOP_CHAIN]`, `[AWAITING_CONFIRMATION]`).
- Decide which skill to chain to next based on a `next_skill` YAML field.
- Maintain a state machine across skill transitions.
- Mutate, summarize, or interpret tool results before returning them to the API.
- Hold per-skill conversation history outside the Responses API's `previous_response_id` chain.

### HITL is a conversation turn, not a state machine

When the model needs user input, it just says so. Its message ends with a question. The runtime sees a final text response (no pending tool calls), surfaces it, parks the thread at `awaiting_user`, and waits. When the user replies, we call the API with `previous_response_id` and the new input. The API resumes naturally.

There are **no `[AWAITING_CONFIRMATION]` markers**. The thread status (`running` → `awaiting_user` → `running`) is sufficient signal.

### Errors are conversation, not control flow

When a tool returns `{"found": false, ...}`, the model decides whether to ask the user, suggest a fix, or stop. There is no `[STOP_CHAIN]` marker. If the model stops, it stops by sending a final text message — same as a successful completion.

---

## 5. One skill per workflow, not one skill per phase

Long workflows (such as the multi-phase agenda creation) are **one skill** with a richer tool set, not several chained skills.

### Why

- Procedural code does not belong in instructions. Once procedure moves to tools, the remaining domain knowledge fits comfortably in a single `SKILL.md` of ~100–150 lines.
- `previous_response_id` carries phase-to-phase context server-side. There is no need for an on-disk JSON handoff between phases.
- A single skill has no `next_skill`, no control-flow markers, no inter-phase context store, and no runtime state machine to tie them together.

### The skill instructions describe phases, the tools execute them

```markdown
## Workflow
1. Find the briefing call(s) for the customer. Confirm with the user.
2. Retrieve and analyze the meeting notes.
3. Generate the engagement agenda.
4. Publish the agenda as a Word document.
```

That's it. The model decides which tool to call at each phase, when to ask the user, when to stop on error. The tools (`find_briefing_calls`, `retrieve_briefing_notes`, `build_agenda`, `publish_agenda_doc`) do the deterministic work.

---

## 6. File layout

Every skill is a folder with two required files plus optional skill-local tools:

```
src/hub_cowork/skills/<group_or_name>/
├── skill.yaml           # Runtime config (loader-readable)
├── SKILL.md             # Domain expertise (model-readable system prompt)
└── tools/               # Optional skill-local tools
    ├── __init__.py
    └── <tool_name>.py
```

### `skill.yaml` — runtime config only, no prose

```yaml
name: engagement_agenda
description: >
  Create an Innovation Hub engagement agenda end-to-end — from briefing call
  discovery through Word document publication. Keywords: agenda, engagement,
  briefing, innovation hub, customer engagement, agenda document.
model_tier: reasoning            # reasoning | fast
reasoning_effort: medium         # low | medium | high (only when model_tier=reasoning)
queued: true                     # serialize across thread executor
tools:
  - query_workiq
  - find_briefing_calls
  - retrieve_briefing_notes
  - build_agenda
  - publish_agenda_doc
  - log_progress
```

**That's the entire YAML.** Anything else (instructions, control markers, chaining, conditionals, transformation rules) is a smell.

### Banned fields

- `instructions:` — moved to `SKILL.md`.
- `next_skill:` — workflows are single skills.
- `conversational:` — every skill is conversational; HITL is a conversation turn.

### `SKILL.md` — markdown system prompt

```markdown
# Engagement Agenda

You help Innovation Hub Solution Engineers create customer engagement agendas
end-to-end, from briefing-call discovery to Word-document publication.

## Workflow
1. Find the relevant briefing call(s) for the customer and confirm the
   selection with them.
2. Retrieve and analyze the meeting notes.
3. Build the agenda using the customer's goals and the engagement type.
4. Publish the agenda as a Word document.

## Engagement-type judgment
- **ADS** — architecture/solution review …
- **BUSINESS_ENVISIONING** vs **SOLUTION_ENVISIONING** — depends on whether
  technical depth is in scope …
- **HACKATHON** — multiple teams …

## Important
- Always confirm the briefing-call selection before proceeding.
- For ADS engagements, always include a Day 2 Recap session.
- If the customer mentioned compliance concerns, add a security
  architecture deep-dive and assign it to the security track lead.

## Reporting progress
Call `log_progress` after each major milestone so the user sees real-time
updates in the UI. Do not call it for internal bookkeeping steps.
```

Read the test: a senior solution engineer should be able to read `SKILL.md`, nod, and say *"yes, this is how I'd brief a junior."* If it reads like a runbook a robot follows, it's wrong.

---

## 7. Tool design rules

1. **One tool, one job.** `publish_agenda_doc` does not also load context. A context loader does not also build filenames.
2. **Inputs are explicit.** The tool takes everything it needs as arguments. It does not reach into shared state.
3. **Outputs are structured.** Return JSON with named fields (`{"found": bool, "path": str, "open_link": str, ...}`). Avoid prose-only returns when structure is possible.
4. **Errors are facts.** Return `{"status": "error", "reason": "<machine-readable>"}`, not a user-facing apology. The model writes the apology.
5. **Side effects are visible.** Tools that write files, send email, or hit APIs include `path` / `message_id` / `request_id` in the result so the model can confirm to the user.
6. **No chained tool wrappers.** A tool does not call another tool internally. Composition is the model's job. (Exception: pure helper functions inside a single tool's module — those are fine.)
7. **Progress reporting is via `on_progress` callback**, never via printing or returning prose updates.

---

## 8. MCP is the tool transport — stdio now, HTTP later

Tools are **MCP servers**, not loose Python modules. This matches how Claude Cowork ships tools and gives us a clean upgrade path from local stdio to remote HTTP without changing skills or runtime logic.

The shape:

- **Each skill that owns tools ships a per-skill MCP server** living next to its `SKILL.md` and `skill.yaml` (`src/hub_cowork/skills/<skill>/mcp_server/`). The server is a small Python module exposing that skill's tools.
- **Cross-cutting tools live in shared MCP servers** under `src/hub_cowork/mcp_servers/` (e.g., `workiq`, `m365`, `progress`). Any skill can list a shared server in its `skill.yaml`.
- **All MCP servers run as stdio subprocesses spawned by the host process.** The user never starts them manually — `python -m hub_cowork` lifts them. From the user's perspective, nothing changes.
- **A single shared MCP client pool inside `agent_core` manages lifecycle** — one subprocess per server name, lazy-spawned on first use, kept warm for the lifetime of the host, gracefully torn down on shutdown. **Calls to the same server are multiplexed over one connection — never one-subprocess-per-tool-call.**
- **Auth crosses the process boundary via the shared on-disk MSAL cache.** MCP server subprocesses call `get_credential()` themselves and get silent-refresh tokens from the same `~/.hub-cowork/` cache the host wrote. No token marshalling over the MCP transport.
- **Future move to Azure Container Apps is a transport swap, not a rewrite.** The MCP protocol is transport-agnostic — flipping a server from stdio to streamable-HTTP/SSE means changing one entry in `skill.yaml` (`transport: stdio` → `transport: http`, plus URL) and deploying the same server module behind a container. Skill code, tool code, and the agent loop are unchanged.

This replaces the older "local Python tools, no MCP" stance from earlier drafts. The MCP runtime cost is acceptable because (a) the host owns the subprocess pool — users see one process to launch, (b) subprocess spawn is amortized across the whole conversation, and (c) it future-proofs the move to ACA-hosted tools.

See §11 for the per-server packaging rules, §11A for shared servers, and §18.5 for what we still keep different from Cowork.

---

## 9. The non-negotiables — quick checklist

When reviewing a PR, asking yourself "does this honor the design?", run through this list.

**Skills & tools (Part I):**

- [ ] Skill instructions live in `SKILL.md`, not in `skill.yaml`.
- [ ] `skill.yaml` contains only runtime config (`name`, `description`, `tools`, `model_tier`, `reasoning_effort`, `queued`).
- [ ] Skill instructions read like senior-engineer briefing notes, not a numbered runbook.
- [ ] No `[STOP_CHAIN]` or `[AWAITING_CONFIRMATION]` markers in instructions.
- [ ] No `next_skill` chains. Multi-phase workflows are a single skill.
- [ ] Every "if X then exact-string-Y" instruction has been moved into a tool that returns structured facts.
- [ ] Tools return JSON facts; they do not author user-facing prose.

**Tool packaging — MCP servers (§11, §11A):**

- [ ] Tool lives in an MCP server (per-skill `mcp_server/` or shared `mcp_servers/<name>/`).
- [ ] Registered with `@mcp.tool(...)`; `description` tells the model *when* to call it, not *how*.
- [ ] Returns JSON-serializable structured facts with a `status`/`found` discriminator.
- [ ] Does not call other tools (within or across servers). Composition is the model's job.
- [ ] Credentials via `get_credential()` inside the tool function. Never re-instantiated, never accepted as input.
- [ ] Progress via MCP notifications (forwarded to `on_progress` by the pool). Never via prints or return prose.
- [ ] No top-level side effects at module import.
- [ ] Server is spawned and reused by the host's `MCPClientPool`; never spawn-per-call.

**Skill loading (§12):**

- [ ] Skill is a folder containing `skill.yaml` + `SKILL.md` (and optional `tools/`).
- [ ] Skill is auto-discovered — no central registry, no decorator, no manifest.
- [ ] `description` is rich enough for the router to match it (intent verbs + trigger phrases).

**Routing (§13) and the agent loop (§14):**

- [ ] Routing is one cheap fast-model call that returns a skill name or `"none"`.
- [ ] Router does not see tool definitions.
- [ ] `agent_core` does not parse model output for control flow.
- [ ] `agent_core`'s tool-execution loop makes zero decisions about what to do next — it just executes what the API requested.
- [ ] HITL works as natural conversation turns — the model asks a question, the runtime parks the thread, the user's reply resumes via `previous_response_id`.

**State, auth, progress (§15–§17):**

- [ ] No `thread.messages`, no per-skill scratchpad, no inter-phase JSON store. `previous_response_id` is the only conversation context.
- [ ] Tools that need auth call `get_credential()` — never construct credentials.
- [ ] Progress flows through `on_progress(kind, message)`, never via prints or return prose.

If any box is unchecked, the design is wrong. Fix the design, then merge.

---

## 9a. Pre-change reasoning gate (mandatory)

Before writing or modifying any skill, tool, or runtime code, run this gate:

> **"Does this change violate any of §1–§8, or any item in §9?"**

If yes — **stop, redesign, and only then implement.** Do not ship a violation with a TODO. State the conflict explicitly in your plan and propose the compliant alternative.

The most common violations to catch up front:

- Putting procedure / string-formatting / numbered steps into `SKILL.md` → move to a tool.
- Putting `instructions:`, `next_skill:`, `conversational:`, or any prose into `skill.yaml` → move to `SKILL.md`.
- Tool returns a user-facing sentence → return structured JSON facts.
- Adding `[STOP_CHAIN]` / `[AWAITING_CONFIRMATION]` markers → use thread-status flag instead.
- `agent_core` deciding *what* to do next → that's the model's job.
- Chaining skills via `next_skill` or passing state via on-disk JSON → collapse into one skill, use `previous_response_id`.

## 9b. Preserved subsystems (do not refactor without explicit ask)

The following are core load-bearing components of the system. Do not refactor them unless the user explicitly asks.

- **Authentication / credential management** — `core/auth_credential.py` with the WAM broker (`InteractiveBrowserBrokerCredential` parented to pywebview HWND, classic fallback), shared-credential pattern via `set_credential()`/`get_credential()`, `AuthenticationRecord` silent-refresh persistence, MSAL cache naming. Battle-tested across Graph / Azure OpenAI / FoundryIQ / Fabric / ACS scopes. (See §16.)
- **Per-conversation concurrency model** — multiple chat threads run independently and in parallel:
  - `core/thread_manager.py` — thread-safe registry of `ConversationThread` objects, observer pattern for UI broadcast, `current_thread_id` `ContextVar` so logs and progress events route to the right panel.
  - `core/thread_executor.py` (`ExecutorPool`) — one daemon worker thread per active conversation, with idle shutdown. The user can run an agenda build in thread A while asking a Q&A question in thread B; each thread has its own Responses-API context (its own `previous_response_id`).
  - `core/conversation_thread.py` — the dataclass that carries id, status (`running` / `awaiting_user` / `complete` / `failed`), `previous_response_id`, `progress_log`, `code_log`, `hitl_correlation_tag`, `source` (local vs Teams).
  - `core/thread_store.py` — `LocalJsonThreadStore` with debounced atomic writes under `~/.hub-cowork/threads/{active,archive}/`. Threads survive restart and resume from `previous_response_id`.
  - `host/redis_bridge.py` — Teams remote-message inbox/outbox bridge with the per-Teams-user in-flight gate, classifier, and `#thread-xxxx` correlation. Orthogonal to skills.
- **Desktop host + UI integration** — `host/desktop_host.py` (WebSocket on 18080, HTTP on 18081, pywebview window, tray wire-up) plus `assets/chat_ui.{html,js,css}`. The full WebSocket protocol (client→server: `create_thread`, `send_to_thread`, `cancel_thread`, `system_query`, …; server→client: `thread_started`, `thread_progress`, `thread_completed`, `thread_archived`, `service_status`, `auth_status`, …) and the request-id correlation scheme are frozen. Changes to the agent loop are **server-side only.**
- **Per-skill model-tier routing** (`reasoning` vs `fast` with `reasoning_effort`) — a deliberate strength over Cowork's single-model approach (§18.3).
- **Settings UI + env override mechanism** — `_env_overrides` in `~/.hub-cowork/hub_config.json`, applied in `__main__.py` before `agent_core` import, `restart` WS command for relaunch.
- **Service-status broadcast** (`core/service_status.py`) — passive reachability tracking for `workiq` / `foundryiq` / `fabric_agent` / `redis_teams`, surfaced to the UI as `service_status` events.

### Contract — where the MCP layer plugs in without disturbing any of the above

The MCP layer (§11, §11A) is a **drop-in dispatcher inside the agent loop.** It does not touch the layers above:

- `ExecutorPool` still hands a `(thread_id, user_input)` to `agent_core.run_agent_on_thread(...)`. Unchanged signature.
- `agent_core` still owns the Responses-API call, the `requires_action` loop, and the per-thread `previous_response_id` write-back. Unchanged.
- The only thing that changes inside the loop is *how a tool call is fulfilled:* `mcp_pool.call(server, name, args, on_progress=...)` dispatches into the warm subprocess. The `on_progress` callback the pool receives is the same one already wired by `_ThreadWorker` — and it's still tagged via `current_thread_id` so events land in the right chat panel.
- Tool-emitted MCP `notifications/progress` are forwarded straight into that `on_progress`, so the UI's `thread_progress` stream is byte-for-byte the same as today.
- `MCPClientPool` is **host-scoped, not thread-scoped.** One subprocess per server, multiplexed across all threads. A long-running agenda build in thread A and a fast Q&A in thread B share the same warm `workiq` server subprocess concurrently — the pool serializes calls per server only when the underlying MCP session requires it (typically not, since each call is a discrete request/response with a unique id).
- Thread cancellation, archive/unarchive, and the `awaiting_user` HITL pattern continue to work as today: the loop exits, the thread state machine takes over, and the next user message resumes via `previous_response_id`. The pool sees nothing special — the next `pool.call(...)` simply happens whenever the model asks for it.

In short: **conversation threading, UI integration, persistence, and the Teams bridge are below the agent loop. The MCP dispatcher lives inside the loop. The seam is clean.**

---

# Part II — Runtime mechanics

> Part I established **what** belongs in each layer. Part II nails down **how** the runtime wires those layers together. Every rule here is binding.

## 11. Tool packaging — MCP servers

Tools are packaged as **MCP servers**. Each server is a small Python module that uses the official `mcp` SDK (FastMCP) to expose one or more tools over stdio. The host spawns these servers as subprocesses; the model sees them as ordinary function tools through the Responses API.

### 11.1 Per-skill MCP servers

A skill that owns tools ships them as a server inside the skill folder:

```
src/hub_cowork/skills/engagement_agenda/
├── skill.yaml
├── SKILL.md
└── mcp_server/
    ├── __init__.py
    ├── __main__.py            # python -m hub_cowork.skills.engagement_agenda.mcp_server
    ├── server.py              # builds the FastMCP app and registers tools
    └── tools/
        ├── find_briefing_calls.py
        ├── retrieve_briefing_notes.py
        ├── build_agenda.py
        └── publish_agenda_doc.py
```

`server.py` is mechanical — it constructs `FastMCP(name="engagement_agenda")`, imports each tool module, and registers them. Tool modules expose pure functions decorated with `@mcp.tool(...)`:

```python
# tools/find_briefing_calls.py
from mcp.server.fastmcp import FastMCP

def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="find_briefing_calls",
        description=(
            "Search WorkIQ for briefing calls related to a customer. "
            "Returns structured meeting metadata (title, date, organizer, "
            "participants, kind=internal|external). Use whenever the user "
            "asks to start an engagement agenda."
        ),
    )
    def find_briefing_calls(customer: str) -> dict:
        """Returns a JSON-serializable dict of facts."""
        ...
```

Rules:

- **`description` is the model's only hint about when to call this tool** — write it like a Cowork MCP tool description (intent-rich, action-oriented, names the trigger context).
- **Return JSON-serializable structured facts.** Never user-facing prose. Never raw exceptions — convert to `{"status": "error", "reason": "<machine-readable>"}`.
- **No top-level side effects** at import time. Auth, file I/O, and network happen inside the tool function, not at module load.
- **Tools never call other tools.** Composition is the model's job. (Pure helpers inside the same server module — fine. Reaching across MCP servers — never.)

### 11.2 Shared (cross-cutting) MCP servers

Tools used by more than one skill live in their own server under `src/hub_cowork/mcp_servers/`:

```
src/hub_cowork/mcp_servers/
├── workiq/                    # query_workiq, resolve_speakers, get_task_status
│   ├── __main__.py
│   ├── server.py
│   └── tools/
├── m365/                      # send_email, create_word_doc, share_onedrive_document
│   ├── __main__.py
│   └── ...
└── progress/                  # log_progress, get_hub_config
    └── ...
```

Each shared server is its own subprocess with its own connection. A skill that needs both shared and local tools simply lists each server in `skill.yaml` (§12.2).

### 11.3 `skill.yaml` declares which MCP servers a skill connects to

```yaml
name: engagement_agenda
description: |
  Build a customer engagement agenda end-to-end…
model_tier: reasoning
reasoning_effort: medium
mcp_servers:
  - workiq            # shared server (resolved to src/hub_cowork/mcp_servers/workiq)
  - m365              # shared
  - progress          # shared
  - .                 # the skill's own mcp_server/ folder
tool_allowlist:       # optional — restrict to a subset of tools the listed servers expose
  - find_briefing_calls
  - retrieve_briefing_notes
  - build_agenda
  - publish_agenda_doc
  - send_email
  - log_progress
```

The runtime spawns (or reuses) one subprocess per listed server, lists each server's tools via MCP `tools/list`, applies `tool_allowlist` if present, and presents the union to the Responses API as ordinary function tools.

### 11.4 Lifecycle — the host owns it

A single `MCPClientPool` lives in `agent_core` and is responsible for:

- **Lazy spawn:** the first time a skill needs server `S`, the pool starts `python -m hub_cowork.<path-to-S>` as a stdio subprocess and completes the MCP handshake.
- **One subprocess per server, multiplexed:** every subsequent call to any tool on `S` reuses the same subprocess and the same MCP session. **Never spawn-per-call.**
- **Warm for the host's lifetime:** subprocesses are kept alive until shutdown. (Optional idle-eviction is allowed but not required.)
- **Graceful shutdown:** on host exit, the pool sends MCP `shutdown` and waits briefly before SIGTERM/SIGKILL.
- **Crash recovery:** if a server subprocess dies mid-call, the pool surfaces it as a tool error (`{"status": "error", "reason": "mcp_server_crashed"}`) and respawns on the next call.

The pool is a **mechanical bridge.** It does not decide which tools to call, does not interpret tool results, does not parse server output for control flow.

### 11.4a Stdio MCP servers are NOT passed to the Responses API — they go through function calling

This is the most important runtime constraint and the reason the `MCPClientPool` exists.

Azure OpenAI's Responses API supports a native **`mcp` tool type** — but **only for remote MCP servers reachable by URL** (streamable HTTP / SSE). The model server connects to those itself, lists their tools, and dispatches calls cloud-to-cloud. A local stdio subprocess on the user's machine is unreachable from Azure and cannot be registered as a Responses-API `mcp` tool.

Therefore, while we run stdio:

1. **At skill-start, the host calls MCP `tools/list` on every server the skill connects to.** The pool collects every tool's name, description, and input schema.
2. **Each MCP tool is registered with the Responses API as an ordinary function tool** (`type: "function"`) in the `tools=[...]` payload of `responses.create(...)`. The function name is `<server>__<tool>` (or just `<tool>` when unambiguous); description and parameters come straight from the MCP advertisement.
3. **When the model returns `requires_action`,** the agent loop looks up which server owns the requested tool, calls `pool.call(server, tool_name, arguments)`, awaits the JSON result, and submits it as a tool output. The Responses API never knows MCP exists on the wire — from its perspective every call is a function call.
4. **MCP `notifications/progress`** received from the server during the call are forwarded to `on_progress(...)` in real time; they do not flow through the Responses API.

The net effect: the model sees the same JSON-schema function tools it sees today; the only thing that changes is *who dispatches the call* (the pool, into a warm stdio subprocess) and *how the tool is implemented* (a `@mcp.tool` function in a server module instead of a loose `handle()`).

When a server later moves to ACA-hosted streamable HTTP (§11.7), we get a choice per server:

- **Keep it client-dispatched** — the pool grows an HTTP branch alongside its stdio branch, and the agent loop is unchanged. Recommended default; preserves auth (§11.5), progress, and error handling on our side.
- **Hand it to the Responses API as a native `mcp` tool** — declare `transport: streamable_http` + `url:` + auth headers in `skill.yaml`, and `agent_core` registers it as `{type: "mcp", server_url: ...}` instead of as function tools. The model server then handles the call cloud-to-cloud. Use only when the server is fully cloud-resident, the auth shape fits Responses' MCP auth, and we don't need progress streaming through `on_progress`.

Until the second mode is needed, **the pool is the only path tool calls take.** No bypass.

### 11.5 Auth across the process boundary

MCP server subprocesses do **not** receive credentials over the wire. They call `get_credential()` from `core/auth_credential.py` themselves. Because the MSAL cache is on disk under `~/.hub-cowork/` and the cache name is shared (`hub_cowork`), the subprocess silently refreshes tokens from the same cache the host populated at sign-in.

Rules for MCP servers:

- ✅ Call `get_credential()` at the top of any tool function that needs an Entra token.
- ✅ Trust the cache. If the cache is empty, surface `{"status": "error", "reason": "not_signed_in"}` — do not attempt interactive auth from a subprocess.
- ❌ Never construct credentials. Never call `DefaultAzureCredential`. Never shell out to `az`.
- ❌ Never accept tokens as tool arguments — that's a leak waiting to happen.

(See §16 for the full credential rules.)

### 11.6 Progress streaming from MCP servers

Tools emit progress via MCP **notifications** (`notifications/progress` or `notifications/message`). The pool forwards every notification to the host's `on_progress(kind, message)` callback, which is already tagged to the active thread via `current_thread_id`. Tools must **not** stream progress through stdout/stderr or stuff it into return values.

(See §17 for the full progress contract.)

### 11.7 Future: stdio → HTTP/SSE for ACA hosting

When a server is moved to Azure Container Apps, only its connection config in `skill.yaml` changes:

```yaml
mcp_servers:
  - name: workiq
    transport: streamable_http
    url: https://workiq-mcp.<aca-env>.azurecontainerapps.io/mcp
```

The server module itself is unchanged — FastMCP supports both transports natively.

On the host side, we have a per-server choice (see §11.4a for the full discussion):

- **Default — client-dispatched HTTP.** The `MCPClientPool` grows an HTTP branch alongside stdio. Tools are still registered with the Responses API as function tools; the pool dispatches `requires_action` calls over HTTP instead of over stdio. Auth, progress streaming, and the agent loop are all unchanged.
- **Opt-in — native Responses-API MCP tool.** If a server is fully cloud-resident with auth that fits Responses' MCP auth model and no need for `on_progress` streaming, declare it as `transport: native_responses_mcp` and the agent loop registers it as `{type: "mcp", server_url: ...}` instead of expanding it into function tools. The model server handles the call cloud-to-cloud.

Either way, **skill code, tool code, and the canonical agent loop in §14.1 are unchanged** — the change is one line in `skill.yaml` plus a branch in the pool / tool-registration step.

### 11.8 Authoring checklist for an MCP-packaged tool

- [ ] Tool lives in the right server: per-skill server if used by one skill, shared server if used by ≥2.
- [ ] One job per tool function. Verb-first name.
- [ ] `@mcp.tool(description=...)` reads like an MCP tool description — *when* to call it, not *how*.
- [ ] All inputs are explicit function parameters with type hints. Tool never reads thread state or undeclared files.
- [ ] Return value is JSON-serializable structured facts with a `status`/`found` discriminator for failure modes.
- [ ] No user-facing sentences in any return path.
- [ ] No nested tool calls. No marker strings.
- [ ] Credentials via `get_credential()`. Never re-instantiated. Never accepted as input.
- [ ] Progress via MCP notifications (which the pool forwards to `on_progress`). Never via prints or return prose.
- [ ] No top-level side effects in the module.

---

## 11A. Shared MCP servers — when and how

A tool belongs in a **shared** MCP server (under `src/hub_cowork/mcp_servers/`) when:

- It is called by two or more skills today, **or**
- It wraps a Microsoft service the whole product depends on (WorkIQ, Graph/M365, ACS), **or**
- It is a runtime utility every skill might want (`log_progress`, `get_hub_config`, `get_task_status`).

A tool belongs in a **per-skill** MCP server (under `src/hub_cowork/skills/<skill>/mcp_server/`) when:

- It encodes domain logic specific to that workflow (`build_agenda`, `score_rfp_response`, `compare_shelf_inventory`).
- Its inputs/outputs are shaped by one skill's data model.

**Don't preemptively share.** If a tool starts as skill-local and later gets adopted by another skill, *then* promote it to the shared `mcp_servers/` tree. Premature sharing creates coupling that's painful to reverse.

**Naming:** shared servers get short, domain-named directories (`workiq`, `m365`, `progress`, `fabric`). Per-skill servers are always under their skill folder and never need a name beyond `mcp_server`.

**Versioning:** for now, every shared server lives at HEAD — no version pinning, no compatibility shims. When ACA migration begins (§11.7), shared servers gain version tags and the host pool starts pinning a version per skill.

---

## 12. Skill loading and discovery

Skills are **folders, not files.** Discovery is recursive and config-free.

### 12.1 Folder-based, recursive discovery

```
src/hub_cowork/skills/                      # discovered recursively
├── qa/
│   ├── skill.yaml
│   └── SKILL.md
├── engagement_agenda/
│   ├── skill.yaml
│   ├── SKILL.md
│   └── tools/
│       ├── find_briefing_calls.py
│       ├── retrieve_briefing_notes.py
│       ├── build_agenda.py
│       └── publish_agenda_doc.py
└── shelf_watch/
    ├── skill.yaml
    ├── SKILL.md
    └── tools/
        ├── shelf_watch_run.py
        ├── _compare.py
        └── _memory.py
```

The skill loader walks every subfolder under `skills/`. A folder containing both `skill.yaml` and `SKILL.md` is a skill. A folder missing either is **a hard error** — the loader refuses to start.

### 12.2 Skills are folders, not flat YAMLs

Every skill is its own folder. This is non-negotiable because:

- Skill-local tools live in a sibling `tools/` subfolder, scoped naturally.
- The loader can detect a skill purely by looking at folder shape — no central manifest.
- `SKILL.md` and `skill.yaml` always sit next to the tools they describe — easy navigation, easy review.

### 12.3 Two-tier loading (the Cowork progressive-disclosure analog)

Cowork keeps every installed skill's *name + description* in the model's context at session start (~100 tokens per skill), and lazily loads the full `SKILL.md` only when that skill is selected.

We approximate the same behavior with a **two-call pattern**:

1. **Router call (cheap, fast model).** The router prompt embeds *only* `name` + `description` from each `skill.yaml`. The router classifies the user's message and emits a skill name (or `"none"`).
2. **Execution call (full model + full SKILL.md).** Only the selected skill's full `SKILL.md` is loaded into the Responses API `instructions` parameter, and only that skill's tool subset is registered.

The net result: full skill bodies never enter context unless they're actually used. Adding a new skill costs the router prompt ~100 tokens; the body is free until invoked. (See §13 for the routing contract; see §18 for why this differs from Cowork's single-model pattern.)

### 12.4 Skills are auto-discovered — no registry

There is no `skills/__init__.py` listing all skills, no central enum, no decorator. Drop in a folder with `skill.yaml` + `SKILL.md`, restart, and the skill is live: indexed by the loader, included in the router prompt, and invokable.

### 12.5 Skill description is the router contract

The `description` field in `skill.yaml` is **the only thing the router sees** for that skill. Treat it like the description of an MCP tool — pack it with intent verbs, customer phrases, and trigger keywords:

```yaml
description: >
  Create an Innovation Hub engagement agenda end-to-end — from briefing call
  discovery through Word document publication. Trigger phrases: "create agenda",
  "engagement agenda", "innovation hub agenda", "build agenda for <customer>",
  "agenda document".
```

Greetings, off-topic chatter, and small talk should be routed to `"none"` — handled directly by the router as a conversation reply, no skill invoked. (Per Cowork: "the router itself can be conversational.")

### 12.6 Skill-local vs shared tools

A tool's location determines its visibility:

- `src/hub_cowork/tools/<tool>.py` — **shared.** Any skill can list it in its `tools:` array.
- `src/hub_cowork/skills/<skill>/tools/<tool>.py` — **skill-local.** Only that skill can use it.

Use skill-local for tools that encode workflow-specific logic (e.g., the `engagement_agenda`-specific `build_agenda`); use shared for cross-cutting utilities (`log_progress`, `query_workiq`, `send_email`, `create_word_doc`).

### 12.7 No registration, no manifest, no decorators

If you find yourself writing a `register_skill()` call, a manifest file, or a decorator-based discovery system — **stop.** Folder shape is the discovery mechanism. The simplicity is the design.

---

## 13. Routing — the classifier call

The router is a **single API call** to the fast model. Its only job: pick a skill name (or `"none"`).

### 13.1 The router prompt

Built dynamically from the `description` field of every loaded skill:

```text
Classify the user's request into ONE of the skills below, or "none" if it's
a greeting, small talk, or off-topic.

Skills:
- engagement_agenda: <description from engagement_agenda/skill.yaml>
- qa: <description from qa/skill.yaml>
- task_status: <description from task_status/skill.yaml>
- ...

Reply with ONLY the skill name, or "none".
```

### 13.2 Router responsibilities

The router:

- ✅ Picks **one** skill or `"none"`.
- ✅ Replies directly to the user when the choice is `"none"` (greetings, "thanks", small talk).
- ✅ Uses the fast tier (`AZURE_OPENAI_CHAT_MODEL_SMALL`).
- ❌ Does **not** decide tool calls. It does not even see the tool list.
- ❌ Does **not** call tools.
- ❌ Does **not** maintain its own conversation history. The router is stateless per turn.

### 13.3 Why the router is a separate call (not one mega-prompt)

Cowork uses one model call where the model both routes and executes (because it always uses the same model). We deliberately split it because:

- **Per-skill model-tier routing.** A Q&A skill runs on the fast model; an agenda workflow runs on the reasoning model. We can't pick the right tier until we know the skill.
- **Per-skill tool scoping.** Sending only the relevant tool subset (typically 3–6 tools) keeps API payloads small and prevents the model from accidentally calling `send_email` from a Q&A turn.
- **Cost.** A fast-model classification call is ~1¢ vs a reasoning-model call with full tool catalog at ~10–30¢.

This is documented as a deliberate divergence in §18.1.

---

## 14. The agent loop — anatomy

`agent_core` is **wiring, not intelligence.** Its loop is the canonical Responses-API tool-execution bridge — nothing more.

### 14.1 The canonical shape

```python
def run_agent_on_thread(skill: Skill, user_input: str, thread: ConversationThread,
                        on_progress) -> str:
    client = _get_responses_client()
    # skill.tool_schemas is the union of every MCP server's tools/list result for
    # this skill, each registered with the Responses API as an ordinary function
    # tool (type="function"). Stdio MCP servers cannot be passed to the API as
    # native mcp tools — only remote HTTP servers can. See §11.4a.
    response = client.responses.create(
        model=_model_for(skill.model_tier),
        instructions=skill.system_prompt,         # the SKILL.md body
        tools=skill.tool_schemas,                 # function-tool schemas built from MCP tools/list
        input=user_input,
        previous_response_id=thread.previous_response_id,
        reasoning={"effort": skill.reasoning_effort} if skill.model_tier == "reasoning" else None,
    )

    while response.status == "requires_action":
        outputs = []
        for tc in response.required_action.submit_tool_outputs.tool_calls:
            server, tool_name = skill.resolve(tc.function.name)   # which MCP server owns this tool
            args = json.loads(tc.function.arguments)
            result = mcp_pool.call(server, tool_name, args, on_progress=on_progress)
            outputs.append({"tool_call_id": tc.id, "output": result})

        response = client.responses.submit_tool_outputs(
            response_id=response.id,
            tool_outputs=outputs,
        )

    thread.previous_response_id = response.id
    return response.output_text
```

That's the whole loop. ~30 lines. **No decisions. No marker parsing. No skill chaining. No state machine.**

### 14.2 What is allowed inside the loop

- Look up the tool by name from this skill's tool registry.
- Parse `tc.function.arguments` (already a JSON string from the API).
- Pass `on_progress` and runtime helpers (credential, workiq CLI path) into `handle()`.
- Forward the tool's return string back as `output`.
- Update `thread.previous_response_id` when the loop ends.

### 14.3 What is forbidden inside the loop

- ❌ Inspecting `result` text for keywords or markers.
- ❌ Mutating, summarizing, truncating, or "cleaning" the tool's result before returning it to the API.
- ❌ Deciding to skip a tool call, retry it, or replace it with a different one.
- ❌ Emitting a final response on the model's behalf.
- ❌ Reading `response.output_text` for `[STOP_CHAIN]` / `[AWAITING_CONFIRMATION]` / any control marker.
- ❌ Looking at the skill's YAML for a `next_skill` and chaining.
- ❌ Catching tool exceptions and converting them to user-facing prose. Re-raise (so the runtime logs it) or return a structured `{"status": "error", "reason": "..."}` and let the model decide.

### 14.4 HITL is the natural exit of the loop

When the model returns a final text response with **no pending tool calls**, the loop exits. The runtime then makes a binary decision based on **thread status**, not text content:

- If the thread was running a workflow and the response ends with what looks like a question → set `thread.status = "awaiting_user"`. Surface text. Wait.
- If the workflow appears complete → set `thread.status = "complete"`.

The "looks like a question" test is intentionally simple (e.g., "ends with `?`" or "explicitly says `please confirm`/`which one`") and lives in a small helper, not in the loop. It's a heuristic to decide UI presentation — **not** a control-flow decision (the loop has already exited either way).

When the user's next message arrives on the same thread, we call the API again with `previous_response_id` pointing at the last response. The API resumes the workflow naturally.

---

## 15. Conversation state — `previous_response_id` is the only state

The Responses API tracks the full conversation server-side, keyed by `previous_response_id`. **That is our only conversation context.**

### 15.1 What the runtime stores per thread

Just enough to resume:

- `previous_response_id: str | None` — the API's pointer to where this thread left off.
- `status: "running" | "awaiting_user" | "complete" | "failed"` — UI state, also used to decide HITL vs completion.
- Lightweight UI metadata (`progress_log`, `code_log`) for the chat panel — write-only side channels, never fed back into the LLM context.

### 15.2 What the runtime does NOT store

- ❌ A `messages` list mirroring the API's history.
- ❌ Per-skill scratchpads.
- ❌ Inter-phase JSON context files on disk (e.g., a `<workflow>/<customer>.json` handoff store).
- ❌ Tool result caches keyed off the conversation.

If the model needs to remember something from earlier in the conversation, it remembers because `previous_response_id` carries the full history server-side. We don't manage it.

### 15.3 Persistence

The thread store (`LocalJsonThreadStore`) persists the ConversationThread — including `previous_response_id` — to `~/.hub-cowork/threads/active/<thread-id>.json`. On restart, the thread resumes exactly where it left off because the API still knows the history keyed off that ID.

### 15.4 The SYSTEM thread

A special pseudo-thread (`thread_id == "system"`) handles cross-task questions ("what threads do I have running?"). It is never persisted and never accumulates `previous_response_id` — every system query is one-shot.

---

## 16. Auth and credential sharing

**Do not touch this subsystem unless explicitly asked.** It is preserved verbatim (§9b).

### 16.1 The model

Hub Cowork uses **one shared Entra credential** for everything: Azure OpenAI, Microsoft Graph, FoundryIQ, Fabric, ACS. The user signs in once via the Settings UI; MSAL silently mints scope-specific access tokens on demand from the same refresh token.

### 16.2 The mechanism

- Built by `core/auth_credential.py::make_credential()`.
- Prefers WAM broker (`InteractiveBrowserBrokerCredential` from `azure-identity-broker`), parented to the pywebview HWND via `set_parent_window_handle()` so the account picker is modal to our UI.
- Falls back to classic `InteractiveBrowserCredential` when the broker / `pymsalruntime` is unavailable.
- `AuthenticationRecord` persisted to disk for silent token refresh on restart.
- MSAL token cache name is `hub_cowork` — must not collide with sibling forks.

### 16.3 The sharing pattern

```python
# In agent_core (once, at startup):
from hub_cowork.core.auth_credential import make_credential
from hub_cowork.core.outlook_helper import set_credential

credential = make_credential(tenant_id=..., cache_name="hub_cowork", ...)
set_credential(credential)

# In any tool that needs auth:
from hub_cowork.core.outlook_helper import get_credential
credential = get_credential()
token = credential.get_token("https://graph.microsoft.com/.default").token
```

Tools never construct credentials. They never call `DefaultAzureCredential`. They never shell out to `az login`. They call `get_credential()` and trust it.

### 16.4 Why this is a strength over Cowork's per-service OAuth

Cowork must do a separate OAuth handshake per MCP server (HubSpot, Slack, Notion …) and run a token-routing gateway. We don't need any of that — one Entra identity grants scoped tokens for every Microsoft service we touch. (See §18.4.)

### 16.5 What is forbidden

- ❌ Per-tool credentials, per-service OAuth dances, `DefaultAzureCredential` chains.
- ❌ Calling `az` CLI subprocesses (the app runs under `pythonw.exe` — no console).
- ❌ Re-instantiating the credential anywhere except `agent_core` startup.
- ❌ Refactoring the WAM broker fallback chain.

---

## 17. Progress streaming and the UI bridge

The chat UI shows real-time progress as the model executes. The contract is small and strict.

### 17.1 The `on_progress` callback

```python
def on_progress(kind: str, message: str, *, milestone: bool = False, **extra) -> None:
    """Emit a progress event tagged to the current thread."""
```

Threaded by `current_thread_id` (a `ContextVar` set by `_ThreadWorker`), so every event auto-routes to the correct chat panel.

`kind` values used today:

- `"agent"` — the active skill's display name.
- `"step"` — short user-visible status ("Searching WorkIQ…", "Building agenda table…").
- `"tool"` — a tool emitted a fact worth showing (typically used by `log_progress`).

### 17.2 Who emits what

- **Tools** call `on_progress(...)` to surface meaningful work — never to narrate every internal line. Coarse-grained, not chatty.
- **`log_progress`** is a *tool* the model calls to push milestone messages into the UI. It's the model's microphone — the model decides what's worth telling the user.
- **`agent_core`** emits `("agent", skill.name)` once when a skill starts, and forwards everything else from tools.

### 17.3 What the UI receives

Progress events flow through the WebSocket as `thread_progress` messages. The full message text is sent verbatim — display-side truncation (and tooltips for full text) is the UI's job. Tools must not truncate.

### 17.4 What is NOT progress streaming

- ❌ Tool return values. Returns are JSON facts for the model, not for the UI.
- ❌ The model's final reply. That comes through as `thread_completed` separately.
- ❌ Print statements. The runtime captures stdout into the code log, but progress is `on_progress` only.

---

# Part III — Deliberate divergences from Claude Cowork

> Cowork's design is the inspiration, not the gospel. Where Hub Cowork's deployment model differs (local-first, single-user, Microsoft-stack-only), we make different choices. Each is documented here so future contributors understand they are intentional, not oversights.

## 18. The divergences

### 18.1 Two-call routing instead of one

**Cowork:** one model call, model both routes and executes.
**Us:** cheap fast-model router call → reasoning-model execution call.
**Why:** per-skill model-tier routing (Q&A on fast, agenda on reasoning) and per-skill tool scoping (smaller payloads, accidental-call prevention) require knowing the skill before we configure the execution call.

### 18.2 Per-skill tool scoping

**Cowork:** every connected MCP tool is available in every skill; the model picks from the full set.
**Us:** `skill.yaml` lists exactly the tools that skill is allowed to call.
**Why:** local tools have real side effects (sending email, writing files, hitting the user's CRM via WorkIQ). Scoping prevents a Q&A turn from accidentally invoking `send_email` and keeps the per-call tool catalog small. The trade-off (less flexible "the model figures out an unexpected path") is acceptable for a single-tenant productivity agent.

### 18.3 Per-skill model-tier routing

**Cowork:** one model handles everything; subscription tier picks Sonnet vs Opus.
**Us:** `model_tier: reasoning | fast` per skill, with `reasoning_effort` for fine control.
**Why:** we pay per-token through Azure OpenAI. Routing simple skills to the fast tier and reserving the reasoning tier for complex multi-step workflows is significant cost optimization that Cowork doesn't expose.

### 18.4 Single Entra credential vs per-service OAuth

**Cowork:** separate OAuth handshake and stored token per MCP server (HubSpot, Slack, Notion, etc.); platform-side token-routing gateway.
**Us:** one shared Entra credential; MSAL mints scope-specific tokens on demand.
**Why:** every external service we hit (Graph, Azure OpenAI, FoundryIQ, Fabric, ACS) accepts Entra tokens. One sign-in covers everything. We don't need a token gateway. (See §16.)

### 18.5 Local stdio MCP now, HTTP MCP later — same protocol either way

**Cowork:** tools are MCP servers; remote ones run cloud-side, local ones run as stdio subprocesses spawned by the host.
**Us:** same — tools are MCP servers, packaged per-skill and as shared cross-cutting servers (§11, §11A). Today they all run as stdio subprocesses spawned by the host's `MCPClientPool`. The user still launches one process (`python -m hub_cowork`); the pool lifts each server lazily on first use and keeps it warm.
**Why:** matches the Cowork pattern, gives us the right boundaries from day one, and makes the future move to ACA-hosted servers a transport swap (stdio → streamable HTTP) instead of a refactor (§11.7).
**What's still different from Cowork:** auth (§18.4 — single Entra credential read from the shared MSAL cache by every server, no per-server OAuth) and execution location (§18.6 — every server runs on the user's machine today; ACA migration moves them to Azure, never to a third-party cloud).

### 18.6 Local-first execution

**Cowork:** model runs in Anthropic cloud; remote tool calls happen cloud-to-cloud; only local MCP stdio tools run on the user's machine.
**Us:** the model runs in Azure OpenAI cloud, but **tool execution is always local** — including for tools that hit cloud services (WorkIQ CLI, Graph, FoundryIQ). Tools call out from the user's machine with the user's credential.
**Why:** keeps user data and credentials on the user's machine. No token gateway. No "is this MCP endpoint reachable from Azure's IPs" question. The user owns the network path.

### 18.7 What we explicitly copy from Cowork (no divergence)

- **Three-layer split** (model = orchestrator, skill = expertise, tool = mechanical) — §1.
- **`SKILL.md` for expertise, `skill.yaml` for config** — §6.
- **Tools return facts, model writes prose** — §3.
- **HITL as natural conversation, not state machine** — §4.
- **Folder-based discovery, no manifest** — §11.2, §12.4.
- **Tool composition is the model's job, not a tool's** — §11.4.
- **Description-driven router** (analog of Cowork's progressive disclosure) — §12.3, §13.

---

## 10. Reference

- [Anthropic knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins) — original SKILL.md pattern.
- [Anthropic skills repo](https://github.com/anthropics/skills) — general-purpose skills.
- [Model Context Protocol spec](https://modelcontextprotocol.io/) and the [Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp` package, FastMCP).
