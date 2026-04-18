"""
ConversationThread — the unit of work in the multi-thread agent model.

Each user task is a thread with its own history, active-session state,
progress log, and per-thread code log. Threads persist to local JSON so
users can re-open them, continue the LLM conversation via the Azure OpenAI
Responses API (`previous_response_id`), or archive them.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


# Status values
# active       — created, accepting input, not currently executing
# running      — executor is mid-LLM/tool loop
# awaiting_user — skill emitted [AWAITING_CONFIRMATION], paused for user reply
# completed    — last run finished normally (user can still send follow-ups)
# failed       — last run raised; user can retry by sending a new message
# archived     — hidden from default list; read-only until unarchived
VALID_STATUS = {"active", "running", "awaiting_user", "completed", "failed", "archived"}


# Default bound on per-thread code_log entries. Older entries drop off.
CODE_LOG_CAP = 1000


@dataclass
class ConversationThread:
    """A single user conversation, persistable and resumable."""

    id: str
    title: str
    skill_name: str | None = None      # set on first user message after routing
    source: str = "ui"                  # "ui" | "remote" | "system"
    external_user: str | None = None    # Teams user email when source == "remote"
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Conversation state passed to the skill runner
    messages: list[dict] = field(default_factory=list)          # [{role, content, ts, request_id}]
    active_session: dict | None = None                           # {"skill_name": str, "stage": str}
    previous_response_id: str | None = None                      # Responses API continuation

    # Observability
    progress_log: list[dict] = field(default_factory=list)      # [{ts, kind, message, request_id}]
    code_log: list[dict] = field(default_factory=list)          # [{ts, level, logger, msg}]

    # Per-run correlation
    last_request_id: str | None = None                          # most recent request_id

    # Traceability for HITL round-trip through Teams
    hitl_correlation_tag: str = ""                              # e.g. "#thread-ab12cd"

    # Reserved for future external archive stores (e.g. Cosmos DB). When set,
    # `archive_location` points to the canonical external record and the
    # local archive file may hold only a stub.
    archive_location: str | None = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def new(cls, *, title: str = "", source: str = "ui",
            external_user: str | None = None) -> "ConversationThread":
        tid = uuid.uuid4().hex[:8]
        return cls(
            id=tid,
            title=title or "New conversation",
            source=source,
            external_user=external_user,
            hitl_correlation_tag=f"#thread-{tid}",
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationThread":
        """Rehydrate from a dict loaded from JSON."""
        # Tolerate missing fields so older persisted threads still load.
        allowed = {
            "id", "title", "skill_name", "source", "external_user", "status",
            "created_at", "updated_at", "messages", "active_session",
            "previous_response_id", "progress_log", "code_log",
            "last_request_id", "hitl_correlation_tag", "archive_location",
        }
        clean = {k: v for k, v in data.items() if k in allowed}
        clean.setdefault("messages", [])
        clean.setdefault("progress_log", [])
        clean.setdefault("code_log", [])
        if not clean.get("hitl_correlation_tag") and clean.get("id"):
            clean["hitl_correlation_tag"] = f"#thread-{clean['id']}"
        return cls(**clean)

    def summary(self) -> dict[str, Any]:
        """Lightweight summary for list views — no heavy log arrays."""
        last_user_excerpt = ""
        for m in reversed(self.messages):
            if m.get("role") == "user":
                last_user_excerpt = (m.get("content") or "")[:120]
                break
        return {
            "id": self.id,
            "title": self.title,
            "skill_name": self.skill_name,
            "source": self.source,
            "external_user": self.external_user,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.messages),
            "last_user_excerpt": last_user_excerpt,
            "hitl_correlation_tag": self.hitl_correlation_tag,
            "archive_location": self.archive_location,
        }

    # ------------------------------------------------------------------
    # Mutation helpers (callers are expected to hold ThreadManager's lock)
    # ------------------------------------------------------------------

    def touch(self) -> None:
        self.updated_at = time.time()

    def append_message(self, role: str, content: str, *,
                       request_id: str | None = None) -> None:
        self.messages.append({
            "role": role,
            "content": content,
            "ts": time.time(),
            "request_id": request_id,
        })
        self.touch()

    def append_progress(self, kind: str, message: str, *,
                        request_id: str | None = None) -> None:
        self.progress_log.append({
            "ts": time.time(),
            "kind": kind,
            "message": message,
            "request_id": request_id,
        })
        self.touch()

    def append_code_log(self, entry: dict) -> None:
        self.code_log.append(entry)
        if len(self.code_log) > CODE_LOG_CAP:
            # Keep the newest CODE_LOG_CAP entries
            del self.code_log[: len(self.code_log) - CODE_LOG_CAP]
        # code_log writes are high-volume — don't touch() on every one;
        # ThreadManager's coalesced save handles persistence cadence.

    def conversational_history(self) -> list[dict]:
        """Return the message list in the shape the Responses API expects
        (role + content only)."""
        return [{"role": m["role"], "content": m["content"]} for m in self.messages]
