# Authoring a Hub Cowork skill (and its tools)

The authoritative spec is [`SKILLS_DESIGN_PRINCIPLES.md`](SKILLS_DESIGN_PRINCIPLES.md). This is the practical recipe — the file shapes, the conventions, the lint script.

A skill is a folder under `src/hub_cowork/skills/`:

```
src/hub_cowork/skills/<skill_name>/
├── skill.yaml           # runtime config only
├── SKILL.md             # the system prompt (Markdown)
└── mcp_server/          # OPTIONAL — only if this skill needs new, skill-private tools
    ├── __init__.py
    ├── __main__.py      # ~10 lines — calls serve("<skill>", [<tool module paths>])
    └── tools/
        ├── __init__.py
        └── <tool>.py    # exports SCHEMA: dict and handle(arguments, *, on_progress=None) -> str
```

The runtime auto-discovers skill folders. There is no registry, no manifest, no central enum, no decorator.

---

## `skill.yaml` — config only, no prose

```yaml
name: my_skill
description: |
  One short paragraph describing WHEN to invoke this skill.
  This is the ONLY thing the router sees about your skill.
  Pack it with intent verbs and trigger phrases.
  Keywords: keyword1, keyword2, ...

model: full              # "full" (reasoning model) or "mini" (fast model)
                         # design-doc target name is `model_tier: reasoning|fast` —
                         # both keys read the same env vars; rename pending Phase 5 cleanup.
reasoning_effort: medium # only when model=full; one of low | medium | high
queued: true             # true = serialized on the conversation's worker thread (default)
                         # false = runs on the SYSTEM pseudo-thread immediately

mcp_servers:             # which MCP servers to wire up for this skill
  - .                    # "." means "this skill's own mcp_server/ folder"
  - workiq               # shared servers under src/hub_cowork/mcp_servers/<name>
  - m365
  - utility

tool_allowlist:          # OPTIONAL — subset filter. If omitted, every tool from
  - query_workiq         # every listed mcp_server is exposed to the model.
  - log_progress
  - send_email
```

**No other keys.** Specifically — no `instructions:`, no `next_skill:`, no `conversational:`, no `tools:` (loose list), no prose. The lint script enforces this.

---

## `SKILL.md` — domain expertise, not procedure

`SKILL.md` is the Markdown system prompt the runtime feeds to the model. Write it as **expertise a senior colleague would explain to a junior**, not a numbered runbook.

DO:
- Describe the problem space, the customer's situation, the choices the model needs to make.
- Explain the heuristics: "If the engagement type is ADS, prefer a 5-day arc starting on a Tuesday." "When the briefing call notes mention more than three personas, ask the user to confirm the priority order before drafting the agenda."
- Document the structured fields each tool returns and how to interpret them.
- Explain HITL turns as natural conversation: "Confirm with the user before publishing. End your turn with a question."

DON'T:
- Numbered step-by-step procedures (`STEP 1 — call query_workiq. STEP 2 — call log_progress.`).
- String-formatting recipes (`"Your final response MUST be exactly: '...'"`).
- Control-flow markers (`[AWAITING_CONFIRMATION]`, `[STOP_CHAIN]`). The lint script blocks these.
- Phase markers (`TURN 1`, `TURN 2+`). The model knows what turn it is on from the conversation.
- Skill chaining (`next_skill:`). One workflow = one skill.

If a step is mechanical (validate input, format JSON, build a filename, look up a config value), that step belongs in a **tool**, not in `SKILL.md`.

---

## Tools — packaged as MCP servers

Every tool in Hub Cowork is hosted by an MCP server subprocess. The MCP wire-protocol layer lives once in [`mcp_servers/_runtime.py`](../../src/hub_cowork/mcp_servers/_runtime.py); every server folder is a thin manifest that calls `serve(...)`.

### Picking the right server

| Used by | Goes in |
|---|---|
| Exactly one skill | `skills/<skill>/mcp_server/tools/<tool>.py` |
| Two or more skills, **or** wraps a foundational service (WorkIQ, Graph, ACS) | One of the shared servers under `mcp_servers/<server>/tools/<tool>.py` (or a new shared server folder) |

Don't preemptively share. Promote a tool to a shared server only when a second skill actually needs it.

### Tool module shape

A tool module is a plain Python module that exports two things:

```python
# src/hub_cowork/mcp_servers/<server>/tools/<tool>.py
"""Tool: my_tool — one-line summary."""

SCHEMA: dict = {
    "type": "function",
    "name": "my_tool",
    "description": (
        "What this tool does. Written for the MODEL to decide when to call it. "
        "Be specific about the trigger conditions and the shape of the result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "customer_name": {"type": "string", "description": "The customer."},
            "limit":         {"type": "integer", "description": "Optional cap.",
                              "default": 10},
        },
        "required": ["customer_name"],
    },
}


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    """Return JSON-serializable structured facts."""
    customer = arguments["customer_name"]

    if on_progress:
        on_progress("status", f"Looking up {customer}…")

    # ... do the work ...

    return json.dumps({
        "status": "ok",            # one of: ok | no_data | error
        "customer": customer,
        "records": [...],
    })
```

The shape is deliberately **not** decorator-based and **not** auto-derived from type hints. We use the lowlevel `mcp.server.lowlevel.Server` from the official `mcp` SDK precisely so the model sees the EXACT same `SCHEMA["parameters"]` dict you wrote — descriptions, enums, nested objects, defaults all round-trip bit-for-bit. FastMCP would flatten this through Pydantic; we don't want that.

### The server `__main__.py` — about 10 lines

```python
# src/hub_cowork/mcp_servers/<server>/__main__.py
"""MCP server: <server> — one-line purpose."""
from hub_cowork.mcp_servers._runtime import serve

if __name__ == "__main__":
    serve("<server>", [
        "hub_cowork.mcp_servers.<server>.tools.tool_a",
        "hub_cowork.mcp_servers.<server>.tools.tool_b",
    ])
```

The runtime imports each module path, reads its `SCHEMA` and `handle`, and advertises them via MCP `tools/list`. The `MCPClientPool` in the host launches this script with `python -m hub_cowork.mcp_servers.<server>` once on first use and keeps it warm for the host's lifetime.

For a per-skill server, swap the import and module paths to the skill's own folder:

```python
# src/hub_cowork/skills/<skill>/mcp_server/__main__.py
from hub_cowork.mcp_servers._runtime import serve

if __name__ == "__main__":
    serve("<skill>", [
        "hub_cowork.skills.<skill>.mcp_server.tools.your_tool",
    ])
```

### Tool contract — the rules

- **Return structured JSON** — `{"status": "ok", ...}` / `{"status": "no_data", ...}` / `{"status": "error", "kind": "config|transient|...", "message": "..."}`. **Never user-facing prose.** The model writes the prose.
- **Tools never call other tools.** Composition is the model's job. If your tool needs to invoke another, split it differently or let the model orchestrate.
- **Tools never construct credentials.** Call `get_credential()` from `hub_cowork.core.auth_credential` (or `hub_cowork.core.agent_core` re-export) — there is one shared credential and the on-disk MSAL cache lets server subprocesses silent-refresh from the same blob the host wrote at sign-in. Do not call `DefaultAzureCredential`. Do not shell out to `az`.
- **Tools never re-instantiate the OpenAI client.** Call `get_responses_client()` if you need it.
- **Progress flows through `on_progress(kind, message)`** — the pool forwards it as MCP `notifications/progress`, the host fans it out to the WebSocket.
- **No top-level side effects at import** — server subprocesses import every tool module on startup. Heavy initialization goes inside `handle(...)` or behind a lazy module-level cache.

### Tool naming conventions

- Public tool: `tool_name.py` — picked up by `serve(...)` if listed.
- Private helper: `_helper.py` (underscore prefix). The `serve(...)` loader and the legacy auto-discoverer skip these. Use them for internal modules a tool decomposes into.

---

## Routing

The router is a one-shot fast-model classifier. It sees only each skill's `description`. To make your skill discoverable, pack the description with intent verbs and trigger phrases (`Keywords: ...`). The router does not see your tools or your `SKILL.md`.

Greetings and small talk are classified as `"none"` and answered by the router directly — your skill is never invoked for them.

---

## HITL (human-in-the-loop)

End your turn with a question. The runtime detects a final text response with no pending tool calls and parks the thread in `awaiting_user`. The user's next message resumes the same Responses-API conversation via `previous_response_id` — the model already has full context.

**Do not encode HITL with markers** (`[AWAITING_CONFIRMATION]`, `[STOP_CHAIN]`, …). **Do not encode HITL with state machines** in `agent_core`. The pattern is exactly:

> The model asks a question → the runtime parks → the next user message resumes.

That's the whole protocol.

---

## Multi-step workflows

One skill, one workflow. Phase context flows through the model's own conversation history (preserved across turns by `previous_response_id`). You do not need on-disk JSON handoffs between phases.

If you find yourself wanting to chain skills with `next_skill`, the right answer is one of:

- Split a tool further so the model has the right primitive to keep going in the same skill.
- Write richer guidance in `SKILL.md` so the model orchestrates the phases itself.

The `engagement_agenda` skill is the proof point — it used to be four chained skills with on-disk JSON handoff (`hub_agenda_creation/{briefing,goals,build,publish}.yaml` + `engagement_context.py`); it is now a single skill that the model orchestrates across turns. Total skill code shrunk; reliability and HITL behavior improved.

---

## Validating

```powershell
python scripts/lint_skills.py
```

Run after any skill edit. The lint blocks the banned legacy patterns and confirms each skill folder is well-formed:

- `instructions:` field in `skill.yaml` → blocked
- `next_skill:` → blocked
- `conversational:` → blocked
- `[STOP_CHAIN]` / `[AWAITING_CONFIRMATION]` markers in `SKILL.md` → blocked
- Missing `SKILL.md` next to `skill.yaml` → blocked

Restart the agent (Settings → Restart, or `.\scripts\restart.ps1`) so the skill loader picks up the new folder. Skills are loaded once at module import — YAML/Markdown edits are NOT live-reloaded.

---

## Quick reference — the §9 PR checklist (from `SKILLS_DESIGN_PRINCIPLES.md`)

Before you mark a skill or tool change "done", confirm:

1. `skill.yaml` contains only config keys — no prose.
2. `SKILL.md` describes judgment, not procedure.
3. Every tool returns structured JSON; no user-facing sentences in tool returns.
4. No tool calls another tool.
5. No tool constructs a credential — `get_credential()` only.
6. No marker strings in `SKILL.md`; no marker parsing in `agent_core`.
7. `previous_response_id` is the only conversation state — no parallel `messages` list, no per-skill scratchpad.
8. `agent_core` was not modified — if you needed to, you are probably violating the layering.
9. Any new shared tool was promoted because a SECOND skill actually needs it (not "might one day").
10. `python scripts/lint_skills.py` passes.
