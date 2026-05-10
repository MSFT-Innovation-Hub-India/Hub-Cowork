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
import re
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
                    "bank_offers, delivery_eta, seller, warranty, rating, "
                    "rating_count, in_stock, product_title, url, "
                    "category_attrs, blocked, reason."
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


def _fmt_rating(row: dict[str, Any]) -> str:
    rating = row.get("rating")
    count = row.get("rating_count")
    if not isinstance(rating, (int, float)):
        return "—"
    base = f"{float(rating):.1f}★"
    if isinstance(count, (int, float)) and count:
        return f"{base} ({int(count):,})"
    return base


def _render_promotions(row: dict[str, Any]) -> str:
    """Consolidated promotions cell — bank offers, exchange, EMI, discount."""
    bullets: list[str] = []
    bank_offers = row.get("bank_offers")
    if isinstance(bank_offers, list):
        for b in bank_offers:
            s = str(b).strip()
            if s:
                bullets.append(s)
    exchange = row.get("exchange_offer")
    if exchange:
        bullets.append(f"Exchange: {exchange}")
    emi = row.get("emi_from_inr")
    if isinstance(emi, (int, float)):
        bullets.append(f"EMI from ₹{int(emi):,}/mo")
    disc = row.get("discount_pct")
    if isinstance(disc, (int, float)) and disc:
        bullets.append(f"{int(disc)}% off MRP")
    if not bullets:
        return "—"
    # Markdown table cells can't contain real newlines AND the chat-side
    # renderer escapes HTML, so `<br>` shows as literal text. Use a
    # bullet-separated inline list instead — still readable, no spillage.
    return " • ".join(bullets)


# Model-id extraction for verdict gating.
#
# The user's input "SKU" is a free-text description, not a real product
# identifier — so a single SKU bucket can hold rows for entirely different
# product variants once discovery returns multiple matches per retailer.
# To avoid the apples-to-different-LG verdict bug, we only compute a price
# verdict when ≥2 distinct retailers carry rows that share a comparable
# model identity (e.g. UT80, QN90D, 128GB, FHV1207Z2B).
#
# The regex captures uppercase alphanumeric tokens 4-15 chars long that
# contain BOTH at least one letter and at least one digit. That catches
# real SKU codes like "65UA83506LA", "QN90D", "UT80", "FHV1207Z2B", and
# also useful comparable specs like "128GB" / "256GB" for phone storage.
_MODEL_ID_RE = re.compile(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{4,15}\b")

# Tokens that match the regex but aren't actually identifying — exclude
# them so they don't accidentally bind two unrelated variants together.
_MODEL_ID_NOISE = {
    "INCH", "INCHES", "USB3", "USB2", "HDMI2", "HDMI4",
    "WIFI5", "WIFI6", "WIFI7", "5GHZ", "2GHZ", "4KHDR", "8KHDR",
    "CM65", "CM55", "CM50", "CM43",  # cm-prefixed size noise from Indian PDPs
}


def _extract_model_ids(*titles: str) -> set[str]:
    """Pull tokens from a product title that look like model identifiers.

    Uppercase, length 4-15, mix of letters + digits. Filters obvious noise.
    Returns the empty set when no titles are provided.
    """
    found: set[str] = set()
    for t in titles:
        if not t:
            continue
        for m in _MODEL_ID_RE.findall(t.upper()):
            if m in _MODEL_ID_NOISE:
                continue
            found.add(m)
    return found


def _shared_model_id(a: set[str], b: set[str]) -> str | None:
    """Return the longest model-id token shared between two sets.

    Treats a 4+ char token as 'shared' if it appears as a substring of any
    token in the other set (handles "QN90D" vs "55QN90DAVL" patterns).
    Returns None when no overlap is found.
    """
    if not a or not b:
        return None
    candidates: list[str] = []
    for x in a:
        for y in b:
            if x == y:
                candidates.append(x)
            elif len(x) >= 4 and x in y:
                candidates.append(x)
            elif len(y) >= 4 and y in x:
                candidates.append(y)
    return max(candidates, key=len) if candidates else None


def _compute_verdict(rows: list[dict[str, Any]]) -> str | None:
    """Best-price verdict line — gated on shared model identity.

    Only emits a price comparison when at least two DIFFERENT retailers
    carry rows whose titles share a model-id-like token. Otherwise emits a
    short note that no like-for-like comparison is possible, so the user
    isn't misled into thinking we compared two different LG models as if
    they were the same product.
    """
    priced = [
        r for r in rows
        if not r.get("blocked") and isinstance(r.get("price_inr"), (int, float))
    ]
    if len(priced) < 2:
        return None

    # Per-row model-id sets, drawn from variant_title and product_title.
    ids_by_row: list[set[str]] = [
        _extract_model_ids(r.get("variant_title") or "", r.get("product_title") or "")
        for r in priced
    ]

    # Union-find: link rows whose titles share a model-id token, so a chain
    # of "A↔B, B↔C" pulls A and C into the same comparable set.
    n = len(priced)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _shared_model_id(ids_by_row[i], ids_by_row[j]):
                union(i, j)

    components: dict[int, list[int]] = {}
    for i in range(n):
        components.setdefault(find(i), []).append(i)

    # Pick the component with the largest cross-retailer price spread.
    best_group: list[int] | None = None
    best_spread = -1
    for idxs in components.values():
        retailers = {priced[i].get("retailer") for i in idxs}
        if len(retailers) < 2:
            continue
        prices = [int(priced[i]["price_inr"]) for i in idxs]
        spread = max(prices) - min(prices)
        if spread > best_spread:
            best_spread = spread
            best_group = idxs

    if best_group is None:
        # No like-for-like comparison possible across retailers.
        return (
            "_No shared model number across retailers — closest variants "
            "shown for context, no price verdict._"
        )

    group_rows = [priced[i] for i in best_group]
    group_rows.sort(key=lambda r: r["price_inr"])
    cheap = group_rows[0]
    expensive = group_rows[-1]
    diff = int(expensive["price_inr"]) - int(cheap["price_inr"])
    cheap_label = cheap.get("retailer_label") or cheap.get("retailer") or "?"
    exp_label = expensive.get("retailer_label") or expensive.get("retailer") or "?"

    matched_id = _shared_model_id(
        _extract_model_ids(cheap.get("variant_title") or "", cheap.get("product_title") or ""),
        _extract_model_ids(expensive.get("variant_title") or "", expensive.get("product_title") or ""),
    )
    suffix = f" (matched on `{matched_id}`)" if matched_id else ""

    if diff <= 0:
        return (
            f"**Verdict:** Both retailers price this at "
            f"₹{int(cheap['price_inr']):,}{suffix}."
        )
    pct = diff / expensive["price_inr"] * 100
    return (
        f"**Verdict:** **{cheap_label}** is ₹{diff:,} cheaper "
        f"({pct:.1f}% off {exp_label}'s price) at "
        f"₹{int(cheap['price_inr']):,} vs ₹{int(expensive['price_inr']):,}"
        f"{suffix}."
    )


def _render_category_attrs_table(
    rows: list[dict[str, Any]],
) -> list[str]:
    """Per-SKU sub-table of category-specific attributes, retailer-by-retailer.

    Returns markdown lines (empty list if no attrs were captured).
    """
    # Union of all attribute keys across the SKU's retailers.
    all_keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        attrs = r.get("category_attrs")
        if not isinstance(attrs, dict):
            continue
        for k in attrs.keys():
            if k not in seen:
                seen.add(k)
                all_keys.append(k)
    if not all_keys:
        return []

    lines: list[str] = []
    lines.append("**Category attributes**")
    lines.append("")
    header = "| Retailer | " + " | ".join(all_keys) + " |"
    sep = "|" + "---|" * (len(all_keys) + 1)
    lines.append(header)
    lines.append(sep)
    for r in rows:
        if r.get("blocked"):
            continue
        label = r.get("retailer_label") or r.get("retailer") or "?"
        # Disambiguate when multiple variants share a retailer.
        vt = (r.get("variant_title") or "").strip()
        if vt:
            short = vt if len(vt) <= 50 else vt[:47] + "…"
            row_label = f"{label} — {short}"
        else:
            row_label = label
        attrs = r.get("category_attrs") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        cells = []
        for k in all_keys:
            v = attrs.get(k)
            if v in (None, "", [], {}):
                cells.append("—")
            elif isinstance(v, (list, tuple)):
                cells.append(", ".join(str(x) for x in v))
            elif isinstance(v, dict):
                cells.append("; ".join(f"{kk}: {vv}" for kk, vv in v.items()))
            else:
                cells.append(str(v))
        lines.append(f"| {row_label} | " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _summarize_changes(
    rows: list[dict[str, Any]],
    previous_index: dict[str, dict[str, Any]],
) -> list[str]:
    """One-line change summaries vs the previous snapshot.

    Counts a change as material when:
      - price moved ≥1% OR ≥₹100 vs the previous run, OR
      - in_stock flipped between True/False.

    Returns a list of pre-formatted markdown bullet strings. Empty list
    when nothing material moved (lets the caller render a "no changes"
    line in the lede).
    """
    changes: list[str] = []
    for r in rows:
        if r.get("blocked"):
            continue
        sku_key = (
            f"{(r.get('sku') or '').strip().lower()}||"
            f"{(r.get('retailer') or '').strip().lower()}||"
            f"{(r.get('variant_title') or '').strip().lower()}"
        )
        prev = previous_index.get(sku_key)
        if not prev:
            continue
        label = r.get("retailer_label") or r.get("retailer") or "?"
        # Use variant_title in the bullet when available — clearer than
        # the user's free-text SKU when multiple variants share a bucket.
        product_label = (r.get("variant_title") or r.get("product_title") or r.get("sku") or "(SKU)")
        if isinstance(product_label, str) and len(product_label) > 70:
            product_label = product_label[:67] + "…"

        cur_price = r.get("price_inr")
        prev_price = prev.get("price_inr")
        if (
            isinstance(cur_price, (int, float))
            and isinstance(prev_price, (int, float))
            and cur_price != prev_price
        ):
            diff = int(cur_price) - int(prev_price)
            material = abs(diff) >= 100 or (
                prev_price and abs(diff) / prev_price >= 0.01
            )
            if material:
                arrow = "▲" if diff > 0 else "▼"
                pct = (diff / prev_price * 100) if prev_price else 0
                direction = "up" if diff > 0 else "down"
                changes.append(
                    f"- {arrow} **{label}** — {product_label}: price {direction} "
                    f"₹{abs(int(diff)):,} ({pct:+.1f}%), now ₹{int(cur_price):,} "
                    f"(was ₹{int(prev_price):,})"
                )

        cur_stock = r.get("in_stock")
        prev_stock = prev.get("in_stock")
        if (
            isinstance(cur_stock, bool)
            and isinstance(prev_stock, bool)
            and cur_stock != prev_stock
        ):
            if cur_stock:
                changes.append(
                    f"- ✅ **{label}** — {product_label}: back in stock "
                    "(was out of stock)"
                )
            else:
                changes.append(
                    f"- ⚠ **{label}** — {product_label}: now out of stock "
                    "(was in stock)"
                )
    return changes


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

    # Diff-first lede: when we have a previous snapshot, lead with
    # what changed. Watch agents earn their keep on deltas, not
    # snapshots — so the reader sees the news before the table.
    if previous_timestamp and previous_index:
        change_lines = _summarize_changes(rows, previous_index)
        lines.append("## What changed since the last run")
        lines.append("")
        if change_lines:
            lines.extend(change_lines)
        else:
            lines.append(
                "_No material changes detected — prices and stock held "
                "steady across all captured rows._"
            )
        lines.append("")

    for sku in order:
        sku_rows = by_sku[sku]
        lines.append(f"## {sku}")
        lines.append("")

        # Verdict line first so the user sees the takeaway at a glance.
        verdict = _compute_verdict(sku_rows)
        if verdict:
            lines.append(verdict)
            lines.append("")

        lines.append(
            "| Retailer | Price | MRP | In stock | Promotions | Delivery | Rating | Seller | Warranty | vs Last Run |"
        )
        lines.append(
            "|---|---|---|---|---|---|---|---|---|---|"
        )
        for r in sku_rows:
            label = r.get("retailer_label") or r.get("retailer") or "?"
            # When a specific variant was scraped (multi-match path), show
            # the variant title alongside the retailer name. The chat-side
            # markdown renderer escapes HTML, so we can't use <br>/<sub> —
            # use an em-dash separator with a smaller-feeling cue.
            variant_title = (r.get("variant_title") or "").strip()
            if variant_title:
                vt_display = variant_title if len(variant_title) <= 70 else variant_title[:67] + "…"
                retailer_cell = f"**{label}** — {vt_display}"
            else:
                retailer_cell = f"**{label}**"
            if r.get("blocked"):
                lines.append(
                    f"| {retailer_cell} | ⚠ blocked ({r.get('reason') or 'unknown'}) | — | — | — | — | — | — | — | — |"
                )
                continue
            price = r.get("price_inr")
            sku_key = (
                f"{(r.get('sku') or '').strip().lower()}||"
                f"{(r.get('retailer') or '').strip().lower()}||"
                f"{(r.get('variant_title') or '').strip().lower()}"
            )
            prev = previous_index.get(sku_key) or {}
            delta_str = _fmt_delta(price, prev.get("price_inr")) if prev else "new"
            lines.append(
                "| {label} | {price} | {mrp} | {stock} | {promo} | {delivery} | {rating} | {seller} | {warranty} | {delta} |".format(
                    label=retailer_cell,
                    price=_fmt_inr(price),
                    mrp=_fmt_inr(r.get("mrp_inr")),
                    stock=_fmt_text(r.get("in_stock")),
                    promo=_render_promotions(r),
                    delivery=_fmt_text(r.get("delivery_eta")),
                    rating=_fmt_rating(r),
                    seller=_fmt_text(r.get("seller")),
                    warranty=_fmt_text(r.get("warranty")),
                    delta=delta_str,
                )
            )

        lines.append("")

        # Category-specific attributes sub-table (Phase 3).
        attr_lines = _render_category_attrs_table(sku_rows)
        if attr_lines:
            lines.extend(attr_lines)

        # Reference URLs for audit.
        url_lines = []
        for r in sku_rows:
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
