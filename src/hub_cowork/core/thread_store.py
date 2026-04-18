"""
ThreadStore — pluggable persistence for ConversationThread objects.

The default `LocalJsonThreadStore` writes one JSON file per thread under
the app home (`app_paths.THREADS_DIR`, default
`~/.hub-cowork/threads/{active|archive}/<id>.json`). Writes are atomic
(`tempfile + os.replace`) and coalesced per-thread on a 250 ms debounce to
avoid thrash during chatty progress/log updates.

External archive stores (e.g. a future `CosmosThreadArchiveStore`) plug in
via the `ThreadArchiveStore` interface below. They are intentionally
**not** implemented here — the seam exists so Cosmos DB (or any other
remote archive) can be bolted on without touching `ThreadManager`.

Expected `ThreadArchiveStore` interface:

    class ThreadArchiveStore:
        def archive(self, thread_dict: dict) -> str:
            '''Persist the thread externally; return a location URI
            (e.g. "cosmosdb://db/container/<id>") stored on the thread.'''

        def restore(self, location: str) -> dict:
            '''Fetch the external record by location and return its
            full thread dict.'''

        def list(self, filter: dict | None = None) -> list[dict]:
            '''Return summary dicts (id, title, updated_at, ...) of
            archived threads matching the optional filter.'''
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol

from hub_cowork.core.conversation_thread import ConversationThread

logger = logging.getLogger("hub_se_agent")


# ---------------------------------------------------------------------------
# Abstract interfaces
# ---------------------------------------------------------------------------

class ThreadStore(Protocol):
    """Primary persistence interface for active + archived threads."""

    def load_all(self) -> list[ConversationThread]: ...
    def load(self, thread_id: str) -> ConversationThread | None: ...
    def save(self, thread: ConversationThread) -> None: ...
    def delete(self, thread_id: str) -> None: ...
    def archive(self, thread: ConversationThread) -> None: ...
    def unarchive(self, thread_id: str) -> ConversationThread | None: ...
    def list_archived_summaries(self) -> list[dict]: ...


class ThreadArchiveStore(Protocol):
    """Optional secondary store for archived threads (e.g. Cosmos DB).

    Not implemented here — documented so future stores plug in cleanly.
    """

    def archive(self, thread_dict: dict) -> str: ...
    def restore(self, location: str) -> dict: ...
    def list(self, filter: dict | None = None) -> list[dict]: ...


# ---------------------------------------------------------------------------
# Local JSON store (default)
# ---------------------------------------------------------------------------

class LocalJsonThreadStore:
    """Disk-backed thread store. One JSON file per thread; coalesced writes."""

    # How long to wait after the last mutation before flushing to disk.
    DEBOUNCE_SECONDS = 0.25

    def __init__(self, root: Path | None = None):
        from hub_cowork.core.app_paths import THREADS_DIR as _DEFAULT_THREADS_DIR
        self._root = root or _DEFAULT_THREADS_DIR
        self._active_dir = self._root / "active"
        self._archive_dir = self._root / "archive"
        self._active_dir.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(parents=True, exist_ok=True)

        # Debounce state
        self._pending: dict[str, ConversationThread] = {}
        self._pending_lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load_all(self) -> list[ConversationThread]:
        """Load every active thread from disk. Archived threads are NOT
        loaded eagerly — callers use `list_archived_summaries` + `load`."""
        threads = []
        for path in sorted(self._active_dir.glob("*.json")):
            t = self._read_file(path)
            if t is not None:
                threads.append(t)
        logger.info("Loaded %d active thread(s) from %s", len(threads), self._active_dir)
        return threads

    def load(self, thread_id: str) -> ConversationThread | None:
        """Load a thread by id (checks active then archive)."""
        for d in (self._active_dir, self._archive_dir):
            p = d / f"{thread_id}.json"
            if p.exists():
                return self._read_file(p)
        return None

    def list_archived_summaries(self) -> list[dict]:
        """Return summary dicts for all archived threads (lazy body load)."""
        summaries = []
        for path in sorted(self._archive_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                t = ConversationThread.from_dict(data)
                summaries.append(t.summary())
            except Exception as e:
                logger.warning("Skipped archived thread %s: %s", path.name, e)
        return summaries

    def _read_file(self, path: Path) -> ConversationThread | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ConversationThread.from_dict(data)
        except Exception as e:
            logger.warning("Could not read thread file %s: %s", path, e)
            return None

    # ------------------------------------------------------------------
    # Save (coalesced)
    # ------------------------------------------------------------------

    def save(self, thread: ConversationThread) -> None:
        """Queue a debounced write. Most recent state wins."""
        with self._pending_lock:
            self._pending[thread.id] = thread
            timer = self._timers.get(thread.id)
            if timer is not None:
                timer.cancel()
            t = threading.Timer(self.DEBOUNCE_SECONDS, self._flush_one, args=(thread.id,))
            t.daemon = True
            self._timers[thread.id] = t
            t.start()

    def flush_all(self) -> None:
        """Synchronously flush every pending write. Call on shutdown."""
        with self._pending_lock:
            ids = list(self._pending.keys())
            for tid in ids:
                timer = self._timers.pop(tid, None)
                if timer is not None:
                    timer.cancel()
        for tid in ids:
            self._flush_one(tid)

    def _flush_one(self, thread_id: str) -> None:
        with self._pending_lock:
            thread = self._pending.pop(thread_id, None)
            self._timers.pop(thread_id, None)
        if thread is None:
            return
        target_dir = self._archive_dir if thread.status == "archived" else self._active_dir
        target = target_dir / f"{thread.id}.json"
        self._atomic_write(target, thread.to_dict())

    def _atomic_write(self, target: Path, payload: dict) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # tempfile in the same dir so os.replace is atomic
            fd, tmp_path = tempfile.mkstemp(
                dir=str(target.parent), prefix=".tmp-", suffix=".json"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, target)
            finally:
                # If replace already moved the tempfile this unlink is a noop.
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        except Exception as e:
            logger.error("Failed to persist thread %s: %s", target.name, e)

    # ------------------------------------------------------------------
    # Delete / archive / unarchive
    # ------------------------------------------------------------------

    def delete(self, thread_id: str) -> None:
        # Cancel any pending write
        with self._pending_lock:
            self._pending.pop(thread_id, None)
            timer = self._timers.pop(thread_id, None)
            if timer is not None:
                timer.cancel()
        for d in (self._active_dir, self._archive_dir):
            p = d / f"{thread_id}.json"
            if p.exists():
                try:
                    p.unlink()
                    logger.info("Deleted thread file %s", p)
                except OSError as e:
                    logger.warning("Could not delete %s: %s", p, e)

    def archive(self, thread: ConversationThread) -> None:
        """Move a thread from active/ to archive/. Caller should have already
        set `thread.status = "archived"` (archive write lands in archive_dir
        based on that status). Also removes the active file if present."""
        # Force an immediate sync write to the correct directory.
        with self._pending_lock:
            self._pending[thread.id] = thread
            timer = self._timers.pop(thread.id, None)
            if timer is not None:
                timer.cancel()
        self._flush_one(thread.id)
        # Drop the stale active-dir copy (if the status transition moved it).
        active_copy = self._active_dir / f"{thread.id}.json"
        archive_copy = self._archive_dir / f"{thread.id}.json"
        if thread.status == "archived" and active_copy.exists() and archive_copy.exists():
            try:
                active_copy.unlink()
            except OSError as e:
                logger.warning("Could not remove old active copy %s: %s", active_copy, e)

    def unarchive(self, thread_id: str) -> ConversationThread | None:
        path = self._archive_dir / f"{thread_id}.json"
        if not path.exists():
            return None
        thread = self._read_file(path)
        if thread is None:
            return None
        thread.status = "active"
        thread.touch()
        # Write to active dir, then remove archive copy.
        self._atomic_write(self._active_dir / f"{thread_id}.json", thread.to_dict())
        try:
            path.unlink()
        except OSError as e:
            logger.warning("Could not remove archive copy %s: %s", path, e)
        return thread
