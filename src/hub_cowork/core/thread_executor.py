"""
ThreadExecutor — one daemon thread per active ConversationThread.

Replaces the single-worker `TaskQueue._worker_loop`. Each conversation runs
independently so multiple users (or one user's multiple parallel tasks) can
execute LLM / tool work simultaneously without blocking each other.

Lifecycle:
    - Created on demand by `ExecutorPool.submit(thread_id, text)` when a user
      sends a message to a thread that has no running executor.
    - Reads user inputs from a per-thread `queue.Queue`.
    - Terminates when the queue is idle for `IDLE_SHUTDOWN_SECONDS`.
    - Re-spawned automatically on the next user input.

Observability:
    - Sets the `current_thread_id` ContextVar before each run so log handlers
      can tag records with the thread id.
    - Drives the thread's progress_log and calls user-supplied broadcast
      hooks for live UI updates.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from typing import Callable

from hub_cowork.core.thread_manager import current_thread_id, get_manager

logger = logging.getLogger("hub_se_agent")


# How long a per-thread executor stays alive after its queue drains.
# A new executor is cheap to spawn, so keeping this short frees OS threads.
IDLE_SHUTDOWN_SECONDS = 30 * 60


# Broadcast function signature — called with a dict payload. Wired by the
# host (meeting_agent.py) to the WebSocket fan-out.
BroadcastFn = Callable[[dict], None]


class _ThreadWorker:
    """Internal per-thread worker state."""

    def __init__(self, pool: "ExecutorPool", thread_id: str):
        self.pool = pool
        self.thread_id = thread_id
        self.inbox: queue.Queue = queue.Queue()
        self.thread: threading.Thread = threading.Thread(
            target=self._run, daemon=True, name=f"exec-{thread_id}",
        )
        self._stopping = False

    def start(self) -> None:
        self.thread.start()

    def submit(self, payload: dict) -> None:
        self.inbox.put(payload)

    def _run(self) -> None:
        # Bind the contextvar for all log records emitted on this OS thread.
        token = current_thread_id.set(self.thread_id)
        try:
            while not self._stopping:
                try:
                    payload = self.inbox.get(timeout=IDLE_SHUTDOWN_SECONDS)
                except queue.Empty:
                    logger.info("[exec %s] idle shutdown", self.thread_id)
                    return
                if payload is None:  # sentinel
                    return
                try:
                    self._execute(payload)
                except Exception as e:
                    logger.error("[exec %s] run failed: %s", self.thread_id, e,
                                 exc_info=True)
        finally:
            current_thread_id.reset(token)
            # Unregister so the next submit spawns a fresh worker.
            self.pool._forget(self.thread_id)

    def _execute(self, payload: dict) -> None:
        """Run one user input against the thread."""
        from hub_cowork.core.agent_core import run_agent_on_thread

        tm = get_manager()
        thread = tm.get(self.thread_id)
        if thread is None:
            logger.warning("[exec %s] thread missing — dropping input", self.thread_id)
            return

        user_input = payload["text"]
        request_id = payload.get("request_id") or uuid.uuid4().hex[:8]

        # Append the user message & switch to running. (For conversational
        # skills, `_run_skill` will also append — we de-dupe by skipping there.
        # Simpler: only append here for *non-conversational* skills. But at
        # this point we don't yet know the skill, so we route through
        # `run_agent_on_thread` which handles both via append_message in the
        # conversational path. To avoid double-append, we do NOT append here.)
        thread.last_request_id = request_id
        tm.set_status(self.thread_id, "running")

        broadcast = self.pool._broadcast
        if broadcast:
            broadcast({
                "type": "thread_started",
                "thread_id": self.thread_id,
                "request_id": request_id,
            })

        def on_progress(kind: str, message: str) -> None:
            tm.append_progress(self.thread_id, kind, message, request_id=request_id)
            if broadcast:
                broadcast({
                    "type": "thread_progress",
                    "thread_id": self.thread_id,
                    "request_id": request_id,
                    "kind": kind,
                    "message": message,
                })

        # Append the user message to the thread (single source of truth).
        # `_run_skill` will read it from thread.messages for conversational
        # skills, or treat it as a fresh single-turn input otherwise.
        tm.append_message(self.thread_id, "user", user_input, request_id=request_id)
        thread = tm.get(self.thread_id) or thread  # refresh

        try:
            result = run_agent_on_thread(thread, user_input, on_progress=on_progress)
        except Exception as e:
            logger.error("[exec %s] execution failed: %s", self.thread_id, e,
                         exc_info=True)
            tm.set_status(self.thread_id, "failed")
            if broadcast:
                broadcast({
                    "type": "thread_error",
                    "thread_id": self.thread_id,
                    "request_id": request_id,
                    "error": str(e)[:500],
                })
            return

        # If `_run_skill` set the thread to awaiting_user, keep that status.
        # Otherwise mark completed.
        current = tm.get(self.thread_id)
        if current and current.status != "awaiting_user":
            tm.set_status(self.thread_id, "completed")

        if broadcast:
            broadcast({
                "type": "thread_completed",
                "thread_id": self.thread_id,
                "request_id": request_id,
                "result": result,
            })

        # Trigger on_thread_reply (Redis bridge etc.)
        if self.pool._on_thread_reply:
            try:
                final = tm.get(self.thread_id)
                status = final.status if final else "completed"
                self.pool._on_thread_reply(
                    thread_id=self.thread_id,
                    request_id=request_id,
                    text=result,
                    status=status,
                )
            except Exception as cb_err:
                logger.warning("on_thread_reply callback failed: %s", cb_err)


class ExecutorPool:
    """Registry of per-thread workers."""

    def __init__(self):
        self._workers: dict[str, _ThreadWorker] = {}
        self._lock = threading.Lock()
        self._broadcast: BroadcastFn | None = None
        self._on_notify: Callable[[str, str], None] | None = None
        self._on_show_window: Callable[[], None] | None = None
        self._on_thread_reply: Callable[..., None] | None = None

    def configure(self, *, on_broadcast: BroadcastFn | None = None,
                  on_notify: Callable[[str, str], None] | None = None,
                  on_show_window: Callable[[], None] | None = None,
                  on_thread_reply: Callable[..., None] | None = None) -> None:
        """Wire output hooks. Called multiple times is fine; each call
        overrides the previously registered value for any non-None kwarg."""
        if on_broadcast is not None:
            self._broadcast = on_broadcast
        if on_notify is not None:
            self._on_notify = on_notify
        if on_show_window is not None:
            self._on_show_window = on_show_window
        if on_thread_reply is not None:
            self._on_thread_reply = on_thread_reply

    def submit(self, thread_id: str, text: str, *,
               request_id: str | None = None) -> str:
        """Enqueue a user input to a thread. Spawns an executor if needed.
        Returns the request_id used for this invocation."""
        request_id = request_id or uuid.uuid4().hex[:8]
        with self._lock:
            worker = self._workers.get(thread_id)
            if worker is None:
                worker = _ThreadWorker(self, thread_id)
                self._workers[thread_id] = worker
                worker.start()
        worker.submit({"text": text, "request_id": request_id})
        return request_id

    def notify(self, title: str, message: str) -> None:
        if self._on_notify:
            try:
                self._on_notify(title, message)
            except Exception:
                pass

    def show_window(self) -> None:
        if self._on_show_window:
            try:
                self._on_show_window()
            except Exception:
                pass

    def _forget(self, thread_id: str) -> None:
        with self._lock:
            self._workers.pop(thread_id, None)


# Module-level singleton
_pool: ExecutorPool | None = None


def get_pool() -> ExecutorPool:
    global _pool
    if _pool is None:
        _pool = ExecutorPool()
    return _pool
