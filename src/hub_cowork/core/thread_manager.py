"""
ThreadManager — owns all ConversationThreads, persists them, and fans out
change notifications to observers (WebSocket broadcast, Redis bridge, etc).

This module replaces the single-slot globals (`_conversation_histories`,
`_active_session`) that used to live in `agent_core.py`. Each thread is
addressable by id and carries its own history and active-session state.

Per-thread log scoping:
    `current_thread_id` is a ContextVar set by ThreadExecutor before
    dispatching to the skill runner. Logging handlers read this value to
    tag every log record with its thread, enabling per-thread log views
    in the UI.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Callable, Iterable

from hub_cowork.core.conversation_thread import ConversationThread
from hub_cowork.core.thread_store import LocalJsonThreadStore, ThreadArchiveStore

logger = logging.getLogger("hub_se_agent")


# ContextVar carrying the id of the thread whose code is currently executing.
# ThreadExecutor sets this at the top of every run; log handlers read it to
# attribute log records to the right ConversationThread.
current_thread_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_thread_id", default=None
)


# Signature for change observers — called with (event_type, thread_id, payload)
ChangeHandler = Callable[[str, str, dict], None]


# Sentinel thread id for the "system" pseudo-thread (cross-cutting queries).
SYSTEM_THREAD_ID = "system"


class ThreadManager:
    """Singleton-ish manager for all conversation threads.

    One instance per process — created at module import, configured by the
    host (`meeting_agent.py`) with a broadcast callback.
    """

    def __init__(self, store: LocalJsonThreadStore | None = None,
                 archive_store: ThreadArchiveStore | None = None):
        self._store = store or LocalJsonThreadStore()
        # Pluggable seam for external archive stores (Cosmos DB, etc).
        # Not instantiated by default — set via `set_archive_store()`.
        self._archive_store = archive_store
        self._threads: dict[str, ConversationThread] = {}
        self._lock = threading.RLock()
        self._observers: list[ChangeHandler] = []
        # Ephemeral threads (e.g. system pseudo-thread query runs) live in
        # `_threads` so the skill executor can mutate them, but are excluded
        # from persistence and observer broadcasts.
        self._ephemeral_ids: set[str] = set()

        # Load persisted active threads on startup.
        for t in self._store.load_all():
            # A thread that was "running" when the agent crashed is stale;
            # downgrade to "active" so the user can resume via a new message.
            if t.status == "running":
                t.status = "active"
            self._threads[t.id] = t
        logger.info("ThreadManager initialized with %d active thread(s)", len(self._threads))

    # ------------------------------------------------------------------
    # Observers
    # ------------------------------------------------------------------

    def add_observer(self, handler: ChangeHandler) -> None:
        self._observers.append(handler)

    def _emit(self, event: str, thread_id: str, payload: dict) -> None:
        if thread_id in self._ephemeral_ids:
            return  # ephemeral threads never broadcast
        for h in list(self._observers):
            try:
                h(event, thread_id, payload)
            except Exception as e:
                logger.warning("Thread observer raised: %s", e)

    def _save(self, t: ConversationThread) -> None:
        """Persist `t` unless it's ephemeral."""
        if t.id in self._ephemeral_ids:
            return
        self._store.save(t)

    # ------------------------------------------------------------------
    # External archive seam (future Cosmos DB, etc)
    # ------------------------------------------------------------------

    def set_archive_store(self, archive_store: ThreadArchiveStore | None) -> None:
        self._archive_store = archive_store

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, *, title: str = "", source: str = "ui",
               external_user: str | None = None,
               ephemeral: bool = False) -> ConversationThread:
        thread = ConversationThread.new(
            title=title, source=source, external_user=external_user,
        )
        with self._lock:
            self._threads[thread.id] = thread
            if ephemeral:
                self._ephemeral_ids.add(thread.id)
        if not ephemeral:
            self._store.save(thread)
            self._emit("thread_created", thread.id, thread.summary())
            logger.info("Thread %s created (source=%s)", thread.id, source)
        else:
            logger.info("Thread %s created ephemeral (source=%s)", thread.id, source)
        return thread

    def dispose_ephemeral(self, thread_id: str) -> None:
        """Drop an ephemeral thread — no persistence, no broadcast."""
        with self._lock:
            if thread_id in self._ephemeral_ids:
                self._threads.pop(thread_id, None)
                self._ephemeral_ids.discard(thread_id)

    def get(self, thread_id: str) -> ConversationThread | None:
        with self._lock:
            t = self._threads.get(thread_id)
            if t is not None:
                return t
        # Fall back to disk (e.g. opening an archived thread).
        return self._store.load(thread_id)

    def list(self, *, include_archived: bool = False,
             external_user: str | None = None,
             statuses: Iterable[str] | None = None) -> list[ConversationThread]:
        """List active in-memory threads, optionally filtered."""
        with self._lock:
            threads = [t for t in self._threads.values()
                       if t.id not in self._ephemeral_ids]
        if external_user is not None:
            threads = [t for t in threads if t.external_user == external_user]
        if statuses is not None:
            sset = set(statuses)
            threads = [t for t in threads if t.status in sset]
        if not include_archived:
            threads = [t for t in threads if t.status != "archived"]
        threads.sort(key=lambda t: t.updated_at, reverse=True)
        return threads

    def list_summaries(self, **kwargs) -> list[dict]:
        return [t.summary() for t in self.list(**kwargs)]

    def list_archived_summaries(self) -> list[dict]:
        return self._store.list_archived_summaries()

    def update_title(self, thread_id: str, title: str) -> None:
        with self._lock:
            t = self._threads.get(thread_id)
            if not t:
                return
            t.title = title
            t.touch()
            self._save(t)
        self._emit("thread_updated", thread_id, t.summary())

    def set_status(self, thread_id: str, status: str) -> None:
        with self._lock:
            t = self._threads.get(thread_id)
            if not t or t.status == status:
                return
            t.status = status
            t.touch()
            self._save(t)
        self._emit("thread_updated", thread_id, t.summary())

    def set_skill(self, thread_id: str, skill_name: str) -> None:
        with self._lock:
            t = self._threads.get(thread_id)
            if not t:
                return
            if t.skill_name == skill_name:
                return
            t.skill_name = skill_name
            # Pick up a friendlier default title from the first user message.
            if t.title in ("", "New conversation") and t.messages:
                first = next((m.get("content", "") for m in t.messages
                              if m.get("role") == "user"), "")
                if first:
                    t.title = first[:60]
            t.touch()
            self._save(t)
        self._emit("thread_updated", thread_id, t.summary())

    def set_active_session(self, thread_id: str, session: dict | None) -> None:
        with self._lock:
            t = self._threads.get(thread_id)
            if not t:
                return
            t.active_session = session
            t.touch()
            self._save(t)

    def set_previous_response_id(self, thread_id: str, response_id: str | None) -> None:
        with self._lock:
            t = self._threads.get(thread_id)
            if not t:
                return
            t.previous_response_id = response_id
            self._save(t)

    # ------------------------------------------------------------------
    # Message / progress / log appenders
    # ------------------------------------------------------------------

    def append_message(self, thread_id: str, role: str, content: str, *,
                       request_id: str | None = None) -> None:
        with self._lock:
            t = self._threads.get(thread_id)
            if not t:
                return
            t.append_message(role, content, request_id=request_id)
            if request_id:
                t.last_request_id = request_id
            self._save(t)
        self._emit("thread_message", thread_id, {
            "role": role, "content": content, "request_id": request_id,
        })

    def append_progress(self, thread_id: str, kind: str, message: str, *,
                        request_id: str | None = None) -> None:
        with self._lock:
            t = self._threads.get(thread_id)
            if not t:
                return
            t.append_progress(kind, message, request_id=request_id)
            self._save(t)
        self._emit("thread_progress", thread_id, {
            "kind": kind, "message": message, "request_id": request_id,
        })

    def append_code_log(self, thread_id: str, entry: dict) -> None:
        """Tag a log record onto a thread. Called from the WS log handler.

        This path is high-volume, so we skip `updated_at` bumps and let the
        store's debounced writer coalesce multiple log appends into one
        disk write.
        """
        with self._lock:
            t = self._threads.get(thread_id)
            if not t:
                return
            t.append_code_log(entry)
            self._save(t)
        self._emit("thread_log_entry", thread_id, entry)

    # ------------------------------------------------------------------
    # Archive / unarchive / delete
    # ------------------------------------------------------------------

    def archive(self, thread_id: str) -> bool:
        with self._lock:
            t = self._threads.pop(thread_id, None)
        if not t:
            return False
        t.status = "archived"
        t.touch()

        # If an external archive store is configured, persist there too and
        # remember the remote location on the thread.
        if self._archive_store is not None:
            try:
                t.archive_location = self._archive_store.archive(t.to_dict())
            except Exception as e:
                logger.error("External archive store failed for %s: %s", thread_id, e)

        self._store.archive(t)
        self._emit("thread_archived", thread_id, t.summary())
        logger.info("Thread %s archived", thread_id)
        return True

    def unarchive(self, thread_id: str) -> ConversationThread | None:
        thread = self._store.unarchive(thread_id)
        if thread is None:
            return None
        with self._lock:
            self._threads[thread.id] = thread
        self._emit("thread_updated", thread.id, thread.summary())
        logger.info("Thread %s unarchived", thread_id)
        return thread

    def delete(self, thread_id: str) -> bool:
        with self._lock:
            self._threads.pop(thread_id, None)
        self._store.delete(thread_id)
        self._emit("thread_deleted", thread_id, {"id": thread_id})
        return True

    def flush(self) -> None:
        """Persist any pending debounced writes. Call on shutdown."""
        self._store.flush_all()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: ThreadManager | None = None


def get_manager() -> ThreadManager:
    global _manager
    if _manager is None:
        _manager = ThreadManager()
    return _manager
