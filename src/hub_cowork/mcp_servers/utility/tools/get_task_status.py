"""
Tool: get_task_status — returns a summary of all conversation threads.

In the multi-thread model there is no central queue; instead each active
conversation is a ConversationThread managed by ThreadManager. This tool
returns a snapshot of everything the user might care about when asking
"what's running?": active tasks, their status, recent progress, and
correlation tags so the user can quote them back in Teams.
"""

import json
import time

SCHEMA = {
    "type": "function",
    "name": "get_task_status",
    "description": (
        "Get the status of all active conversation threads (tasks). Shows "
        "which tasks are running, which are waiting for user confirmation, "
        "which completed recently, their last progress step, and their "
        "correlation tag (e.g. #thread-ab12cd) so users can reference a "
        "specific task in a follow-up message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "external_user": {
                "type": "string",
                "description": (
                    "Optional filter — return only threads for this external "
                    "user (typically the sender email for remote Teams "
                    "requests). Omit for all threads."
                ),
            },
            "include_completed": {
                "type": "boolean",
                "description": "Include completed threads in the result (default true).",
            },
        },
        "required": [],
    },
}


def _summarize(thread) -> dict:
    now = time.time()
    age_minutes = round((now - thread.created_at) / 60, 1)
    last_progress = None
    if thread.progress_log:
        entry = thread.progress_log[-1]
        # Progress entries are dicts ({ts, kind, message, request_id}) in the
        # current schema; older threads may still have (ts, kind, msg) tuples.
        if isinstance(entry, dict):
            t = entry.get("ts") or now
            kind = entry.get("kind") or ""
            msg = entry.get("message") or ""
        else:
            try:
                t, kind, msg = entry[0], entry[1], entry[2]
            except Exception:
                t, kind, msg = now, "", str(entry)
        last_progress = {
            "kind": kind,
            "message": (msg or "")[:200],
            "seconds_ago": round(now - t, 1),
        }
    return {
        "thread_id": thread.id,
        "correlation_tag": thread.hitl_correlation_tag,
        "title": thread.title,
        "status": thread.status,
        "skill": thread.skill_name,
        "source": thread.source,
        "external_user": thread.external_user,
        "age_minutes": age_minutes,
        "awaiting_user": thread.status == "awaiting_user",
        "last_progress": last_progress,
    }


def handle(arguments: dict, **kwargs) -> str:
    from hub_cowork.core.thread_manager import get_manager

    tm = get_manager()
    args = arguments or {}
    external_user = args.get("external_user") or None
    include_completed = args.get("include_completed", True)

    statuses = ("active", "running", "awaiting_user")
    if include_completed:
        statuses = statuses + ("completed", "failed")

    threads = tm.list(external_user=external_user, statuses=statuses)

    buckets: dict[str, list] = {
        "running": [], "awaiting_user": [], "active": [],
        "completed": [], "failed": [],
    }
    for t in threads:
        buckets.setdefault(t.status, []).append(_summarize(t))

    result = {
        "total": len(threads),
        "counts": {k: len(v) for k, v in buckets.items()},
        "running": buckets["running"],
        "awaiting_user": buckets["awaiting_user"],
        "active": buckets["active"],
        "completed": buckets["completed"][-5:],
        "failed": buckets["failed"][-5:],
    }
    return json.dumps(result, indent=2)
