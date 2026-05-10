"""Per-thread session storage for the shelf_watch skill.

The `shelf_watch_run` orchestrator is multi-turn — it interleaves with
the user for SKU plausibility confirmation and variant disambiguation.
Each turn is a fresh tool call, so the orchestrator needs to remember
the discovery payload and the user's resolved scope between calls.

State is persisted to a per-thread JSON file under
`~/.hub-cowork/shelf_watch/sessions/<thread_id>.json` so it survives a
process restart and is naturally scoped to one ConversationThread.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from hub_cowork.core.app_paths import APP_HOME
from hub_cowork.core.thread_manager import current_thread_id

logger = logging.getLogger("hub_se_agent")

_SESSIONS_DIR = APP_HOME / "shelf_watch" / "sessions"


def _path(thread_id: str | None = None) -> Path:
    tid = thread_id or current_thread_id.get() or "system"
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return _SESSIONS_DIR / f"{tid}.json"


def load(thread_id: str | None = None) -> dict[str, Any]:
    """Return the saved session state, or {} if none exists."""
    p = _path(thread_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as ex:
        logger.warning("shelf_watch._session: failed to load %s (%s)", p, ex)
        return {}


def save(state: dict[str, Any], thread_id: str | None = None) -> None:
    p = _path(thread_id)
    try:
        p.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as ex:
        logger.warning("shelf_watch._session: failed to save %s (%s)", p, ex)


def clear(thread_id: str | None = None) -> None:
    p = _path(thread_id)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass
