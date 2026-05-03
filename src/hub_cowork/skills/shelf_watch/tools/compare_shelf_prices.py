"""
Tool: compare_shelf_prices

Drives the generic `core.computer_use` harness once per (SKU, retailer)
pair to capture pricing intelligence (price, MRP, EMI, exchange offer,
in-stock) from public PDPs. No login, no cart, no scraping behind
authentication.

The model running INSIDE the harness is gpt-5.4 with the built-in
`computer` tool — it sees screenshots, decides clicks, types into the
search box, opens the first matching PDP, then returns a JSON line. This
tool just orchestrates the per-pair runs and persists results.

Outputs are also written to disk under
~/.hub-cowork/shelf_watch/runs/<timestamp>/ so a follow-up `build_shelf_report`
call (or any future trend analysis) can read them back.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from hub_cowork.core.app_paths import APP_HOME
from hub_cowork.core.computer_use import run_computer_use_task
from hub_cowork.tools._tool_result import ok, error
from hub_cowork.skills.shelf_watch.tools._memory import save_run as _save_run_to_memory

logger = logging.getLogger("hub_se_agent")


# Default demo SKU set — small on purpose. Hardcoded for v1; will move to
# hub_config.json once the skill is past the demo stage.
_DEFAULT_SKUS = [
    "Apple iPhone 16 128GB Black",
    "Samsung 55 inch QN90D Neo QLED 4K TV",
    "LG 7kg Front Load Washing Machine FHV1207Z2B",
]

# Region defaults for this skill. Croma and Reliance Digital are India-only
# retailers, so we default the browser context to en-IN / Asia/Kolkata so
# PDPs render INR pricing consistently and skip "choose your region" overlays.
# Power users can override via hub_config.json (`shelf_watch_locale` /
# `shelf_watch_timezone`) without changing code — useful when the SKU set
# expands beyond Indian retailers.
_DEFAULT_LOCALE = "en-IN"
_DEFAULT_TIMEZONE = "Asia/Kolkata"


def _resolve_region() -> tuple[str, str]:
    """Pull locale/timezone overrides from hub_config; fall back to defaults."""
    try:
        from hub_cowork.core import hub_config
        cfg = hub_config.load()
        locale = (cfg.get("shelf_watch_locale") or "").strip() or _DEFAULT_LOCALE
        tz = (cfg.get("shelf_watch_timezone") or "").strip() or _DEFAULT_TIMEZONE
        return locale, tz
    except Exception:
        return _DEFAULT_LOCALE, _DEFAULT_TIMEZONE

# Retailer registry. We deliberately land on the **homepage** and let the
# computer-use model use the on-page search box rather than constructing
# a search URL. Retailer search URLs change frequently (Reliance Digital's
# ?q= and ?searchQuery= paths both 404 as of May 2026 after their Jio
# storefront migration), so this is more resilient.
_RETAILERS: dict[str, dict[str, Any]] = {
    "croma": {
        "label": "Croma",
        "start_url": "https://www.croma.com/",
        "allow_domains": ["www.croma.com", "croma.com"],
        "search_hint": (
            "Click the search icon / search box at the top of the page, "
            "type the SKU, and press Enter to load the search results."
        ),
    },
    "reliance_digital": {
        "label": "Reliance Digital",
        "start_url": "https://www.reliancedigital.in/",
        "allow_domains": [
            "www.reliancedigital.in", "reliancedigital.in",
            # Reliance Digital migrated to the Jio storefront which serves
            # PDPs and CDN assets from these subdomains.
            "jiostore.online", "cdn.jiostore.online", "cdn.pixelbin.io",
        ],
        "search_hint": (
            "Click the search box at the top of the page (it usually says "
            "'What are you looking for?'), type the SKU, and press Enter "
            "to load the search results. If a 'choose your location' or "
            "pin-code popup appears, dismiss it (close button) before searching."
        ),
    },
}


_PER_RUN_INSTRUCTIONS = """\
You are a competitive-pricing scout running inside a real Chromium browser.
Your goal is to capture public-facing product information for ONE SKU on ONE
retailer's website and return it as a single JSON object.

RULES (non-negotiable):
1. Stay on the retailer's own domain (and its CDN / storefront subdomains).
   Do not visit search engines, ad redirects, payment partners, or third-party
   comparison sites.
2. Do NOT log in. Do NOT add anything to cart. Do NOT click "Buy Now".
3. Do NOT attempt to solve any CAPTCHA or "Are you human?" challenge. If
   you see one, stop immediately and return the blocked-result JSON below.
4. Only read what is publicly visible on the product detail page (PDP).
5. If a location / pin-code / cookie / app-install popup appears, dismiss
   it (close button or "Maybe later") so it doesn't block the search box.
6. If the search returns multiple matches, open the FIRST result whose
   title clearly matches the requested SKU (model number / capacity / size
   are the strongest signals). Skip refurbished, used, and accessory listings.

WORKFLOW:
- The current page is the retailer's HOMEPAGE.
- Locate the search box and type the SKU exactly as given, then press Enter.
- Wait for the search results page to load.
- Identify the best matching product card and click it to open its PDP.
- Wait for the PDP to load (look for the price block).
- If the price is not visible in the current viewport, **scroll down** (one
  or two scroll actions of ~600px) until the price block is on screen.
  Indian retail PDPs typically render the price within the first scroll.
- Read price, MRP / strike-through original price, % discount, EMI starting
  amount (e.g. "EMI from ₹2,499/month"), exchange offer (e.g. "Up to ₹15,000
  off on exchange"), and in-stock / out-of-stock / pin-code-required state.
- If a value isn't shown after scrolling, use null. Don't guess.

EXTRACTION CHECKLIST (perform BEFORE you emit JSON):
  a. The current PDP screenshot has been rendered at high resolution.
     Read every number with a ₹ symbol that is visible on screen.
  b. The LARGEST ₹ amount near the product title is almost always
     `price_inr`. The smaller ₹ amount with strike-through is `mrp_inr`.
  c. If you see "EMI from ₹X" or "EMI starting at ₹X" — that's `emi_from_inr`.
  d. If you see "Up to ₹X off on exchange" — that's `exchange_offer`.
  e. If you see "In stock", "Add to Cart" enabled, or a delivery date —
     `in_stock` is true. If you see "Out of stock" or "Notify me" — false.
  f. If after a thorough scroll you genuinely see NO ₹ price on the page,
     scroll up to the top and try once more before returning null.
  g. NEVER return all-null JSON without first scrolling AT LEAST twice
     and reading the screenshots carefully. All-null on a real PDP almost
     always means you gave up too early.

OUTPUT (final assistant message — JSON only, no prose, no markdown fences):
On success:
  {"price_inr": 79900, "mrp_inr": 84900, "discount_pct": 6,
   "emi_from_inr": 2499, "exchange_offer": "Up to ₹15,000 off on exchange",
   "in_stock": true, "product_title": "...", "url": "https://..."}

On block / failure:
  {"blocked": true, "reason": "captcha" | "403" | "no_match" | "<other>"}

Numbers must be integers in rupees with no commas or currency symbols.
Stop calling the computer tool as soon as you have the JSON ready.
"""


SCHEMA = {
    "type": "function",
    "name": "compare_shelf_prices",
    "description": (
        "Use Computer-Use (gpt-5.4 + Playwright Chromium) to capture price, "
        "MRP, EMI, exchange offer, and stock status for a list of SKUs from "
        "Croma and Reliance Digital. One headed Chromium session per "
        "(SKU, retailer) pair. Public PDPs only — no login, no cart, no "
        "captcha solving. Returns a structured comparison and persists raw "
        "JSON + screenshots under ~/.hub-cowork/shelf_watch/runs/<timestamp>/."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skus": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of SKU descriptions to compare. Each entry is fed "
                    "to the retailer's site search verbatim. Omit to use the "
                    "default demo trio (iPhone 16, Samsung 55\" QN90D, LG "
                    "7kg FHV1207Z2B washer)."
                ),
            },
            "retailers": {
                "type": "array",
                "items": {"type": "string", "enum": ["croma", "reliance_digital"]},
                "description": (
                    "Retailers to query. Defaults to both Croma and "
                    "Reliance Digital."
                ),
            },
            "headless": {
                "type": "boolean",
                "description": (
                    "Launch Chromium without a visible window. Default false. "
                    "True is more discreet but currently more likely to be "
                    "challenged by retailer bot defenses."
                ),
            },
            "max_iterations_per_run": {
                "type": "integer",
                "description": (
                    "Safety cap on computer_call turns per (SKU, retailer) "
                    "pair. Default 30."
                ),
            },
        },
        "required": [],
    },
}


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:60] or "sku"


def _extract_json_payload(text: str) -> dict[str, Any] | None:
    """Pull the first {...} blob out of the model's final message."""
    if not text:
        return None
    # Try strict JSON first.
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fall back to the first balanced-looking object substring.
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _to_int_rupees(value: Any) -> int | None:
    """Coerce a model-returned price-ish value to integer rupees.

    The CUA model returns prices in many shapes depending on what it sees
    on the PDP: "₹69,900.00", "Rs. 69900", "69,900/-", 69900, "EMI from
    ₹3,290/mo*", etc. This function is permissive: strip currency symbols
    and units, parse the first numeric run, and round.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    if not isinstance(value, str):
        return None
    s = value.replace(",", "").replace("\u20b9", "")  # strip ₹ and commas
    # Pick the first numeric run (handles "EMI from 3290/mo*", "from 69900").
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return int(round(float(m.group(0))))
    except Exception:
        return None


def _to_pct(value: Any) -> int | None:
    """Coerce '6% OFF' / '6%' / 6 / '6.5' to integer percent."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    if not isinstance(value, str):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", value)
    if not m:
        return None
    try:
        return int(round(float(m.group(0))))
    except Exception:
        return None


def _to_in_stock(payload: dict[str, Any]) -> bool | None:
    """Infer in_stock from the various shapes the model returns."""
    # Direct boolean wins.
    val = payload.get("in_stock")
    if isinstance(val, bool):
        return val
    # Fall back to free-text availability fields.
    for key in ("availability", "stock", "stock_status", "in_stock"):
        v = payload.get(key)
        if isinstance(v, str):
            low = v.lower()
            if any(t in low for t in ("out of stock", "unavailable", "sold out", "notify me")):
                return False
            if any(t in low for t in ("in stock", "available", "add to cart", "buy now")):
                return True
    return None


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map the model's free-form JSON onto our canonical row schema.

    The CUA model is creative with field names \u2014 we've seen `price`,
    `price_inr`, `selling_price`, `deal_price`, `offer_price`, `mrp`,
    `mrp_inr`, `original_price`, `emi_price`, `emi_from_inr`,
    `exchange_offer`, `availability`, `in_stock`, etc. Pick the best
    available signal for each canonical field rather than requiring the
    model to nail our exact keys.
    """
    # Selling price: prefer explicit current/deal/selling, fall back to plain `price`.
    price_candidates = [
        payload.get("price_inr"),
        payload.get("deal_price"),
        payload.get("selling_price"),
        payload.get("current_price"),
        payload.get("sale_price"),
        payload.get("offer_price"),
        payload.get("price"),
    ]
    price_inr = next((_to_int_rupees(v) for v in price_candidates if v not in (None, "")), None)

    # MRP: explicit MRP / list / original price.
    mrp_candidates = [
        payload.get("mrp_inr"),
        payload.get("mrp"),
        payload.get("list_price"),
        payload.get("original_price"),
        payload.get("strike_price"),
    ]
    mrp_inr = next((_to_int_rupees(v) for v in mrp_candidates if v not in (None, "")), None)

    # If model gave us offer_price + price as a pair (Reliance pattern),
    # the lower one is the selling price and the higher is the MRP.
    if price_inr is not None and mrp_inr is not None and mrp_inr < price_inr:
        price_inr, mrp_inr = mrp_inr, price_inr

    discount_pct = _to_pct(
        payload.get("discount_pct")
        or payload.get("discount")
        or payload.get("discount_percent")
    )
    # Derive discount when missing but we have both prices.
    if discount_pct is None and price_inr and mrp_inr and mrp_inr > price_inr:
        discount_pct = int(round((mrp_inr - price_inr) * 100 / mrp_inr))

    emi_from_inr = _to_int_rupees(
        payload.get("emi_from_inr")
        or payload.get("emi_price")
        or payload.get("emi_starting")
        or payload.get("emi_from")
        or payload.get("emi")
    )

    exchange_offer = (
        payload.get("exchange_offer")
        or payload.get("exchange")
        or payload.get("exchange_bonus")
    )
    if exchange_offer is not None and not isinstance(exchange_offer, str):
        exchange_offer = str(exchange_offer)

    return {
        "price_inr": price_inr,
        "mrp_inr": mrp_inr,
        "discount_pct": discount_pct,
        "emi_from_inr": emi_from_inr,
        "exchange_offer": exchange_offer,
        "in_stock": _to_in_stock(payload),
        "product_title": payload.get("product_title") or payload.get("product_name") or payload.get("title"),
        "url": payload.get("url") or payload.get("pdp_url"),
    }


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    skus: list[str] = arguments.get("skus") or list(_DEFAULT_SKUS)
    retailer_keys: list[str] = arguments.get("retailers") or list(_RETAILERS.keys())
    headless: bool = bool(arguments.get("headless", False))
    max_iter: int = int(arguments.get("max_iterations_per_run", 30))

    unknown = [r for r in retailer_keys if r not in _RETAILERS]
    if unknown:
        return error(
            "compare_shelf_prices",
            "config",
            f"Unknown retailer(s): {unknown}. Supported: {list(_RETAILERS.keys())}",
        )

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = APP_HOME / "shelf_watch" / "runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    locale, timezone_id = _resolve_region()

    rows: list[dict[str, Any]] = []
    total = len(skus) * len(retailer_keys)
    done = 0

    if on_progress:
        on_progress(
            "progress",
            f"**Shelf Watch starting** — {len(skus)} SKU(s) × "
            f"{len(retailer_keys)} retailer(s) = {total} run(s). "
            f"Browser mode: {'headless' if headless else 'headed'}, "
            f"locale: {locale}, timezone: {timezone_id}.",
        )

    for sku in skus:
        for rk in retailer_keys:
            done += 1
            cfg = _RETAILERS[rk]
            label = cfg["label"]
            start_url = cfg["start_url"]
            shot_dir = run_dir / f"{_slugify(sku)}__{rk}"

            if on_progress:
                on_progress(
                    "progress",
                    f"[{done}/{total}] {label}: looking up `{sku}`",
                )

            try:
                result = run_computer_use_task(
                    instructions=_PER_RUN_INSTRUCTIONS,
                    user_task=(
                        f"Find the SKU '{sku}' on {label}'s site. "
                        f"{cfg['search_hint']} Then open the best-matching "
                        "PDP, read the pricing fields, and return the JSON."
                    ),
                    start_url=start_url,
                    allow_domains=cfg["allow_domains"],
                    max_iterations=max_iter,
                    headless=headless,
                    locale=locale,
                    timezone_id=timezone_id,
                    screenshot_dir=shot_dir,
                    on_progress=on_progress,
                )
            except Exception as ex:
                logger.exception("compare_shelf_prices: harness raised")
                rows.append({
                    "sku": sku, "retailer": rk, "retailer_label": label,
                    "blocked": True, "reason": f"harness_error: {ex}",
                })
                continue

            payload = _extract_json_payload(result.final_text) or {}
            logger.info(
                "compare_shelf_prices: %s/%s harness returned final_text=%r parsed=%r blocked=%s reason=%s",
                rk, _slugify(sku), (result.final_text or "")[:400], payload,
                result.blocked, result.block_reason,
            )
            row: dict[str, Any] = {
                "sku": sku,
                "retailer": rk,
                "retailer_label": label,
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "iterations": result.iterations,
                "screenshots_dir": str(shot_dir),
                "visited_urls": result.visited_urls,
            }
            if result.blocked:
                row["blocked"] = True
                row["reason"] = result.block_reason or "harness_blocked"
            elif payload.get("blocked"):
                row["blocked"] = True
                row["reason"] = payload.get("reason") or "model_blocked"
            else:
                row["blocked"] = False
                row.update(_normalize_payload(payload))
            rows.append(row)

            if on_progress:
                if row.get("blocked"):
                    on_progress(
                        "progress",
                        f"[{done}/{total}] {label}: ⚠ blocked ({row.get('reason')})",
                    )
                else:
                    price_str = (
                        f"₹{row['price_inr']:,}" if isinstance(row.get("price_inr"), int)
                        else "(price not read)"
                    )
                    on_progress(
                        "progress",
                        f"[{done}/{total}] {label}: {price_str} — "
                        f"{row.get('product_title') or sku}",
                    )

    # Persist the raw run for downstream tools / audit.
    out_json = run_dir / "comparison.json"
    try:
        out_json.write_text(
            json.dumps({"timestamp": timestamp, "rows": rows}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as ex:
        logger.warning("compare_shelf_prices: failed to write %s (%s)", out_json, ex)

    # Mirror to OneDrive (or fallback) memory folder so trends survive
    # across runs and the user has the raw JSON synced to the cloud.
    memory_info: dict[str, Any] = {}
    try:
        memory_info = _save_run_to_memory(timestamp, rows)
        if on_progress and memory_info.get("memory_folder"):
            on_progress(
                "progress",
                f"Snapshot saved to memory: `{memory_info['memory_folder']}` "
                f"({memory_info.get('captured_count', 0)} clean row(s)).",
            )
    except Exception as ex:
        logger.warning("compare_shelf_prices: memory save failed (%s)", ex)

    payload = {
        "timestamp": timestamp,
        "run_dir": str(run_dir),
        "comparison_json_path": str(out_json),
        "memory": memory_info,
        "rows": rows,
    }
    return ok("compare_shelf_prices", json.dumps(payload, ensure_ascii=False))
