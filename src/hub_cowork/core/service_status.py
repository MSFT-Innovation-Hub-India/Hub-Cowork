"""
Service connectivity status monitor.

Tracks reachability for the four external services the agent talks to, so
the UI can render a little green/red dot per service:

    workiq        — local WorkIQ CLI  (grouped under "MicrosoftIQ")
    foundryiq     — FoundryIQ Azure AI Search  (grouped under "MicrosoftIQ")
    fabric_agent  — Fabric Data Agent  (grouped under "MicrosoftIQ")
    redis_teams   — Redis bridge + agent presence registration for the
                    Teams relay

Design
------
Connectivity is the determinant of status; empty / no-match results are NOT
failures. Status values:

    "ok"            — service reachable (last call returned `ok` or `no_data`)
    "down"          — transport / auth / remote failure
    "unconfigured"  — required env vars / binary missing
    "unknown"       — haven't probed or been called yet

Two update paths feed the monitor:

1. Passive tracking — every time a tool returns a `_tool_result` envelope,
   `mark_from_envelope()` flips the state for that service:
     - envelope status "ok" or "no_data"  → ok
     - envelope status "error" kind=config → unconfigured
     - envelope status "error" any other  → down
   This matches the user's intent: semantic "no data" does not turn the
   dot red.

2. Active probes — a background thread runs lightweight probes on startup
   and every `_PROBE_INTERVAL` seconds so stale state (nobody has called
   WorkIQ in a while) doesn't show as permanent "unknown". Probes only
   touch services that are currently `unknown` or where the last update
   is older than `_PROBE_INTERVAL`, to avoid burning the real credentials
   when the tool traffic is already telling us the answer.

The Redis bridge reports its own state through `mark()` directly — the
definition of "channel established" is Redis connected AND the agent
presence key successfully registered, which the bridge knows first-hand.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Literal

logger = logging.getLogger("hub_se_agent")

ServiceName = Literal["workiq", "foundryiq", "fabric_agent", "redis_teams"]
ServiceStatus = Literal["ok", "down", "unconfigured", "unknown"]

SERVICE_NAMES: tuple[ServiceName, ...] = (
    "workiq", "foundryiq", "fabric_agent", "redis_teams",
)

# How often the active probe thread re-checks services that have gone
# quiet. Keep it gentle — this is for UI freshness, not health monitoring.
_PROBE_INTERVAL = 120.0  # 2 minutes
_PROBE_TIMEOUT = 6.0


class _ServiceStatusMonitor:
    """Thread-safe singleton tracking per-service connectivity."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {
            name: {
                "status": "unknown",
                "detail": "",
                "checked_at": 0.0,
            }
            for name in SERVICE_NAMES
        }
        self._on_change: Callable[[dict], None] | None = None
        self._probe_thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------
    def set_broadcast(self, callback: Callable[[dict], None]) -> None:
        """Callback invoked on any state change with a full snapshot dict."""
        self._on_change = callback

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "type": "service_status",
                "services": {k: dict(v) for k, v in self._state.items()},
            }

    # ------------------------------------------------------------------
    # Updaters
    # ------------------------------------------------------------------
    def mark(
        self,
        service: ServiceName,
        status: ServiceStatus,
        detail: str = "",
    ) -> None:
        """Update one service. Broadcasts a snapshot if the status actually changed."""
        if service not in self._state:
            return
        changed = False
        with self._lock:
            cur = self._state[service]
            if cur["status"] != status or cur["detail"] != detail:
                changed = True
            cur["status"] = status
            cur["detail"] = detail
            cur["checked_at"] = time.time()
        if changed:
            logger.info(
                "[service_status] %s -> %s (%s)", service, status, detail or "-"
            )
            self._broadcast()

    def mark_from_envelope(self, tool: str, envelope_status: str, kind: str = "") -> None:
        """Record the outcome of a tool call.

        Empty / no-match results (envelope_status == "no_data") are treated
        as `ok` because the service is plainly reachable. `error` with
        kind="config" becomes `unconfigured`; anything else becomes `down`.
        """
        service = _TOOL_TO_SERVICE.get(tool)
        if service is None:
            return
        if envelope_status in ("ok", "no_data"):
            self.mark(service, "ok", "")
            return
        if envelope_status == "error":
            if kind == "config":
                self.mark(service, "unconfigured", f"config missing ({kind})")
            else:
                self.mark(service, "down", f"error: {kind or 'unknown'}")

    # ------------------------------------------------------------------
    # Active probes
    # ------------------------------------------------------------------
    def start_probes(self) -> None:
        """Launch the background probe thread. Safe to call multiple times."""
        if self._probe_thread and self._probe_thread.is_alive():
            return
        self._stopping.clear()
        self._probe_thread = threading.Thread(
            target=self._probe_loop, daemon=True, name="service-status-probe",
        )
        self._probe_thread.start()

    def stop(self) -> None:
        self._stopping.set()

    def _probe_loop(self) -> None:
        # One quick bootstrap pass so the UI gets real values within a few
        # seconds of startup, then settle into the long interval.
        time.sleep(2.0)
        while not self._stopping.is_set():
            try:
                self._probe_once()
            except Exception as e:
                logger.warning("Service probe cycle failed: %s", e)
            if self._stopping.wait(timeout=_PROBE_INTERVAL):
                return

    def _probe_once(self) -> None:
        now = time.time()
        # Only probe services whose state is older than the probe interval
        # OR still `unknown`. Skip `redis_teams` — the bridge owns that
        # signal and updates it first-hand.
        with self._lock:
            targets = [
                name for name in ("workiq", "foundryiq", "fabric_agent")
                if self._state[name]["status"] == "unknown"
                or (now - self._state[name]["checked_at"]) >= _PROBE_INTERVAL
            ]
        for name in targets:
            try:
                probe_fn = _PROBES[name]
                status, detail = probe_fn()
                self.mark(name, status, detail)
            except Exception as e:
                self.mark(name, "down", f"probe error: {e}")

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------
    def _broadcast(self) -> None:
        cb = self._on_change
        if cb is None:
            return
        try:
            cb(self.snapshot())
        except Exception as e:
            logger.warning("service_status broadcast failed: %s", e)


# Map tool name -> service it represents (for envelope-driven updates)
_TOOL_TO_SERVICE: dict[str, ServiceName] = {
    "query_workiq": "workiq",
    "search_foundryiq": "foundryiq",
    "query_fabric_agent": "fabric_agent",
}


# ---------------------------------------------------------------------------
# Lightweight active probes
# ---------------------------------------------------------------------------

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _is_signed_in() -> bool:
    """Return True only if the shared Azure credential has a cached token.

    Probes use this to avoid triggering interactive browser authentication
    from a background thread before the user has signed in via the UI.
    """
    try:
        from hub_cowork.core.agent_core import check_azure_auth
        ok, _ = check_azure_auth()
        return ok
    except Exception:
        return False


def _probe_workiq() -> tuple[ServiceStatus, str]:
    """Check that the workiq CLI is installed and responds to --version.

    We deliberately do NOT run a real query — that would hit M365 with a
    dummy question and pollute telemetry. `--version` just verifies the
    binary launches.

    On Windows the CLI ships as a .cmd shim (e.g. `workiq.cmd` from npm),
    and `subprocess.run(["workiq", ...])` without shell=True does NOT honor
    PATHEXT — CreateProcess only finds bare `workiq` if it has no extension.
    Use shutil.which() which DOES honor PATHEXT to resolve to the .cmd path.
    """
    cli = os.environ.get("WORKIQ_PATH") or shutil.which("workiq") or "workiq"
    try:
        result = subprocess.run(
            [cli, "--version"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return "unconfigured", "WorkIQ CLI not on PATH (set WORKIQ_PATH)"
    except subprocess.TimeoutExpired:
        return "down", "CLI --version timed out"
    except Exception as e:
        return "down", f"CLI launch failed: {e}"
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        return "down", f"CLI exited rc={result.returncode}: {stderr[:80]}"
    return "ok", ""


def _probe_foundryiq() -> tuple[ServiceStatus, str]:
    """Probe FoundryIQ by acquiring a token and hitting the configured KB.

    We issue a GET to `{endpoint}/knowledgebases('{kb}')?api-version=...`,
    which is the same resource the tool actually uses for `/retrieve`.
    Returns 200 if the KB exists, 404 if the KB name is wrong, 401/403 on
    auth failure. Using the same api-version as the tool guarantees we are
    talking to a surface the service supports (the `/indexes` endpoint is
    not exposed under the knowledge-base preview api-version).

    Important: probes must NEVER trigger interactive browser auth. If the
    user hasn't signed in yet, return "unknown" so the UI shows a neutral
    dot rather than spawning a popup window from a background thread.
    """
    try:
        from hub_cowork.skills.rfp_evaluation.tools.search_foundryiq import (
            _load_config, _get_credential, _get_bearer_token, _get_session,
        )
    except Exception as e:
        return "down", f"module import failed: {e}"
    cfg = _load_config()
    endpoint = (cfg.get("FOUNDRYIQ_ENDPOINT") or "").rstrip("/")
    tenant_id = cfg.get("RESOURCE_TENANT_ID") or ""
    api_version = cfg.get("FOUNDRYIQ_API_VERSION") or "2025-11-01-preview"
    kb_name = cfg.get("FOUNDRYIQ_KB_NAME") or "rfp-knowledge-store"
    if not endpoint:
        return "unconfigured", "FOUNDRYIQ_ENDPOINT not set"
    if not tenant_id:
        return "unconfigured", "RESOURCE_TENANT_ID not set"
    if not _is_signed_in():
        return "unknown", "waiting for sign-in"
    try:
        cred = _get_credential(tenant_id, cfg.get("FOUNDRYIQ_AUTH_MODE") or "browser")
        token = _get_bearer_token(cred)
    except Exception as e:
        return "down", f"token acquisition failed: {e}"
    try:
        resp = _get_session().get(
            f"{endpoint}/knowledgebases('{kb_name}')?api-version={api_version}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_PROBE_TIMEOUT,
        )
    except Exception as e:
        return "down", f"network: {e}"
    if resp.status_code == 200:
        return "ok", ""
    if resp.status_code in (401, 403):
        return "down", f"auth {resp.status_code}"
    if resp.status_code == 404:
        return "unconfigured", f"knowledge base '{kb_name}' not found"
    return "down", f"http {resp.status_code}"


def _probe_fabric_agent() -> tuple[ServiceStatus, str]:
    """Probe Fabric Data Agent by acquiring a bearer token for its scope.

    Token acquisition exercises the end-to-end auth chain (InteractiveBrowser
    credential → resource tenant → Fabric scope) without invoking the agent
    itself (which would spin up a thread + run on the Fabric side and cost
    real compute). A successful token implies the agent URL is reachable
    via the same network, since Entra ID lives on Azure public endpoints.
    """
    endpoint = (
        os.environ.get("FABRIC_DATA_AGENT_URL")
        or os.environ.get("DATA_AGENT_URL")
        or ""
    )
    tenant_id = (
        os.environ.get("RESOURCE_TENANT_ID")
        or os.environ.get("AZURE_TENANT_ID")
        or ""
    )
    if not endpoint:
        return "unconfigured", "FABRIC_DATA_AGENT_URL not set"
    if not tenant_id:
        return "unconfigured", "RESOURCE_TENANT_ID not set"
    if not _is_signed_in():
        return "unknown", "waiting for sign-in"
    try:
        # Reuse the same credential factory the tool itself uses so we
        # don't hold a second InteractiveBrowser instance open.
        from hub_cowork.skills.rfp_evaluation.tools.query_fabric_agent import _get_credential  # type: ignore
        cred = _get_credential(tenant_id, os.environ.get("FABRIC_AUTH_MODE") or "browser")
        # Fabric uses the PowerBI (Fabric) resource scope.
        token = cred.get_token("https://api.fabric.microsoft.com/.default")
        if not token or not token.token:
            return "down", "token acquisition returned empty"
    except Exception as e:
        msg = str(e)[:120]
        if "unauthorized" in msg.lower() or "401" in msg or "403" in msg:
            return "down", f"auth: {msg}"
        return "down", f"token error: {msg}"
    return "ok", ""


_PROBES: dict[ServiceName, Callable[[], tuple[ServiceStatus, str]]] = {
    "workiq": _probe_workiq,
    "foundryiq": _probe_foundryiq,
    "fabric_agent": _probe_fabric_agent,
}


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_instance: _ServiceStatusMonitor | None = None
_instance_lock = threading.Lock()


def get_monitor() -> _ServiceStatusMonitor:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = _ServiceStatusMonitor()
        return _instance
