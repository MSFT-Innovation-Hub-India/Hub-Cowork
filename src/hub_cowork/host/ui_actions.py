"""
Settings-UI server actions.

Ad-hoc server-side actions triggered directly by the Settings modal in the
chat UI (not by any skill). Each action runs in its own worker thread and
broadcasts progress/result events over the WebSocket so the UI can update
in place.

Currently:
  - validate_speakers — batch-resolves a list of speaker names via WorkIQ
    so the user can sanity-check the speaker roster they configured.
"""

from __future__ import annotations

import logging

from hub_cowork.core.thread_manager import (
    current_thread_id as _current_thread_id,
    SYSTEM_THREAD_ID,
)

logger = logging.getLogger("hub_se_agent")


def run_validate_speakers(
    request_id: str | None,
    names: list[str],
    *,
    broadcast,
):
    """Batch-resolve speaker names via WorkIQ and broadcast the result.

    Triggered by the Settings modal → "Validate all" button. Runs off the
    asyncio event loop because the WorkIQ CLI call can take several seconds.

    `broadcast` is the host's `_broadcast(msg: dict)` callable.
    """
    from hub_cowork.tools.resolve_speakers import resolve as _resolve
    from hub_cowork.core.agent_core import WORKIQ_CLI

    def on_progress(kind: str, message: str):
        broadcast({"type": "validate_speakers_progress",
                   "request_id": request_id,
                   "kind": kind, "message": message})

    broadcast({"type": "validate_speakers_started",
               "request_id": request_id,
               "count": len(names)})
    token = _current_thread_id.set(SYSTEM_THREAD_ID)
    try:
        result = _resolve(names, workiq_cli=WORKIQ_CLI,
                          on_progress=on_progress)
        broadcast({"type": "speakers_validated",
                   "request_id": request_id,
                   "results": result.get("results", [])})
    except Exception as e:
        logger.error("validate_speakers [%s] failed: %s",
                     request_id, e, exc_info=True)
        broadcast({"type": "speakers_validated",
                   "request_id": request_id,
                   "error": str(e)[:500],
                   "results": []})
    finally:
        _current_thread_id.reset(token)
