"""
Shelf Watch run memory.

Persists every comparison run as a JSON snapshot under
    <agenda_output_folder>/shelf-watch/runs/run-<timestamp>.json
and maintains a rolling
    <agenda_output_folder>/shelf-watch/history.json
that records the latest captured price per (SKU, retailer) pair so
follow-up runs can compute deltas and the user can see trends across
weeks without re-scraping anything.

`agenda_output_folder` is the same OneDrive path used by the agenda
generator (set in Settings → Hub Config). When it is empty we fall back
to `~/Documents/hub-cowork-agenda-docs/shelf-watch/` so the tool keeps
working out of the box.

We deliberately keep history small (HISTORY_RUNS_TO_KEEP) so the JSON
stays diff-friendly when synced via OneDrive — large blobs cause noisy
sync churn and slow opens.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("hub_se_agent")


HISTORY_RUNS_TO_KEEP = 20  # rolling window in history.json


def _resolve_base_folder() -> Path:
    """Return the OneDrive (or fallback) base folder, creating it if needed."""
    base: str = ""
    try:
        from hub_cowork.core import hub_config
        cfg = hub_config.load()
        base = (cfg.get("agenda_output_folder") or "").strip()
    except Exception:
        logger.debug("shelf_watch memory: hub_config.load failed", exc_info=True)
    if not base:
        base = str(Path.home() / "Documents" / "hub-cowork-agenda-docs")
    folder = Path(base) / "shelf-watch"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "runs").mkdir(parents=True, exist_ok=True)
    return folder


def get_memory_dir() -> Path:
    """Public accessor — used by tools that want to surface the path."""
    return _resolve_base_folder()


def _key(row: dict[str, Any]) -> str:
    """Stable identity for a (SKU, retailer) pair across runs."""
    sku = (row.get("sku") or "").strip().lower()
    rk = (row.get("retailer") or "").strip().lower()
    return f"{sku}||{rk}"


def save_run(timestamp: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Persist this run as a snapshot and update the rolling history.

    Returns a small descriptor with paths and a count of rows that were
    captured cleanly (i.e. not blocked) — handy for the calling tool to
    surface in its progress message.
    """
    folder = _resolve_base_folder()

    # 1. Per-run snapshot.
    snap_name = f"run-{timestamp}.json"
    snap_path = folder / "runs" / snap_name
    try:
        snap_path.write_text(
            json.dumps({"timestamp": timestamp, "rows": rows}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as ex:
        logger.warning("shelf_watch memory: failed to write %s (%s)", snap_path, ex)

    # 2. Rolling history.
    history_path = folder / "history.json"
    history: dict[str, Any] = {"runs": [], "latest_per_pair": {}}
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
            if not isinstance(history, dict):
                history = {"runs": [], "latest_per_pair": {}}
            history.setdefault("runs", [])
            history.setdefault("latest_per_pair", {})
        except Exception as ex:
            logger.warning("shelf_watch memory: history.json unreadable (%s) — resetting", ex)
            history = {"runs": [], "latest_per_pair": {}}

    captured = sum(1 for r in rows if not r.get("blocked"))

    history["runs"].append({
        "timestamp": timestamp,
        "snapshot_path": str(snap_path),
        "row_count": len(rows),
        "captured_count": captured,
    })
    # Trim to the rolling window.
    history["runs"] = history["runs"][-HISTORY_RUNS_TO_KEEP:]

    # Update latest-per-pair index with this run's clean rows.
    for r in rows:
        if r.get("blocked"):
            continue
        history["latest_per_pair"][_key(r)] = {
            "timestamp": timestamp,
            "sku": r.get("sku"),
            "retailer": r.get("retailer"),
            "retailer_label": r.get("retailer_label"),
            "price_inr": r.get("price_inr"),
            "mrp_inr": r.get("mrp_inr"),
            "discount_pct": r.get("discount_pct"),
            "in_stock": r.get("in_stock"),
            "url": r.get("url"),
        }

    try:
        history_path.write_text(
            json.dumps(history, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as ex:
        logger.warning("shelf_watch memory: failed to write %s (%s)", history_path, ex)

    return {
        "memory_folder": str(folder),
        "snapshot_path": str(snap_path),
        "history_path": str(history_path),
        "captured_count": captured,
    }


def load_previous_snapshot(before_timestamp: str | None = None) -> dict[str, Any] | None:
    """
    Return the most recent snapshot strictly older than `before_timestamp`,
    or the most recent overall when None. Used by build_shelf_report for
    delta computation. Returns None if no prior run exists.
    """
    folder = _resolve_base_folder()
    runs_dir = folder / "runs"
    if not runs_dir.exists():
        return None
    candidates = sorted(runs_dir.glob("run-*.json"))
    if not candidates:
        return None
    if before_timestamp is not None:
        candidates = [p for p in candidates if p.stem.replace("run-", "") < before_timestamp]
        if not candidates:
            return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except Exception as ex:
        logger.warning("shelf_watch memory: failed to read %s (%s)", candidates[-1], ex)
        return None


def index_by_pair(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Helper for callers that need pair-keyed lookup of a row list."""
    return {_key(r): r for r in rows if not r.get("blocked")}
