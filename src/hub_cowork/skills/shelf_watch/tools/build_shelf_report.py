"""
Tool: build_shelf_report

Renders a markdown comparison of the rows captured by `compare_shelf_prices`
and (optionally) writes a Word .docx via the shared `create_word_doc` tool
so the user has a sharable artifact.

Pure formatting — no browser, no LLM round-trip, no network. Designed to
be called immediately after `compare_shelf_prices` in the same skill turn.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from hub_cowork.tools._tool_result import ok, error
from hub_cowork.tools.create_word_doc import handle as create_word_doc_handle
from hub_cowork.skills.shelf_watch.tools._memory import (
    load_previous_snapshot as _load_previous_snapshot,
    index_by_pair as _index_by_pair,
    get_memory_dir as _get_memory_dir,
)

logger = logging.getLogger("hub_se_agent")


SCHEMA = {
    "type": "function",
    "name": "build_shelf_report",
    "description": (
        "Render a markdown comparison report from the rows returned by "
        "compare_shelf_prices, and optionally save it as a Word document. "
        "Pass the rows array verbatim from the prior tool call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "description": (
                    "The `rows` array from compare_shelf_prices' payload. "
                    "Each row carries sku, retailer_label, price_inr, "
                    "mrp_inr, discount_pct, emi_from_inr, exchange_offer, "
                    "in_stock, product_title, url, blocked, reason."
                ),
                "items": {"type": "object"},
            },
            "title": {
                "type": "string",
                "description": (
                    "Report title. Default 'Shelf Watch — Competitive "
                    "Pricing Snapshot'."
                ),
            },
            "save_word_doc": {
                "type": "boolean",
                "description": (
                    "If true (default), also save the report as a .docx and "
                    "open it. Set false to return markdown only."
                ),
            },
        },
        "required": ["rows"],
    },
}


def _fmt_inr(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"₹{int(value):,}"
    return "—"


def _fmt_pct(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{int(value)}%"
    return "—"


def _fmt_text(value: Any) -> str:
    if value in (None, "", False):
        return "—"
    if value is True:
        return "Yes"
    return str(value)


def _fmt_delta(current: Any, previous: Any) -> str:
    """Format the price change vs the previous captured run."""
    if not isinstance(current, (int, float)) or not isinstance(previous, (int, float)):
        return "—"
    diff = int(current) - int(previous)
    if diff == 0:
        return f"flat (was ₹{int(previous):,})"
    arrow = "▲" if diff > 0 else "▼"
    pct = (diff / previous * 100) if previous else 0
    return f"{arrow} ₹{abs(diff):,} ({pct:+.1f}%) vs ₹{int(previous):,}"


def _build_markdown(
    rows: list[dict[str, Any]],
    title: str,
    previous_index: dict[str, dict[str, Any]],
    previous_timestamp: str | None,
) -> str:
    if not rows:
        return f"# {title}\n\n_No rows captured._\n"

    # Group by SKU so the report reads as one comparison per product.
    by_sku: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in rows:
        sku = r.get("sku") or "(unknown SKU)"
        if sku not in by_sku:
            by_sku[sku] = []
            order.append(sku)
        by_sku[sku].append(r)

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"_Captured {datetime.now().strftime('%Y-%m-%d %H:%M')} (local time)._")
    if previous_timestamp:
        lines.append(f"_Comparing against prior run from {previous_timestamp}._")
    lines.append("")

    for sku in order:
        lines.append(f"## {sku}")
        lines.append("")
        lines.append(
            "| Retailer | Price | MRP | Discount | EMI from | Exchange offer | In stock | vs Last Run |"
        )
        lines.append(
            "|---|---|---|---|---|---|---|---|"
        )
        cheapest_price: int | None = None
        cheapest_retailer: str | None = None
        for r in by_sku[sku]:
            label = r.get("retailer_label") or r.get("retailer") or "?"
            if r.get("blocked"):
                lines.append(
                    f"| {label} | ⚠ blocked ({r.get('reason') or 'unknown'}) | — | — | — | — | — | — |"
                )
                continue
            price = r.get("price_inr")
            if isinstance(price, (int, float)):
                if cheapest_price is None or price < cheapest_price:
                    cheapest_price = int(price)
                    cheapest_retailer = label
            sku_key = f"{(r.get('sku') or '').strip().lower()}||{(r.get('retailer') or '').strip().lower()}"
            prev = previous_index.get(sku_key) or {}
            delta_str = _fmt_delta(price, prev.get("price_inr")) if prev else "new"
            lines.append(
                "| {label} | {price} | {mrp} | {disc} | {emi} | {exch} | {stock} | {delta} |".format(
                    label=label,
                    price=_fmt_inr(price),
                    mrp=_fmt_inr(r.get("mrp_inr")),
                    disc=_fmt_pct(r.get("discount_pct")),
                    emi=_fmt_inr(r.get("emi_from_inr")),
                    exch=_fmt_text(r.get("exchange_offer")),
                    stock=_fmt_text(r.get("in_stock")),
                    delta=delta_str,
                )
            )

        # Per-SKU verdict line.
        successful = [r for r in by_sku[sku] if not r.get("blocked")]
        lines.append("")
        if cheapest_retailer and len(successful) >= 2:
            prices = [r.get("price_inr") for r in successful if isinstance(r.get("price_inr"), (int, float))]
            if len(prices) >= 2:
                gap = max(prices) - min(prices)
                lines.append(
                    f"**Verdict:** {cheapest_retailer} is cheapest at "
                    f"{_fmt_inr(cheapest_price)} (₹{int(gap):,} below the "
                    f"highest-priced competitor)."
                )
        elif cheapest_retailer:
            lines.append(f"**Verdict:** Only {cheapest_retailer} returned a price for this SKU.")
        else:
            lines.append("**Verdict:** No clean prices captured — review screenshots.")
        lines.append("")

        # Reference URLs for audit.
        url_lines = []
        for r in by_sku[sku]:
            if r.get("url"):
                url_lines.append(f"- {r.get('retailer_label') or r.get('retailer')}: {r['url']}")
        if url_lines:
            lines.append("**Sources:**")
            lines.extend(url_lines)
            lines.append("")

    lines.append("---")
    lines.append("")
    try:
        mem_dir = _get_memory_dir()
        lines.append(f"_Run snapshots are kept under `{mem_dir}` (synced to OneDrive when the configured agenda folder lives there)._")
    except Exception:
        pass
    lines.append(
        "_Generated by Hub Cowork's `shelf_watch` skill using Azure OpenAI "
        "computer-use + Playwright. Public PDPs only — no login, no cart, "
        "no captcha solving._"
    )
    lines.append("")
    return "\n".join(lines)


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    rows = arguments.get("rows")
    if not isinstance(rows, list):
        return error(
            "build_shelf_report",
            "config",
            "Missing or invalid 'rows' argument; expected an array of objects.",
        )
    title = arguments.get("title") or "Shelf Watch — Competitive Pricing Snapshot"
    save_doc = bool(arguments.get("save_word_doc", True))

    # Pull the previous snapshot from OneDrive memory for delta column.
    previous_index: dict[str, dict[str, Any]] = {}
    previous_timestamp: str | None = None
    try:
        prev = _load_previous_snapshot()
        if prev and isinstance(prev.get("rows"), list):
            # Only treat it as "previous" if it isn't this same run.
            current_keys = {
                f"{(r.get('sku') or '').strip().lower()}||{(r.get('retailer') or '').strip().lower()}"
                for r in rows
            }
            prev_keys = {
                f"{(r.get('sku') or '').strip().lower()}||{(r.get('retailer') or '').strip().lower()}"
                for r in prev["rows"]
            }
            # Heuristic: if every current pair is in prev AND timestamps differ,
            # it's a real prior. We don't get the current run's timestamp here
            # (rows came from the model verbatim), so just guard against the
            # degenerate identical case.
            if prev_keys and current_keys and prev_keys == current_keys and all(
                p.get("captured_at") == c.get("captured_at")
                for p, c in zip(prev["rows"], rows)
            ):
                pass  # same run, skip
            else:
                previous_index = _index_by_pair(prev["rows"])
                previous_timestamp = prev.get("timestamp")
    except Exception as ex:
        logger.warning("build_shelf_report: previous-snapshot load failed (%s)", ex)

    markdown = _build_markdown(rows, title, previous_index, previous_timestamp)

    word_doc_msg: str | None = None
    if save_doc:
        try:
            filename = "Shelf-Watch-" + datetime.now().strftime("%Y-%m-%d-%H%M") + ".docx"
            doc_result = create_word_doc_handle(
                {"filename": filename, "markdown_content": markdown},
                on_progress=on_progress,
            )
            word_doc_msg = doc_result
        except Exception as ex:
            logger.exception("build_shelf_report: create_word_doc failed")
            word_doc_msg = f"Word doc save failed: {ex}"

    payload = {
        "markdown": markdown,
        "word_doc": word_doc_msg,
        "previous_run_timestamp": previous_timestamp,
        "memory_folder": str(_get_memory_dir()) if True else None,
    }
    return ok("build_shelf_report", json.dumps(payload, ensure_ascii=False))
