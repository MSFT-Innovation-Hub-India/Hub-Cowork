"""
Standard tool-result envelope.

All integration tools (WorkIQ, FoundryIQ, Fabric Data Agent, etc.) return a
JSON-encoded envelope string so the calling LLM can distinguish three
mutually exclusive outcomes with no guesswork:

    {"status": "ok",      "data": "..."}            # success with data
    {"status": "no_data", "message": "..."}         # service responded, no match
    {"status": "error",   "kind": "...",
                           "message": "..."}         # transport/config/remote failure

`kind` for errors is one of:
    "config"     — missing env var / not installed
    "auth"       — 401/403/token acquisition failed
    "timeout"    — transport timeout or run polling exceeded budget
    "network"    — connection refused/reset/DNS
    "remote"     — 5xx / service-reported failure / non-zero process exit
    "unexpected" — anything else

Why JSON envelopes instead of free-text / sentinel prefixes
-----------------------------------------------------------
1. LLM-friendly: the OpenAI Responses API passes tool output as a string.
   JSON is the lingua franca; both GPT-4 / GPT-5 class models parse it
   reliably and the `status` field is explicit, not inferred.
2. Machine-countable: every call emits one structured log line on the
   `hub_se_agent.tool_metrics` logger, so outcomes can be tallied from the
   log without rewriting the tools.
3. Stable contract: callers (skill instructions) can reason about all
   possible shapes with a closed schema.
"""

from __future__ import annotations

import json
import logging
from typing import Any

# Dedicated logger so metrics can be filtered / routed independently of
# the general agent log. Propagates to the parent `hub_se_agent` handler
# by default, so it appears in agent.log with the tag `[tool_metric]`.
_metric_logger = logging.getLogger("hub_se_agent.tool_metrics")


def _report_service(tool: str, status: str, kind: str = "") -> None:
    """Forward this outcome to the service connectivity monitor.

    Imported lazily to avoid a circular import at module load (the monitor
    imports tool modules when probing, tools import this helper). Any
    failure here is silently dropped — telemetry must not break tools.
    """
    try:
        from hub_cowork.core.service_status import get_monitor
        get_monitor().mark_from_envelope(tool, status, kind)
    except Exception:
        pass


ErrorKind = str  # Literal would require 3.12+ typing import; keep loose.


def ok(tool: str, data: str, *, meta: dict[str, Any] | None = None) -> str:
    """Successful call with data."""
    payload: dict[str, Any] = {"status": "ok", "data": data}
    if meta:
        payload["meta"] = meta
    _metric_logger.info(
        "[tool_metric] tool=%s outcome=ok chars=%d", tool, len(data or "")
    )
    _report_service(tool, "ok")
    return json.dumps(payload, ensure_ascii=False)


def no_data(tool: str, message: str, *, query: str | None = None) -> str:
    """
    Service responded successfully but has no matching data for this query.

    This is NOT an error — the agent should adapt (try a different query,
    tell the user) rather than retry the same call.
    """
    payload: dict[str, Any] = {"status": "no_data", "message": message}
    if query:
        payload["query"] = query[:200]
    _metric_logger.info("[tool_metric] tool=%s outcome=no_data", tool)
    _report_service(tool, "no_data")
    return json.dumps(payload, ensure_ascii=False)


def error(tool: str, kind: ErrorKind, message: str) -> str:
    """Transport / config / remote failure. Data state is unknown."""
    payload = {"status": "error", "kind": kind, "message": message}
    _metric_logger.warning(
        "[tool_metric] tool=%s outcome=error kind=%s", tool, kind
    )
    _report_service(tool, "error", kind)
    return json.dumps(payload, ensure_ascii=False)


# Short human-readable description used in skill instructions and docs.
ENVELOPE_DESCRIPTION = """\
Every integration tool (query_workiq, search_foundryiq, query_fabric_agent) \
returns a JSON envelope string with one of three shapes:

  {"status": "ok", "data": "..."}            — use `data` as the result
  {"status": "no_data", "message": "..."}    — service responded but found \
nothing; do NOT retry the same call, either reformulate the query or tell \
the user the information is not available
  {"status": "error", "kind": "...", "message": "..."}   — transport / auth \
/ config failure; do NOT fabricate data, surface the failure to the user. \
`kind` is one of: config, auth, timeout, network, remote, unexpected.
"""
