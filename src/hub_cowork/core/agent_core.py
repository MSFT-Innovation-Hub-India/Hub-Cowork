"""
Core agent logic — Router + skill-based sub-agents, tool execution, auth helpers.

Architecture:
  Router (master agent) → classifies user intent → hands off to the matching skill.
  Skills are loaded dynamically from YAML files in the skills/ folder.
  Adding a new skill requires only a new YAML file — no Python code changes.
"""

import json
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import yaml

from azure.identity import (
    AuthenticationRecord,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)
from dotenv import load_dotenv
from openai import OpenAI

from hub_cowork.core.outlook_helper import set_credential

# override=True makes values from .env win over any placeholder values that
# may be lingering in the parent shell (e.g. from an earlier smoke-test
# session). .env is intended to be the single source of truth for this app.
load_dotenv(override=False)

logger = logging.getLogger("hub_se_agent")

ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
CHAT_MODEL = os.environ["AZURE_OPENAI_CHAT_MODEL"]
CHAT_MODEL_SMALL = os.environ.get("AZURE_OPENAI_CHAT_MODEL_SMALL", CHAT_MODEL)
API_VERSION = os.environ["AZURE_OPENAI_API_VERSION"]

# Persistent token cache + authentication record
# The token cache `name` doubles as a Windows Credential Manager target
# prefix, so it must differ between the forks to avoid them fighting over
# the same cached token blob.
_cache_options = TokenCachePersistenceOptions(name="hub_cowork")
_tenant_id = os.environ.get("AZURE_TENANT_ID")
from hub_cowork.core.app_paths import AUTH_RECORD_PATH as _AUTH_RECORD_PATH  # noqa: E402
_AUTH_RECORD_PATH.parent.mkdir(exist_ok=True)


def _create_credential(record=None):
    """Create credential, optionally with a saved AuthenticationRecord for silent refresh."""
    from hub_cowork.core.auth_credential import make_credential
    return make_credential(
        tenant_id=_tenant_id,
        cache_name="hub_cowork",
        authentication_record=record,
    )


# Load saved authentication record if it exists (enables silent token refresh)
_auth_record = None
if _AUTH_RECORD_PATH.exists():
    try:
        _auth_record = AuthenticationRecord.deserialize(_AUTH_RECORD_PATH.read_text())
        logger.info("Loaded saved authentication record")
    except Exception:
        logger.warning("Failed to load auth record — will require sign-in")

_credential = _create_credential(_auth_record)
set_credential(_credential)

_responses_client: OpenAI | None = None
_responses_client_token_expires: float = 0
_responses_client_lock = threading.Lock()  # guards concurrent token refresh


# ---------------------------------------------------------------------------
# WorkIQ CLI resolution
# ---------------------------------------------------------------------------

def _find_workiq() -> str | None:
    """Resolve the full path to the workiq CLI."""
    # 1. Same venv as the agent
    venv_dir = Path(sys.executable).parent
    for name in ("workiq", "workiq.exe"):
        candidate = venv_dir / name
        if candidate.exists():
            return str(candidate)
    # 2. Explicit env var
    env_path = os.environ.get("WORKIQ_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    # 3. System PATH
    found = shutil.which("workiq")
    if found:
        return found
    return None


WORKIQ_CLI = _find_workiq()
if WORKIQ_CLI:
    logger.info("workiq CLI found: %s", WORKIQ_CLI)
else:
    logger.warning("workiq CLI not found. Install it or set WORKIQ_PATH in .env")


# ---------------------------------------------------------------------------
# Azure auth helpers
# ---------------------------------------------------------------------------

def get_credential():
    """Return the shared InteractiveBrowserCredential (for use by Redis bridge etc)."""
    return _credential


def check_azure_auth() -> tuple[bool, str]:
    """Check if Azure credentials are cached (non-interactive — never opens browser)."""
    if _auth_record is None:
        return False, "Not signed in"
    try:
        _credential.get_token("https://cognitiveservices.azure.com/.default")
        return True, "Authenticated"
    except Exception as e:
        return False, str(e)


def run_az_login(tenant_id: str | None = None,
                 subscription_id: str | None = None) -> tuple[bool, str]:
    """Trigger interactive browser login, save record for future silent refresh."""
    global _auth_record, _credential
    try:
        record = _credential.authenticate(
            scopes=["https://cognitiveservices.azure.com/.default"]
        )
        # Save the authentication record so future launches can silently refresh
        _AUTH_RECORD_PATH.write_text(record.serialize())
        _auth_record = record
        # Recreate credential with the record for silent refresh
        _credential = _create_credential(_auth_record)
        set_credential(_credential)
        logger.info("Auth record saved to %s", _AUTH_RECORD_PATH)
        return True, "Signed in successfully"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# OpenAI client (token-refreshing)
# ---------------------------------------------------------------------------

def get_responses_client() -> OpenAI:
    """Return a cached OpenAI client for Azure OpenAI Responses API.

    Silently refreshes tokens via cached refresh token.
    Falls back to interactive browser login if refresh fails.

    Thread-safe: refresh happens under `_responses_client_lock` so that
    multiple ThreadExecutors starting up near token expiry don't race.
    """
    global _responses_client, _responses_client_token_expires
    now = time.time()
    if _responses_client is not None and now < _responses_client_token_expires - 300:
        return _responses_client
    with _responses_client_lock:
        # Re-check inside the lock — another thread may have refreshed already.
        now = time.time()
        if _responses_client is not None and now < _responses_client_token_expires - 300:
            return _responses_client
        base_url = ENDPOINT.rstrip("/")
        if not base_url.endswith("/openai/v1"):
            base_url = f"{base_url}/openai/v1"
        try:
            token_obj = _credential.get_token(
                "https://cognitiveservices.azure.com/.default"
            )
        except Exception:
            logger.warning("Token refresh failed — attempting interactive login...")
            ok, msg = run_az_login()
            if not ok:
                raise RuntimeError(
                    f"Azure authentication expired. Please sign in again. ({msg})"
                )
            logger.info("Interactive login succeeded: %s", msg)
            token_obj = _credential.get_token(
                "https://cognitiveservices.azure.com/.default"
            )
        _responses_client_token_expires = token_obj.expires_on
        _responses_client = OpenAI(
            base_url=base_url,
            api_key=token_obj.token,
        )
    return _responses_client


# ---------------------------------------------------------------------------
# Skill loader — reads YAML files from skills/ folder
# ---------------------------------------------------------------------------

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # src/hub_cowork
_SKILLS_DIR = _PACKAGE_ROOT / "skills"


# ---------------------------------------------------------------------------
# MCP-based tool layer (per SKILLS_DESIGN_PRINCIPLES.md §11 / §14.1)
#
# Tools are MCP servers, spawned as stdio subprocesses by `MCPClientPool`.
# Each skill declares the servers it depends on in its `skill.yaml`:
#
#     mcp_servers:
#       - .                  # the skill's own per-skill server (skills/<name>/mcp_server)
#       - workiq             # shared servers under hub_cowork.mcp_servers.<name>
#       - utility
#       - m365
#     tool_allowlist:        # optional — restrict to a subset of advertised tools
#       - log_progress
#       - query_workiq
#
# At skill-load time we ask the pool for each declared server's `tools/list`,
# build Responses-API `type="function"` schemas, and remember which server
# owns each tool name so `handle_tool_call` can dispatch correctly.
#
# Per non-negotiable #14: stdio MCP servers cannot be passed to the Responses
# API as native `mcp` tools (that mode is HTTPS-only). They are exposed as
# ordinary function tools and dispatched client-side via the pool.
# ---------------------------------------------------------------------------

from hub_cowork.core.mcp_client_pool import MCPClientPool, python_module_server  # noqa: E402
import atexit  # noqa: E402

# Module-singleton pool shared by every conversation thread.
mcp_pool = MCPClientPool()
atexit.register(mcp_pool.shutdown)

# Built-in shared servers (under hub_cowork.mcp_servers.*). Per-skill servers
# are registered on demand by `_register_skill_servers` when a skill declares
# `.` in its mcp_servers list.
_SHARED_SERVERS = {
    "workiq":  "hub_cowork.mcp_servers.workiq",
    "m365":    "hub_cowork.mcp_servers.m365",
    "utility": "hub_cowork.mcp_servers.utility",
}
for _name, _module in _SHARED_SERVERS.items():
    mcp_pool.register(_name, python_module_server(_module))

# Per-skill servers are registered as their owning skill loads.
# Map: tool_name -> server_name. Built incrementally as skills are loaded
# so `handle_tool_call` always knows where to dispatch.
_TOOL_TO_SERVER: dict[str, str] = {}

# Cache of every tool schema we've seen, keyed by name. Used by Skill.tools
# to build the Responses-API tool list without re-listing each turn.
TOOL_SCHEMAS: dict[str, dict] = {}


def _register_skill_servers(skill_folder_name: str) -> str | None:
    """Register a skill's per-skill MCP server (if it ships one) and return
    the server name. The convention is:
        skills/<skill>/mcp_server/__main__.py
    is auto-registered as server name `<skill>`.
    """
    server_dir = _SKILLS_DIR / skill_folder_name / "mcp_server"
    if not (server_dir / "__main__.py").is_file():
        return None
    server_name = skill_folder_name
    module_path = f"hub_cowork.skills.{skill_folder_name}.mcp_server"
    mcp_pool.register(server_name, python_module_server(module_path))
    return server_name


def _resolve_skill_tools(skill_folder_name: str,
                         declared_servers: list[str],
                         allowlist: list[str] | None
                         ) -> tuple[list[dict], dict[str, str]]:
    """For one skill, ask each declared MCP server for its tools, apply the
    optional allowlist, and return:
      * the list of Responses-API function-tool schemas to advertise
      * a name -> server map so the dispatcher can route calls

    `declared_servers` may contain `"."` to mean the skill's own per-skill
    server (auto-named after the skill folder).
    """
    schemas: list[dict] = []
    name_to_server: dict[str, str] = {}
    seen: set[str] = set()
    for entry in declared_servers:
        if entry == ".":
            server = _register_skill_servers(skill_folder_name)
            if server is None:
                logger.warning(
                    "Skill '%s' declared mcp_server '.' but no mcp_server "
                    "folder found under skills/%s/", skill_folder_name,
                    skill_folder_name,
                )
                continue
        else:
            server = entry
            if not mcp_pool.is_registered(server):
                logger.error(
                    "Skill '%s' references unknown MCP server '%s'. "
                    "Known: %s", skill_folder_name, server,
                    sorted(_SHARED_SERVERS.keys()),
                )
                continue
        try:
            advertised = mcp_pool.list_tools(server)
        except Exception as e:
            logger.error("Failed to list tools on server '%s': %s", server, e)
            continue
        for schema in advertised:
            tool_name = schema["name"]
            if allowlist is not None and tool_name not in allowlist:
                continue
            if tool_name in seen:
                logger.warning(
                    "Skill '%s': tool name '%s' advertised by multiple "
                    "servers; first one wins.", skill_folder_name, tool_name,
                )
                continue
            seen.add(tool_name)
            schemas.append(schema)
            name_to_server[tool_name] = server
            # Cache globally too so Skill.tools and handle_tool_call work
            # uniformly across skills.
            TOOL_SCHEMAS[tool_name] = schema
            _TOOL_TO_SERVER[tool_name] = server
    return schemas, name_to_server


class Skill:
    """A loaded skill definition.

    Per SKILLS_DESIGN_PRINCIPLES.md: a skill is a folder with `skill.yaml`
    (runtime config) and `SKILL.md` (the system prompt as Markdown). Legacy
    YAML keys `conversational` and `next_skill` are accepted but ignored —
    the runtime no longer branches on conversation mode and does not chain
    skills (state flows via `previous_response_id`).
    """

    def __init__(self, data: dict, source_file: str, instructions: str | None = None,
                 *, skill_folder: str | None = None):
        self.name: str = data["name"]
        self.description: str = data["description"].strip()
        self.model_tier: str = data.get("model", "mini")  # "full" or "mini"
        self.queued: bool = data.get("queued", True)  # queue business tasks by default

        # Tool wiring — MCP-based per SKILLS_DESIGN_PRINCIPLES.md §11.
        # `mcp_servers` lists shared server names (workiq/m365/utility) plus
        # `.` for the skill's own per-skill server. `tool_allowlist` is an
        # optional subset filter applied across the union.
        # Legacy: a flat `tools:` list of tool names is still accepted and
        # treated as an allowlist resolved against ALL shared + own servers.
        mcp_servers = data.get("mcp_servers")
        tool_allowlist = data.get("tool_allowlist")
        legacy_tools = data.get("tools")
        folder = skill_folder or self.name
        if mcp_servers is None:
            # Back-compat: synthesize defaults — try the skill's own server
            # (only if a per-skill mcp_server/ folder actually exists) plus
            # the shared servers, and use the legacy `tools:` list as the
            # allowlist.
            mcp_servers = []
            if (_SKILLS_DIR / folder / "mcp_server" / "__main__.py").is_file():
                mcp_servers.append(".")
            mcp_servers.extend(["workiq", "m365", "utility"])
            if tool_allowlist is None and legacy_tools is not None:
                tool_allowlist = list(legacy_tools)

        self.mcp_servers: list[str] = list(mcp_servers)
        self.tool_allowlist: list[str] | None = (
            list(tool_allowlist) if tool_allowlist is not None else None
        )
        self._resolved_tools, self._tool_to_server = _resolve_skill_tools(
            folder, self.mcp_servers, self.tool_allowlist,
        )
        self.tool_names: list[str] = [t["name"] for t in self._resolved_tools]

        # Instructions resolution order:
        #   1. explicit `instructions` arg (loaded from SKILL.md)  -- preferred
        #   2. `instructions:` field in the YAML (legacy fallback)
        if instructions is not None:
            self.instructions: str = instructions.strip()
        elif "instructions" in data:
            self.instructions = data["instructions"].strip()
        else:
            raise ValueError(
                f"Skill '{self.name}' has no instructions: provide a SKILL.md "
                f"alongside its skill.yaml, or an `instructions:` field in the YAML."
            )
        self.reasoning_effort: str | None = data.get("reasoning_effort")  # "low", "medium", or "high"
        self.source_file: str = source_file

        # Warn if legacy keys are still present.
        for legacy in ("conversational", "next_skill"):
            if legacy in data:
                logger.warning(
                    "Skill '%s': YAML key '%s' is ignored by the runtime — remove it.",
                    self.name, legacy,
                )

    @property
    def model(self) -> str:
        return CHAT_MODEL if self.model_tier == "full" else CHAT_MODEL_SMALL

    @property
    def tools(self) -> list[dict]:
        return list(self._resolved_tools)

    def server_for_tool(self, name: str) -> str | None:
        return self._tool_to_server.get(name)


def _load_skills() -> dict[str, Skill]:
    """Load all skill folders from the skills/ tree.

    A skill folder contains:
      - skill.yaml  (runtime config: name, description, tools, model, ...)
      - SKILL.md    (the system prompt as Markdown)

    Legacy single-file YAMLs with an inline `instructions:` field are still
    accepted for backward compatibility, but new skills MUST follow the folder
    pattern. (See docs/architecture/SKILLS_DESIGN_PRINCIPLES.md §6, §12.)
    """
    skills: dict[str, Skill] = {}
    if not _SKILLS_DIR.is_dir():
        logger.warning("Skills directory not found: %s", _SKILLS_DIR)
        return skills

    # Discover every YAML under skills/ (any depth). For each YAML, look for a
    # sibling SKILL.md. Files under tools/ subfolders are ignored.
    for path in sorted(_SKILLS_DIR.glob("**/*.yaml")):
        if path.name.startswith("_"):
            continue
        # Skip anything inside a tools/ subfolder
        if "tools" in path.parts:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                logger.error("Skill YAML %s is not a mapping — skipping", path)
                continue

            # Look for a sibling SKILL.md (the new shape).
            md_path = path.parent / "SKILL.md"
            instructions: str | None = None
            if md_path.is_file():
                instructions = md_path.read_text(encoding="utf-8")
                if "instructions" in data:
                    logger.warning(
                        "Skill '%s' has BOTH SKILL.md and YAML `instructions:` — "
                        "SKILL.md wins. Remove the YAML field.",
                        data.get("name", path.name),
                    )

            skill = Skill(data, str(path), instructions=instructions,
                          skill_folder=path.parent.name)
            skills[skill.name] = skill
            logger.info(
                "Loaded skill: %s (%s model, %d tools, source=%s) from %s",
                skill.name, skill.model_tier, len(skill.tool_names),
                "SKILL.md" if instructions is not None else "yaml.instructions",
                path.relative_to(_SKILLS_DIR),
            )
        except Exception as e:
            logger.error("Failed to load skill from %s: %s", path, e)
    return skills


# Load skills at import time
_skills = _load_skills()
logger.info("Skills loaded: %s", list(_skills.keys()))


def get_loaded_skills() -> list[dict]:
    """Return a summary of all loaded skills for the UI.

    Includes internal/chained skills too — flagged so the UI can group or
    visually de-emphasize them. The `group` field is the immediate parent
    folder under skills/ (or None for top-level skills) so the UI can show
    chained skill families together.
    """
    out: list[dict] = []
    for s in _skills.values():
        try:
            rel = Path(s.source_file).resolve().relative_to(_SKILLS_DIR.resolve())
            parts = rel.parts
            group = parts[0] if len(parts) > 1 else None
        except Exception:
            group = None
        out.append({
            "name": s.name,
            "description": s.description,
            "model": s.model_tier,
            "tools": s.tool_names,
            "group": group,
            "internal": s.description.upper().startswith("[INTERNAL"),
        })
    return out


# ---------------------------------------------------------------------------
# Router (master agent) — classifies intent dynamically from loaded skills
# ---------------------------------------------------------------------------

def _build_router_prompt() -> str:
    """Build the router system prompt from all loaded skills."""
    lines = [
        "You are a routing agent. Your ONLY job is to classify the user's request "
        "and return a JSON object.",
        "",
        "Classify into one of these categories:",
        "",
    ]
    routable_skills = []
    for skill in _skills.values():
        # Skip internal chained skills — they are not user-facing entry points
        if "[INTERNAL" in skill.description.upper():
            continue
        routable_skills.append(skill)
        lines.append(f'{len(routable_skills)}. "{skill.name}" — {skill.description}')
        lines.append("")

    # Add a catch-all for greetings / small talk that need no skill
    lines.append(f'{len(routable_skills) + 1}. "none" — Greetings, small talk, thanks, goodbyes, '
                 f'or simple conversational messages that do NOT require any data lookup or action '
                 f'(e.g. "hi", "hello", "thanks", "how are you").')
    lines.append("")

    skill_names = [f'"{ s.name}"' for s in routable_skills]
    skill_names.append('"none"')
    examples = " or ".join(f'{{"skill": {n}}}' for n in skill_names)
    lines.append(f"Respond with ONLY a JSON object, no other text:")
    lines.append(examples)
    return "\n".join(lines)


ROUTER_PROMPT = _build_router_prompt()
logger.debug("Router prompt:\n%s", ROUTER_PROMPT)


def _route(user_input: str) -> str:
    """Classify user intent and return the skill name.

    Thread-scoped in the new model: called only on the *first* user message of
    a ConversationThread. Subsequent messages within a thread stay in that
    thread's skill, so the old `_active_session` router bias is no longer
    needed.
    """
    client = get_responses_client()
    response = client.responses.create(
        model=CHAT_MODEL,
        instructions=ROUTER_PROMPT,
        input=[{"role": "user", "content": user_input}],
        tools=[],
    )
    text = ""
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    text += part.text
    try:
        result = json.loads(text.strip())
        skill_name = result.get("skill") or result.get("agent", "qa")
        logger.info("[Router] Classified as: %s", skill_name)
        if skill_name == "none":
            return "none"
        if skill_name in _skills:
            return skill_name
        logger.warning("[Router] Unknown skill '%s' — defaulting to qa", skill_name)
        return "qa"
    except (json.JSONDecodeError, AttributeError):
        logger.warning("[Router] Could not parse response: %s — defaulting to qa", text)
        return "qa"


# ---------------------------------------------------------------------------
# Inbox classifier — used by the Redis bridge to decide whether a Teams
# message starts a new thread, continues an existing one, or is a system
# query across all threads.
# ---------------------------------------------------------------------------

_INBOX_CLASSIFIER_PROMPT = (
    "You are an inbox classifier for a multi-thread personal agent.\n"
    "Given the user's incoming message and a list of their currently active\n"
    "conversation threads, decide whether the message:\n"
    "  - starts a NEW thread, OR\n"
    "  - continues an EXISTING thread (reply, follow-up, confirmation), OR\n"
    "  - is a SYSTEM query about the agent's own task list / activity.\n\n"
    "STRICT definition of SYSTEM (use sparingly — only these patterns):\n"
    "  • \"what are you working on?\" / \"what's running?\" / \"what's in progress?\"\n"
    "  • \"show my tasks\" / \"list my tasks\" / \"task status\" / \"how many tasks?\"\n"
    "  • \"are you busy?\" / \"what's queued?\"\n"
    "  • Questions whose answer is a summary of the agent's OWN active threads.\n\n"
    "NOT system (these are NEW threads that need a real skill / tool):\n"
    "  • \"check my calendar\" / \"what's on my calendar today?\" / \"do I have meetings?\"\n"
    "  • \"check my emails\" / \"summarize the email from X\" / \"any RFPs in my inbox?\"\n"
    "  • \"find the document about Y\" / \"who is working on project Z?\"\n"
    "  • Any question that requires looking up the user's M365 data\n"
    "    (calendar, mail, files, contacts) — those go to a skill, not system.\n\n"
    "STRONG signals for EXISTING (continue, do NOT start a new thread):\n"
    "  • The message contains a tag like `#thread-ab12cd`.\n"
    "  • A thread is in status `awaiting_user` AND its `last_agent` excerpt\n"
    "    asked a question, requested information, or listed options. Almost\n"
    "    any reply from the same user \u2014 even one that just provides facts\n"
    "    with no pronouns \u2014 is the awaited reply. Examples:\n"
    "      - last_agent: \"Tell me the customer name, date, and venue.\"\n"
    "        message:    \"Customer is Acme, date 21 Apr 2026, venue MS Teams.\"\n"
    "        \u2192 EXISTING (the user is filling in the requested fields).\n"
    "      - last_agent: \"Which of these 3 calls should I use? 1) ... 2) ... 3) ...\"\n"
    "        message:    \"option 2\" / \"the second one\" / \"go with the Acme one\"\n"
    "        \u2192 EXISTING.\n"
    "      - last_agent: \"Shall I send the invite?\"\n"
    "        message:    \"yes\" / \"go ahead\" / \"hold off\" / \"not yet\"\n"
    "        \u2192 EXISTING.\n"
    "  • Anaphoric / referring expressions (\"this\", \"that\", \"it\", \"the one\",\n"
    "    \"above\", \"the previous\", \"more details\", \"what about\") almost\n"
    "    always continue the most recent thread whose last_agent excerpt\n"
    "    contains a relevant noun.\n\n"
    "When to choose NEW even with an active `awaiting_user` thread:\n"
    "  • The message clearly opens a different topic with no overlap with\n"
    "    that thread's title or last_agent excerpt (e.g. thread is about\n"
    "    agenda repurposing, message is \"check my emails\").\n"
    "  • The message starts with explicit \"new task:\", \"separately,\",\n"
    "    \"unrelated:\", or similar.\n\n"
    "Respond with ONLY a JSON object (no markdown, no commentary):\n"
    '  {\"kind\": \"new\"}                              — new thread\n'
    '  {\"kind\": \"existing\", \"thread_id\": \"ab12cd\"}  — continue that thread\n'
    '  {\"kind\": \"system\"}                           — agent task-list query only\n\n'
    "Tie-breakers:\n"
    "  • A message containing a tag like `#thread-ab12cd` strongly implies\n"
    "    EXISTING with that thread_id. Trust message semantics if they\n"
    "    clearly disagree with the tag.\n"
    "  • If exactly one thread is in `awaiting_user`, prefer EXISTING with\n"
    "    that thread unless the message clearly opens an unrelated topic.\n"
    "  • When unsure between SYSTEM and NEW, choose NEW. The agent will\n"
    "    route to the right skill from there."
)


_TITLE_GEN_PROMPT = (
    "You write very short titles for chat threads. Given a user's first "
    "message, return a 3 to 6 word title that captures the intent. "
    "Rules:\n"
    "  • No quotes, no trailing punctuation.\n"
    "  • Use Title Case.\n"
    "  • Drop filler words (please, can you, I want to, etc.).\n"
    "  • Prefer concrete nouns: customer name, document type, action.\n"
    "  • Max ~40 characters.\n"
    "Return ONLY the title text, nothing else."
)


def generate_thread_title(user_input: str) -> str:
    """Use the mini model to produce a short title for a new thread.

    Returns a fallback truncation of the user input if the LLM call fails
    or returns something unusable.
    """
    fallback = (user_input or "").strip().splitlines()[0][:60] if user_input else ""
    text = (user_input or "").strip()
    if not text:
        return fallback or "New conversation"
    try:
        client = get_responses_client()
        resp = client.responses.create(
            model=CHAT_MODEL_SMALL,
            instructions=_TITLE_GEN_PROMPT,
            input=[{"role": "user", "content": text[:2000]}],
            tools=[],
        )
        out = ""
        for item in resp.output:
            if item.type == "message":
                for part in item.content:
                    if part.type == "output_text":
                        out += part.text
        title = out.strip().strip('"').strip("'").rstrip(".").strip()
        # First line only, hard cap
        title = title.splitlines()[0][:60] if title else ""
        return title or fallback or "New conversation"
    except Exception as e:
        logger.warning("[TitleGen] Title generation failed: %s — using fallback", e)
        return fallback or "New conversation"


def classify_inbox(text: str, active_threads_summary: list[dict]) -> dict:
    """Classify an incoming remote message against the user's active threads.

    `active_threads_summary` is a list of dicts like
    `{id, title, skill_name, status, last_user_excerpt, hitl_correlation_tag}`.

    Returns `{"kind": "new" | "existing" | "system", "thread_id"?: str}`.
    Falls back to `{"kind": "new"}` on any parse failure.
    """
    if not active_threads_summary:
        # No active threads — only "new" or "system" are meaningful.
        summary_block = "(no active threads)"
    else:
        lines = []
        for s in active_threads_summary:
            lines.append(
                f"- id={s.get('id')} tag={s.get('hitl_correlation_tag', '')} "
                f"skill={s.get('skill_name')} status={s.get('status')} "
                f"title={s.get('title', '')[:60]!r} "
                f"last_user={s.get('last_user_excerpt', '')[:80]!r} "
                f"last_agent={s.get('last_agent_excerpt', '')[:160]!r}"
            )
        summary_block = "\n".join(lines)

    prompt = (
        _INBOX_CLASSIFIER_PROMPT
        + "\n\nActive threads:\n" + summary_block
        + "\n\nIncoming message:\n" + text.strip()
    )
    try:
        client = get_responses_client()
        resp = client.responses.create(
            model=CHAT_MODEL_SMALL,
            instructions=prompt,
            input=[{"role": "user", "content": text}],
            tools=[],
        )
        out = ""
        for item in resp.output:
            if item.type == "message":
                for part in item.content:
                    if part.type == "output_text":
                        out += part.text
        result = json.loads(out.strip())
        kind = result.get("kind")
        if kind not in ("new", "existing", "system"):
            return {"kind": "new"}
        if kind == "existing":
            tid = result.get("thread_id")
            valid_ids = {s.get("id") for s in active_threads_summary}
            if not tid or tid not in valid_ids:
                logger.warning("[InboxClassifier] 'existing' with unknown thread_id=%r — treating as new", tid)
                return {"kind": "new"}
            return {"kind": "existing", "thread_id": tid}
        return {"kind": kind}
    except Exception as e:
        logger.warning("[InboxClassifier] Classification failed: %s — defaulting to 'new'", e)
        return {"kind": "new"}


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def handle_tool_call(name: str, arguments: str, on_progress=None) -> str:
    """Execute a tool call by dispatching it to the owning MCP server.

    Per SKILLS_DESIGN_PRINCIPLES.md §14.1 / §11: the agent loop is a thin
    bridge — it submits whatever tool the model asked for and returns the
    result string. No mutation, no chaining, no marker parsing.
    """
    args = json.loads(arguments) if arguments else {}
    server = _TOOL_TO_SERVER.get(name)
    if server is None:
        return json.dumps({"status": "error", "tool": name,
                            "error": f"unknown tool '{name}' (no MCP server owns it)"})
    return mcp_pool.call(server, name, args, on_progress=on_progress)


# ---------------------------------------------------------------------------
# Conversation state — now thread-scoped.
#
# Previously this module held single-slot globals
# (`_conversation_histories` keyed by skill name, and a single `_active_session`).
# Those caused cross-contamination when multiple conversations ran in parallel.
#
# The new model pushes all conversation state onto `ConversationThread`
# objects owned by `thread_manager.ThreadManager`. A skill runs against one
# thread at a time and all mutations go through the manager.
# ---------------------------------------------------------------------------

# Imported lazily to keep `agent_core` importable by tools without requiring
# the thread subsystem.
def _get_thread_manager():
    from hub_cowork.core.thread_manager import get_manager
    return get_manager()


def reset_thread(thread_id: str) -> None:
    """Clear conversation state on a specific thread (messages, active_session,
    previous_response_id). The thread itself remains."""
    tm = _get_thread_manager()
    t = tm.get(thread_id)
    if not t:
        return
    t.messages.clear()
    t.active_session = None
    t.previous_response_id = None
    t.touch()
    tm._store.save(t)  # direct save — we just mutated in place
    logger.info("[thread %s] Conversation state reset", thread_id)


# Kept as a no-op shim for any caller that hasn't migrated yet. The UI's
# "New conversation" button now creates a fresh thread instead.
def reset_qa_history() -> None:
    """Deprecated: no-op. Per-thread state has replaced the single history."""
    logger.info("reset_qa_history() called — no-op in thread-scoped model")


class Cancelled(Exception):
    """Raised when a thread's run is cancelled cooperatively by the user.

    The agent loop polls a caller-supplied `is_cancelled()` callable between
    LLM turns and before tool dispatch / skill chaining. The ThreadExecutor
    catches this and marks the thread as `cancelled`.
    """
    pass


# ---------------------------------------------------------------------------
# Generic skill runner (thread-scoped)
# ---------------------------------------------------------------------------

def _run_skill(skill: "Skill", thread, user_input: str,
               on_progress=None, is_cancelled=None) -> str:
    """Run a skill against a ConversationThread — the canonical Responses-API loop.

    Per SKILLS_DESIGN_PRINCIPLES.md §14 / §15:
      - Conversation context flows through `previous_response_id` only. The
        thread's `messages` list is a write-only side channel for the UI to
        re-render the transcript; it is NEVER fed back into the LLM input.
      - The runtime does no marker parsing, no skill chaining, no result
        mutation. It dispatches `function_call` items the model produced,
        submits `function_call_output`, and loops until no more tool calls.
      - HITL is detected by a simple heuristic on the final text (trailing
        question mark) and surfaced as `thread.status = awaiting_user`.
    """
    from hub_cowork.core.thread_manager import get_manager
    tm = get_manager()

    client = get_responses_client()

    # Always feed a single user message. The model sees prior context via
    # `previous_response_id`. Do NOT replay thread.messages.
    input_messages = [{"role": "user", "content": user_input}]
    logger.info("[%s/%s] Query: %s", thread.id, skill.name, user_input)

    if on_progress and skill.tool_names:
        on_progress("step", f"{skill.name}: starting...")

    tools = skill.tools or []
    api_kwargs: dict = dict(
        model=skill.model,
        instructions=skill.instructions,
        input=input_messages,
        tools=tools if tools else [],
    )
    if skill.reasoning_effort:
        api_kwargs["reasoning"] = {"effort": skill.reasoning_effort}
    if thread.previous_response_id:
        api_kwargs["previous_response_id"] = thread.previous_response_id
    response = client.responses.create(**api_kwargs)

    # Tool-call loop (only if the skill has any tools).
    if tools:
        step = 1
        # Tools whose progress is already user-visible (or that are pure bookkeeping)
        # don't need an extra "Step N: <tool>" progress line.
        _silent_tools = {"log_progress", "get_hub_config"}
        while True:
            if is_cancelled and is_cancelled():
                raise Cancelled()
            for item in response.output:
                if hasattr(item, "type") and item.type == "reasoning":
                    for part in getattr(item, "summary", []):
                        logger.info("[%s/%s] Reasoning: %s", thread.id, skill.name,
                                    getattr(part, "text", ""))

            tool_calls = [item for item in response.output if item.type == "function_call"]
            if not tool_calls:
                break

            tool_results = []
            for tc in tool_calls:
                # Compact arg preview so the Logs pane shows what was asked.
                _args_preview = (tc.arguments or "").strip()
                if len(_args_preview) > 600:
                    _args_preview = _args_preview[:600] + "…(truncated)"
                logger.info("[%s/%s Step %d] -> %s args=%s",
                            thread.id, skill.name, step, tc.name, _args_preview)
                if on_progress and tc.name not in _silent_tools:
                    on_progress("step", f"Step {step}: {tc.name}")
                result = handle_tool_call(tc.name, tc.arguments, on_progress)
                _result_preview = (result or "").strip().replace("\n", " ")
                if len(_result_preview) > 800:
                    _result_preview = _result_preview[:800] + "…(truncated)"
                logger.info("[%s/%s Step %d] <- %s result=%s",
                            thread.id, skill.name, step, tc.name, _result_preview)
                tool_results.append({
                    "type": "function_call_output",
                    "call_id": tc.call_id,
                    "output": result,
                })

            step += 1
            client = get_responses_client()
            loop_kwargs: dict = dict(
                model=skill.model,
                instructions=skill.instructions,
                input=tool_results,
                tools=tools,
                previous_response_id=response.id,
            )
            if skill.reasoning_effort:
                loop_kwargs["reasoning"] = {"effort": skill.reasoning_effort}
            response = client.responses.create(**loop_kwargs)

    # Extract final assistant text.
    final_text = ""
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    final_text += part.text

    # Persist the latest response id for future follow-ups.
    tm.set_previous_response_id(thread.id, response.id)

    # Transitional courtesy: strip legacy control-flow markers from the
    # *displayed* text so users don't see "[AWAITING_CONFIRMATION]" tokens
    # while we migrate the older SKILL.md prose. The runtime no longer
    # branches on these markers.
    persisted_text = final_text or ""
    for _marker in ("[AWAITING_CONFIRMATION]", "[STOP_CHAIN]"):
        persisted_text = persisted_text.replace(_marker, "")
    persisted_text = persisted_text.strip()

    # UI side channel only — never read back as LLM input.
    if persisted_text:
        tm.append_message(thread.id, "assistant", persisted_text,
                          request_id=thread.last_request_id)

    # HITL heuristic: if the model ends its message with a question, treat
    # the thread as awaiting the user. Otherwise mark complete via the
    # executor's default ("completed"). See SKILLS_DESIGN_PRINCIPLES §14.4.
    if persisted_text.rstrip().endswith("?"):
        tm.set_status(thread.id, "awaiting_user")
        logger.info("[%s/%s] Awaiting user reply (trailing question).",
                    thread.id, skill.name)

    return persisted_text


# ---------------------------------------------------------------------------
# Master entry points
# ---------------------------------------------------------------------------

def route(user_input: str) -> str:
    """Public wrapper around the router — returns the skill name."""
    return _route(user_input)


def get_skill(name: str):
    """Return a Skill by name, or None."""
    return _skills.get(name)


def _run_none_skill(user_input: str) -> str:
    """Direct LLM reply for greetings / small talk (no skill invoked)."""
    client = get_responses_client()
    resp = client.responses.create(
        model=CHAT_MODEL,
        instructions=(
            "You are Hub SE Agent, a friendly helper for Microsoft 365 data. "
            "Respond briefly and naturally to greetings and small talk. "
            "Let the user know you can help with their M365 data — calendar, emails, "
            "documents, contacts — or create engagement agendas and meeting invites."
        ),
        input=[{"role": "user", "content": user_input}],
        tools=[],
    )
    reply = ""
    for item in resp.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    reply += part.text
    return reply or "Hey! How can I help you today?"


def run_skill_on_thread(thread, skill_name: str, user_input: str,
                        on_progress=None, is_cancelled=None) -> str:
    """Run a specific skill on a ConversationThread. Updates thread state
    (skill_name, status, messages, active_session, previous_response_id)
    through the ThreadManager."""
    from hub_cowork.core.thread_manager import get_manager
    tm = get_manager()

    if skill_name == "none":
        logger.info("[%s] Handling as small talk — direct LLM reply", thread.id)
        # Small talk bypasses skills but still lives on the thread so the
        # user sees their own message + the reply in context.
        tm.append_message(thread.id, "user", user_input,
                          request_id=thread.last_request_id)
        reply = _run_none_skill(user_input)
        tm.append_message(thread.id, "assistant", reply,
                          request_id=thread.last_request_id)
        return reply

    skill = _skills.get(skill_name)
    if not skill:
        logger.warning("Skill '%s' not found — falling back to qa", skill_name)
        skill = _skills.get("qa")

    # Record the skill on the thread so the UI can label it.
    if skill:
        tm.set_skill(thread.id, skill.name)
        if on_progress:
            on_progress("agent", skill.name)

    return _run_skill(skill, thread, user_input, on_progress,
                      is_cancelled=is_cancelled)


def run_agent_on_thread(thread, user_input: str, on_progress=None,
                        is_cancelled=None) -> str:
    """Route (if the thread doesn't yet have a skill) and run on the thread.

    After the first message of a thread, the skill is fixed for that thread
    and subsequent messages skip routing.
    """
    from hub_cowork.core.thread_manager import get_manager
    tm = get_manager()

    if thread.skill_name:
        skill_name = thread.skill_name
        logger.info("[%s] Continuing with pinned skill: %s", thread.id, skill_name)
    else:
        skill_name = _route(user_input)
        if skill_name != "none":
            tm.set_skill(thread.id, skill_name)
    return run_skill_on_thread(thread, skill_name, user_input, on_progress,
                               is_cancelled=is_cancelled)


# ---------------------------------------------------------------------------
# Legacy entry points — kept as thin wrappers for the (few) callers that
# haven't moved to the thread-scoped API yet (e.g. agent.py REPL).
#
# These create an ephemeral thread under the hood so state still goes
# through the ThreadManager; they will eventually be removed.
# ---------------------------------------------------------------------------

def run_skill(skill_name: str, user_input: str, on_progress=None) -> str:
    from hub_cowork.core.thread_manager import get_manager
    tm = get_manager()
    thread = tm.create(title=user_input[:60] or "Legacy call", source="ui")
    return run_skill_on_thread(thread, skill_name, user_input, on_progress)


def run_agent(user_input: str, on_progress=None) -> str:
    from hub_cowork.core.thread_manager import get_manager
    tm = get_manager()
    thread = tm.create(title=user_input[:60] or "Legacy call", source="ui")
    return run_agent_on_thread(thread, user_input, on_progress)
