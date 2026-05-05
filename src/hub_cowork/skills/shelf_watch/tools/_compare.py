"""
Tool: compare_shelf_prices

Two-pass pipeline per (SKU × retailer):

  Pass 1 — Navigation (Computer-Use, gpt-5.4):
    Drives a real Chromium browser through the retailer's homepage →
    search → PDP → scroll the entire page top-to-bottom. The CUA model
    is told NOT to extract anything; its only job is to land on the PDP
    and produce a complete set of viewport screenshots.

  Pass 2 — Extraction (vision LLM, gpt-5):
    A single vision-LLM call reads ALL the screenshots Pass 1 captured
    and emits the strict JSON schema (price, MRP, EMI, exchange, bank
    offers, delivery, seller, warranty, rating, plus per-SKU
    category-specific specs planned by a small model).

Why split? CUA models are post-trained for action efficiency and
terminate the loop the moment they think they're "done", which causes
them to skip extraction even when the data is on screen. A dedicated
vision pass with the screenshots already in hand has no such bias and
produces dramatically more complete results.

No login, no cart, no scraping behind authentication. Outputs are also
written to disk under ~/.hub-cowork/shelf_watch/runs/<timestamp>/ so
follow-up tools and trend analysis can read them back.
"""

from __future__ import annotations

import base64
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


def _resolve_headless_default() -> bool:
    """Default browser visibility from hub_config (`shelf_watch_headless`).

    Defaults to False (visible/headed) so users can watch the run and so
    retailer bot defenses are less likely to challenge us. Set to true in
    hub_config.json to run quietly in the background once the workflow is
    proven on a given machine.
    """
    try:
        from hub_cowork.core import hub_config
        cfg = hub_config.load()
        return bool(cfg.get("shelf_watch_headless", False))
    except Exception:
        return False


def _resolve_skip_vision_when_jsonld_complete() -> bool:
    """Opt-in fast path: skip the vision extractor entirely when the PDP's
    JSON-LD covered the canonical fields (price + product_title at minimum).

    Defaults to False so v1 stays conservative — vision keeps running and
    JSON-LD is overlaid on top of its output for the canonical fields. Set
    `shelf_watch_skip_vision_when_jsonld_complete: true` in hub_config.json
    once the workflow is proven on a given retailer set; that turns watch
    runs into sub-second JSON-LD parses with no LLM calls per PDP.
    """
    try:
        from hub_cowork.core import hub_config
        cfg = hub_config.load()
        return bool(cfg.get("shelf_watch_skip_vision_when_jsonld_complete", False))
    except Exception:
        return False


# JS executed against the rendered PDP after the navigator finishes
# scrolling. Pulls every JSON-LD Product block on the page (handles raw,
# array, and @graph-wrapped shapes) plus a couple of sanity-check
# signals. Croma and Reliance Digital both ship structured Product
# schema reliably, so this is usually all we need to capture the
# canonical fields without paying for vision extraction.
_PDP_STRUCTURED_JS = """
() => {
  const products = [];
  const blocks = document.querySelectorAll('script[type="application/ld+json"]');
  for (const b of blocks) {
    try {
      const raw = b.textContent || b.innerText || '';
      if (!raw.trim()) continue;
      const data = JSON.parse(raw);
      const items = Array.isArray(data) ? data : [data];
      for (const item of items) {
        if (!item || typeof item !== 'object') continue;
        const t = item['@type'];
        const types = Array.isArray(t) ? t : [t];
        if (types.indexOf('Product') !== -1) products.push(item);
        if (Array.isArray(item['@graph'])) {
          for (const g of item['@graph']) {
            if (!g || typeof g !== 'object') continue;
            const gt = g['@type'];
            const gts = Array.isArray(gt) ? gt : [gt];
            if (gts.indexOf('Product') !== -1) products.push(g);
          }
        }
      }
    } catch (e) {}
  }
  const text = (sel) => {
    const el = document.querySelector(sel);
    return el ? (el.innerText || el.textContent || '').trim() : null;
  };
  return {
    jsonld_products: products,
    page_title: document.title || null,
    h1: text('h1'),
    url: location.href
  };
}
"""


async def _extract_pdp_structured(page):
    """DOM extractor passed to the CUA harness. Returns JSON-LD Product
    blocks plus page sanity-check signals scraped from the rendered PDP.

    Vision still runs against the same screenshots in v1; this just gives
    the caller a structured ground-truth source to override the vision
    output for canonical fields (price, MRP, title, in_stock, rating).
    Set `shelf_watch_skip_vision_when_jsonld_complete: true` in
    hub_config.json to skip the vision pass entirely when JSON-LD is
    complete.
    """
    try:
        return await page.evaluate(_PDP_STRUCTURED_JS)
    except Exception as ex:
        logger.warning("compare_shelf_prices: JSON-LD scrape failed (%s)", ex)
        return {}

# Retailer registry. We deliberately land on the **homepage** and let the
# computer-use model use the on-page search box rather than constructing
# a search URL. Retailer search URLs change frequently (Reliance Digital's
# ?q= and ?searchQuery= paths both 404 as of May 2026 after their Jio
# storefront migration), so this is more resilient.
#
# These ship as defaults. Users can override / extend the registry by
# adding a `shelf_watch_retailers` object to ~/.hub-cowork/hub_config.json,
# e.g. to add Best Buy / Amazon / Vijay Sales / a regional retailer:
#
#   "shelf_watch_retailers": {
#     "amazon_in": {
#       "label": "Amazon India",
#       "start_url": "https://www.amazon.in/",
#       "allow_domains": ["amazon.in", "www.amazon.in", "m.media-amazon.com"],
#       "search_hint": "Use the search bar at the top of the page..."
#     }
#   }
#
# Override behavior: the user-supplied dict is **merged** on top of the
# defaults below (per-key replace), so you can tweak just the search_hint
# for an existing retailer without re-specifying allow_domains.
_DEFAULT_RETAILERS: dict[str, dict[str, Any]] = {
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


def _resolve_retailers() -> dict[str, dict[str, Any]]:
    """Merge user-configured retailers on top of the shipped defaults.

    User overrides come from `shelf_watch_retailers` in hub_config.json.
    Each value must have at minimum `label`, `start_url`, and
    `allow_domains` (list of strings); `search_hint` is optional and
    falls back to a generic instruction.
    """
    retailers = {k: dict(v) for k, v in _DEFAULT_RETAILERS.items()}
    try:
        from hub_cowork.core import hub_config
        cfg = hub_config.load()
        user = cfg.get("shelf_watch_retailers") or {}
        if not isinstance(user, dict):
            logger.warning(
                "shelf_watch_retailers must be a dict, got %s — ignoring", type(user).__name__
            )
            return retailers
        for key, entry in user.items():
            if not isinstance(entry, dict):
                logger.warning("shelf_watch_retailers[%s] is not a dict — skipping", key)
                continue
            if key in retailers:
                # Per-key merge so users can tweak one field without
                # repeating the rest of the entry.
                retailers[key].update(entry)
            else:
                # New retailer — must have the required fields.
                missing = [f for f in ("label", "start_url", "allow_domains") if not entry.get(f)]
                if missing:
                    logger.warning(
                        "shelf_watch_retailers[%s] missing required fields %s — skipping",
                        key, missing,
                    )
                    continue
                if not entry.get("search_hint"):
                    entry = dict(entry)
                    entry["search_hint"] = (
                        "Use the on-page search to find the SKU, then open "
                        "the best-matching product detail page."
                    )
                retailers[key] = entry
    except Exception as ex:
        logger.warning("shelf_watch: failed to load retailer overrides (%s)", ex)
    return retailers


# Snapshot at import time. Restart-the-agent reloads, same as skill YAML changes.
_RETAILERS: dict[str, dict[str, Any]] = _resolve_retailers()


_PER_RUN_INSTRUCTIONS = """\
You are a browser navigator running inside a real Chromium browser.
Your ONLY job is to land on the correct product detail page (PDP) for the
requested SKU and scroll through the entire page so a separate vision
extractor can read every section. You do NOT need to extract any data
yourself.

RULES (non-negotiable):
1. Stay on the retailer's own domain (and its CDN / storefront subdomains).
   Do not visit search engines, ad redirects, payment partners, or third-party
   comparison sites.
2. Do NOT log in. Do NOT add anything to cart. Do NOT click "Buy Now".
3. Do NOT attempt to solve any CAPTCHA. If you see one, stop and reply
   exactly: BLOCKED captcha
4. Do NOT type a pin-code if a delivery widget asks for one. Just leave it.
5. If a location / cookie / app-install / newsletter popup blocks the page,
   dismiss it (close button or "Maybe later") and continue.
6. If the search returns multiple matches, open the FIRST result that
   plausibly matches the requested SKU as a human shopper would judge —
   same product CLASS (TV / phone / washing machine), same brand when
   given, and the dominant spec the user named (size, capacity, model
   line) is in the right ballpark. Treat softer attributes as
   preferences, not gates: if the user said "LED" and the closest
   match is a QLED or NanoCell TV (also LED-backlit), that's fine.
   When a specific `variant_title` is supplied in the task below, open
   that one — match by the most distinctive words; the title may be
   slightly truncated or rewrapped on the card.
   Skip refurbished, used, accessory, and bundle listings. Reply
   exactly `BLOCKED no_match` only when nothing on the results page is
   even the same product class.

WORKFLOW:
1. The current page is the retailer's HOMEPAGE.
2. Find the search box, type the SKU exactly as given, press Enter.
3. Click the best-matching product card to open its PDP.
4. Once on the PDP, perform AT LEAST 5 scroll actions of ~600px each,
   pausing briefly between scrolls so each section renders. Cover:
     - the price / stock area near the top
     - the offers / EMI / bank-offers area
     - the delivery / seller / warranty area
     - the specifications / "About this item" area
     - the ratings & reviews area near the bottom
5. If a "View all offers", "See more", "Specifications" tab, or similar
   accordion is visible, click it once to expand. Do not click anything
   that navigates away from the PDP.
6. After scrolling through the full PDP, reply with exactly one word: DONE

Do NOT return any JSON, prices, or extracted data. The downstream pipeline
reads the screenshots you captured. Your reply must be either DONE or
BLOCKED <reason>. Aim for 8–14 computer-tool turns total per PDP.
"""


# ----- Phase 2: per-SKU attribute planning via the small chat model -----

_ATTRIBUTE_PLANNER_PROMPT = """\
You are a retail merchandising analyst. Given a single SKU description,
identify the product category and the spec attributes a buyer would compare
across retailers for THAT category.

Return JSON only, with this exact shape:
{
  "category": "<short noun phrase, e.g. 'smartphone', 'OLED TV', 'front-load washing machine'>",
  "attributes": [
    {"key": "<snake_case key>", "label": "<short human label>", "hint": "<one-line hint on where/how it appears on a PDP>"},
    ...
  ]
}

Rules:
- 4 to 7 attributes. No fewer, no more.
- Pick attributes that materially affect a purchase decision and that are
  typically printed on a public product detail page (specs table or hero
  bullets). Skip subjective things like "design" or "build quality".
- Skip price, MRP, EMI, exchange, stock, ratings, warranty, seller,
  delivery, bank_offers — those are captured separately.
- Use snake_case keys, short labels, and concise hints.
- For phones: storage_gb, ram_gb, display_size_in, chipset, camera_mp, battery_mah.
- For TVs: panel_size_in, resolution, panel_type, refresh_rate_hz, hdr, smart_os.
- For washing machines: capacity_kg, load_type, energy_rating, wash_programs, max_spin_rpm.
- For laptops: cpu, ram_gb, ssd_gb, gpu, display_size_in, weight_kg.
- For headphones: driver_type, anc, battery_life_hr, codec_support, wireless.
- For cameras: sensor_size, megapixels, lens_mount, video_max, ibis.
"""


def _plan_attributes(sku: str, *, on_progress=None) -> dict[str, Any]:
    """Ask the small model what category-specific attributes to capture.

    Best-effort: any failure returns an empty plan and the run continues
    with just the canonical + generic-enrichment fields. This is purely
    additive intelligence — never blocks the workflow.
    """
    fallback = {"category": "", "attributes": []}
    try:
        from hub_cowork.core.agent_core import get_responses_client, CHAT_MODEL_SMALL
        client = get_responses_client()
        resp = client.responses.create(
            model=CHAT_MODEL_SMALL,
            instructions=_ATTRIBUTE_PLANNER_PROMPT,
            input=[{"role": "user", "content": f"SKU: {sku}"}],
            tools=[],
        )
        text = ""
        for item in resp.output:
            if getattr(item, "type", None) == "message":
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", None) == "output_text":
                        text += getattr(part, "text", "") or ""
        plan = _extract_json_payload(text) or {}
        if not isinstance(plan, dict):
            return fallback
        attrs = plan.get("attributes")
        if not isinstance(attrs, list):
            return fallback
        # Sanitize: drop entries missing key/label.
        clean = []
        for a in attrs:
            if not isinstance(a, dict):
                continue
            k = (a.get("key") or "").strip()
            lbl = (a.get("label") or "").strip()
            if not k or not lbl:
                continue
            clean.append({
                "key": re.sub(r"[^a-z0-9_]", "_", k.lower()),
                "label": lbl,
                "hint": (a.get("hint") or "").strip(),
            })
        return {
            "category": (plan.get("category") or "").strip(),
            "attributes": clean,
        }
    except Exception as ex:
        logger.warning("shelf_watch: attribute planning failed for %r (%s)", sku, ex)
        return fallback


def _format_category_block(plan: dict[str, Any]) -> str:
    """Render the per-SKU plan as a bullet list for the extractor prompt."""
    attrs = plan.get("attributes") or []
    if not attrs:
        return "(No category-specific attributes requested for this SKU.)"
    cat = plan.get("category") or "this product"
    lines = [f"Category: {cat}. Capture these attribute keys when visible:"]
    for a in attrs:
        hint = f" — {a['hint']}" if a.get("hint") else ""
        lines.append(f"  - {a['key']}  ({a['label']}){hint}")
    return "\n".join(lines)


# ----- Vision extractor: read the canonical schema from CUA's screenshots -----

_EXTRACTOR_PROMPT = """\
You are a meticulous retail data extractor. You will be shown a sequence
of screenshots captured by a navigator agent as it scrolled through ONE
product detail page (PDP) on an Indian retail website. Your job is to
read every screenshot carefully and emit a single JSON object with the
fields listed below.

OUTPUT (final assistant message — JSON only, no prose, no markdown fences):

CANONICAL FIELDS (always include; null if truly absent across all screenshots):
  - price_inr        : integer rupees, current selling price
  - mrp_inr          : integer rupees, strike-through / list price
  - discount_pct     : integer percent off
  - emi_from_inr     : integer rupees, "EMI from ₹X" / "EMI starting at ₹X"
  - exchange_offer   : free text, e.g. "Up to ₹15,000 off on exchange"
  - in_stock         : true | false (true if "Add to Cart" enabled or a
                       delivery date is shown; false if "Out of stock" /
                       "Notify me" / "Currently Unavailable")
  - product_title    : the title shown on the PDP, verbatim

GENERIC ENRICHMENTS (always include; null/[] if not visible):
  - bank_offers      : array of short strings, one per bank/card discount
                       visible (e.g. "₹3,000 off with HDFC Credit Card EMI")
  - delivery_eta     : free text, e.g. "Delivery by Tue, 7 May" or
                       "Not available for your pincode"
  - seller           : "Sold by Croma" / "Sold by Reliance Retail" / 3P seller
  - warranty         : free text, e.g. "1 year manufacturer warranty"
  - rating           : numeric (e.g. 4.5)
  - rating_count     : integer review count

CATEGORY-SPECIFIC ATTRIBUTES:
{category_block}

Add each category attribute as its own top-level JSON key using the exact
snake_case key shown above. Use null if the attribute is not visible in
any screenshot. Do NOT invent attributes not listed.

RULES:
- Numbers must be integers in rupees with NO commas or currency symbols
  (except `rating` which may be a decimal like 4.5).
- The LARGEST ₹ amount near the product title is almost always
  `price_inr`. The smaller ₹ amount with strike-through is `mrp_inr`.
- If price > mrp, you've swapped them — fix it.
- bank_offers must be an array of distinct short tagline strings, one per
  offer. If you see "+5 more offers" but only 2 are visible, return just
  the visible 2.
- For rating, if you see "4★ (24 Ratings)" then rating=4.0, rating_count=24.
- Do NOT echo the SKU or category back into the JSON.

RELEVANCE GATE (check FIRST, before extracting anything):
The user asked us to research a SPECIFIC SKU. Before extracting,
confirm the PDP is the same product CLASS as what the user described.
Use your judgment — no hard-coded checklist:
  - Same broad category (TV stays a TV, washing machine stays a
    washing machine, phone stays a phone).
  - Same brand when the user named one.
  - Same dominant spec the user named (e.g. if they said "65 inch",
    the PDP must not be a 32-inch TV; if they said "8kg", it must not
    be a 12kg machine).
A near-equivalent on a softer attribute is fine — if the user said
"LED" and this is a QLED / NanoCell TV, that's still LED-backlit;
extract it. The discovery step has already done a fit-score; trust
that and only block when the navigator clearly landed on the wrong
product CLASS.

If you decide to block, return EXACTLY this JSON and nothing else:
  {"blocked": true, "reason": "wrong_product: <pdp_title>"}

If the screenshots show ONLY a captcha, error page, or no PDP at all,
return: {"blocked": true, "reason": "<short reason>"}
"""


def _extract_from_screenshots(
    sku: str,
    screenshots: list[Path],
    plan: dict[str, Any],
    *,
    pdp_url: str | None,
    on_progress=None,
) -> dict[str, Any]:
    """Run a single vision LLM call over the captured screenshots.

    Uses CHAT_MODEL (full-size) because extraction quality matters more
    than cost here — but it's still one shot, ~$0.01 per SKU/retailer.
    Returns either a dict with the canonical fields filled in, or
    {"blocked": True, "reason": ...} on failure.
    """
    if not screenshots:
        return {"blocked": True, "reason": "no_screenshots"}

    try:
        from hub_cowork.core.agent_core import get_responses_client, CHAT_MODEL
    except Exception as ex:
        logger.warning("shelf_watch: extractor import failed (%s)", ex)
        return {"blocked": True, "reason": f"extractor_import_error: {ex}"}

    # Encode each screenshot as a base64 data URL. Skip unreadable files.
    image_parts: list[dict[str, Any]] = []
    for path in screenshots:
        try:
            data = path.read_bytes()
        except Exception as ex:
            logger.warning("shelf_watch: cannot read screenshot %s (%s)", path, ex)
            continue
        b64 = base64.b64encode(data).decode("ascii")
        image_parts.append({
            "type": "input_image",
            "image_url": f"data:image/png;base64,{b64}",
            "detail": "high",
        })

    if not image_parts:
        return {"blocked": True, "reason": "screenshots_unreadable"}

    category_block = _format_category_block(plan)
    prompt = _EXTRACTOR_PROMPT.replace("{category_block}", category_block)

    user_content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                f"SKU being researched: {sku}\n"
                f"PDP URL (for your reference, do not echo): {pdp_url or 'unknown'}\n\n"
                f"Below are {len(image_parts)} screenshot(s) captured top-to-bottom "
                "as the navigator scrolled through the PDP. Read all of them carefully "
                "and emit the JSON described in your instructions."
            ),
        }
    ]
    user_content.extend(image_parts)

    try:
        client = get_responses_client()
        resp = client.responses.create(
            model=CHAT_MODEL,
            instructions=prompt,
            input=[{"role": "user", "content": user_content}],
            tools=[],
        )
        text = ""
        for item in resp.output:
            if getattr(item, "type", None) == "message":
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", None) == "output_text":
                        text += getattr(part, "text", "") or ""
        payload = _extract_json_payload(text) or {}
        if not isinstance(payload, dict):
            return {"blocked": True, "reason": "extractor_non_dict"}
        return payload
    except Exception as ex:
        logger.exception("shelf_watch: vision extractor failed")
        return {"blocked": True, "reason": f"extractor_error: {ex}"}


SCHEMA = {
    "type": "function",
    "name": "compare_shelf_prices",
    "description": (
        "Two-pass shelf-watch: (1) Computer-Use (gpt-5.4 + Playwright "
        "Chromium) navigates each retailer's site to the SKU's PDP and "
        "scrolls through it; (2) a vision LLM extracts price, MRP, EMI, "
        "exchange offer, bank offers, delivery ETA, seller, warranty, "
        "rating, and category-specific specs (auto-planned per SKU by a "
        "small model) from the captured screenshots. Public PDPs only — "
        "no login, no cart, no captcha solving. Returns a structured "
        "comparison and persists raw JSON + screenshots under "
        "~/.hub-cowork/shelf_watch/runs/<timestamp>/."
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
                    "7kg FHV1207Z2B washer). Ignored when `variants` is "
                    "supplied."
                ),
            },
            "retailers": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Retailer keys to query. Defaults to all configured "
                    "retailers (Croma + Reliance Digital out of the box; "
                    "users can add more via `shelf_watch_retailers` in "
                    "hub_config.json). Ignored when `variants` is supplied."
                ),
            },
            "variants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sku": {"type": "string"},
                        "retailer": {"type": "string"},
                        "variant_title": {"type": "string"},
                        "variant_url": {
                            "type": "string",
                            "description": (
                                "Optional absolute PDP URL captured by "
                                "`discover_shelf_variants`. When present, "
                                "the navigator goes here directly and "
                                "skips the search-and-click step."
                            ),
                        },
                    },
                    "required": ["sku", "retailer", "variant_title"],
                },
                "description": (
                    "Explicit list of (SKU, retailer, variant_title) triples "
                    "to deep-scrape, normally produced by "
                    "`discover_shelf_variants` and confirmed by the user. "
                    "When supplied, the navigator opens the result whose "
                    "title matches `variant_title` rather than picking the "
                    "first match itself, and the row in the output carries "
                    "the same `variant_title`. Overrides `skus` + "
                    "`retailers`."
                ),
            },
            "headless": {
                "type": "boolean",
                "description": (
                    "Launch Chromium without a visible window. Defaults to "
                    "the `shelf_watch_headless` setting in hub_config.json "
                    "(false out of the box). Pass true/false here to override "
                    "for a single run. Headless is more discreet but currently "
                    "more likely to be challenged by retailer bot defenses."
                ),
            },
            "max_iterations_per_run": {
                "type": "integer",
                "description": (
                    "Safety cap on computer_call turns per (SKU, retailer) "
                    "pair. Default 30."
                ),
            },
            "prefer_structured_extraction": {
                "type": "boolean",
                "description": (
                    "When true, skip the vision-LLM extraction pass for any "
                    "PDP whose JSON-LD Product schema already covers the "
                    "canonical fields (price + product_title at minimum). "
                    "Bank offers / exchange / delivery / warranty columns "
                    "will be empty for those rows — that's the trade-off "
                    "for sub-second runs with no LLM calls per PDP. "
                    "Defaults to the "
                    "`shelf_watch_skip_vision_when_jsonld_complete` setting "
                    "in hub_config.json (false out of the box)."
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

    # ----- Phase 1: generic enrichments -----
    bank_offers_raw = (
        payload.get("bank_offers")
        or payload.get("offers")
        or payload.get("card_offers")
        or payload.get("available_offers")
    )
    if isinstance(bank_offers_raw, str):
        bank_offers = [bank_offers_raw]
    elif isinstance(bank_offers_raw, list):
        bank_offers = [str(x).strip() for x in bank_offers_raw if str(x).strip()]
    else:
        bank_offers = []

    delivery_eta = (
        payload.get("delivery_eta")
        or payload.get("delivery")
        or payload.get("delivery_by")
        or payload.get("eta")
    )
    if delivery_eta is not None and not isinstance(delivery_eta, str):
        delivery_eta = str(delivery_eta)

    seller = (
        payload.get("seller")
        or payload.get("sold_by")
        or payload.get("fulfilled_by")
    )
    if seller is not None and not isinstance(seller, str):
        seller = str(seller)

    warranty = payload.get("warranty") or payload.get("warranty_info")
    if warranty is not None and not isinstance(warranty, str):
        warranty = str(warranty)

    rating: float | None = None
    rating_raw = payload.get("rating") or payload.get("stars") or payload.get("avg_rating")
    if isinstance(rating_raw, (int, float)):
        rating = float(rating_raw)
    elif isinstance(rating_raw, str):
        m = re.search(r"\d+(?:\.\d+)?", rating_raw)
        if m:
            try:
                rating = float(m.group(0))
            except ValueError:
                rating = None

    rating_count = _to_int_rupees(
        payload.get("rating_count")
        or payload.get("review_count")
        or payload.get("num_reviews")
        or payload.get("reviews")
    )

    canonical = {
        "price_inr": price_inr,
        "mrp_inr": mrp_inr,
        "discount_pct": discount_pct,
        "emi_from_inr": emi_from_inr,
        "exchange_offer": exchange_offer,
        "in_stock": _to_in_stock(payload),
        "product_title": payload.get("product_title") or payload.get("product_name") or payload.get("title"),
        "url": payload.get("url") or payload.get("pdp_url"),
        "bank_offers": bank_offers,
        "delivery_eta": delivery_eta,
        "seller": seller,
        "warranty": warranty,
        "rating": rating,
        "rating_count": rating_count,
    }

    # ----- Phase 2: pass-through everything else as category_attrs -----
    # Anything the model returned that isn't one of our canonical / generic
    # keys is treated as a category-specific attribute and surfaced in the
    # report as a sub-table.
    reserved = set(canonical.keys()) | {
        # Input fields the model sometimes echoes back from user_task.
        "sku", "category", "brand", "retailer", "retailer_label",
        # Synonyms we consumed above.
        "price", "deal_price", "selling_price", "current_price", "sale_price",
        "offer_price", "mrp", "list_price", "original_price", "strike_price",
        "discount", "discount_percent",
        "emi_price", "emi_starting", "emi_from", "emi",
        "exchange", "exchange_bonus",
        "offers", "card_offers", "available_offers",
        "delivery", "delivery_by", "eta",
        "sold_by", "fulfilled_by",
        "warranty_info",
        "stars", "avg_rating",
        "review_count", "num_reviews", "reviews",
        "product_name", "title", "pdp_url",
        "availability", "stock", "stock_status", "in_stock_text",
        "blocked", "reason",
        # Generic disclaimers / boilerplate the model loves to attach.
        "price_inclusive_tax", "tax_inclusive", "inclusive_of_taxes",
        "currency",
    }
    category_attrs: dict[str, Any] = {}
    for k, v in payload.items():
        if k in reserved:
            continue
        if v in (None, "", [], {}):
            continue
        category_attrs[str(k)] = v
    if category_attrs:
        canonical["category_attrs"] = category_attrs

    return canonical


def _normalize_jsonld_to_canonical(jsonld: dict[str, Any]) -> dict[str, Any]:
    """Map a JSON-LD Product object to our canonical row schema.

    Returns only the fields that could actually be filled — missing keys
    stay absent so the caller can decide whether to fall back to vision
    or merge selectively. Tolerant of the three common offer shapes:

      offers: {Offer ...}                              (single)
      offers: [{Offer}, {Offer}, ...]                  (list)
      offers: {AggregateOffer, offers: [...], lowPrice, highPrice}
    """
    out: dict[str, Any] = {}

    name = jsonld.get("name") or jsonld.get("title")
    if isinstance(name, str) and name.strip():
        out["product_title"] = name.strip()

    offers = jsonld.get("offers")
    offer_list: list[dict[str, Any]] = []
    if isinstance(offers, dict):
        if isinstance(offers.get("offers"), list):
            # AggregateOffer wrapper.
            offer_list = [o for o in offers["offers"] if isinstance(o, dict)]
            # AggregateOffer also carries lowPrice/highPrice itself.
            if offers.get("lowPrice") is not None or offers.get("highPrice") is not None:
                offer_list.append(offers)
        else:
            offer_list = [offers]
    elif isinstance(offers, list):
        offer_list = [o for o in offers if isinstance(o, dict)]

    prices: list[int] = []
    in_stock_signal: bool | None = None
    for o in offer_list:
        for key in ("price", "lowPrice"):
            pi = _to_int_rupees(o.get(key))
            if pi is not None:
                prices.append(pi)
        phi = _to_int_rupees(o.get("highPrice"))
        if phi is not None:
            prices.append(phi)
        avail = o.get("availability") or o.get("itemAvailability") or ""
        if isinstance(avail, str) and avail:
            low = avail.lower()
            if (
                "instock" in low
                or "in_stock" in low
                or "limitedavailability" in low
                or low.endswith("/instock")
                or "available" in low
            ):
                in_stock_signal = True
            elif (
                "outofstock" in low
                or "out_of_stock" in low
                or "soldout" in low
                or "sold_out" in low
                or "discontinued" in low
            ):
                in_stock_signal = False

    if prices:
        unique_sorted = sorted(set(prices))
        out["price_inr"] = unique_sorted[0]
        if len(unique_sorted) >= 2:
            out["mrp_inr"] = unique_sorted[-1]

    if in_stock_signal is not None:
        out["in_stock"] = in_stock_signal

    rating = jsonld.get("aggregateRating")
    if isinstance(rating, dict):
        rv = rating.get("ratingValue")
        rc = rating.get("reviewCount") or rating.get("ratingCount")
        if rv is not None:
            try:
                out["rating"] = float(rv)
            except (TypeError, ValueError):
                pass
        if rc is not None:
            try:
                out["rating_count"] = int(float(rc)) if isinstance(rc, str) else int(rc)
            except (TypeError, ValueError):
                pass

    return out


def _canonical_from_structured(structured: Any) -> dict[str, Any]:
    """Pick the most-filled Product entry from the page's JSON-LD blocks
    and return its canonical-field mapping. Empty dict when nothing usable
    was found.
    """
    if not isinstance(structured, dict):
        return {}
    products = structured.get("jsonld_products")
    if not isinstance(products, list) or not products:
        return {}
    best: dict[str, Any] | None = None
    best_score = -1
    for p in products:
        if not isinstance(p, dict):
            continue
        score = 0
        if p.get("offers"):
            score += 3
        if p.get("name"):
            score += 2
        if p.get("aggregateRating"):
            score += 1
        if score > best_score:
            best_score = score
            best = p
    if best is None:
        return {}
    return _normalize_jsonld_to_canonical(best)


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    raw_variants = arguments.get("variants") or []
    headless: bool = bool(arguments.get("headless", _resolve_headless_default()))
    max_iter: int = int(arguments.get("max_iterations_per_run", 40))
    prefer_structured: bool = bool(
        arguments.get(
            "prefer_structured_extraction",
            _resolve_skip_vision_when_jsonld_complete(),
        )
    )

    # Build the unified work-list. Each work-item is a 4-tuple
    # (sku, retailer_key, variant_title, variant_url) where
    # variant_title / variant_url are None for the legacy
    # "let the navigator pick" behavior.
    work: list[tuple[str, str, str | None, str | None]] = []

    if raw_variants:
        # Variant-driven mode: skus / retailers args are ignored.
        for v in raw_variants:
            if not isinstance(v, dict):
                continue
            sku = (v.get("sku") or "").strip()
            rk = (v.get("retailer") or "").strip()
            vt = (v.get("variant_title") or "").strip()
            vu = (v.get("variant_url") or "").strip() or None
            if not (sku and rk and vt):
                continue
            if rk not in _RETAILERS:
                return error(
                    "compare_shelf_prices",
                    "config",
                    f"Unknown retailer in variants: {rk!r}. "
                    f"Supported: {list(_RETAILERS.keys())}",
                )
            work.append((sku, rk, vt, vu))
        if not work:
            return error(
                "compare_shelf_prices",
                "config",
                "`variants` was provided but contained no usable entries.",
            )
    else:
        # Legacy mode: cross-product of skus × retailers, no explicit titles.
        skus: list[str] = arguments.get("skus") or list(_DEFAULT_SKUS)
        retailer_keys: list[str] = arguments.get("retailers") or list(_RETAILERS.keys())
        unknown = [r for r in retailer_keys if r not in _RETAILERS]
        if unknown:
            return error(
                "compare_shelf_prices",
                "config",
                f"Unknown retailer(s): {unknown}. "
                f"Supported: {list(_RETAILERS.keys())}",
            )
        for sku in skus:
            for rk in retailer_keys:
                work.append((sku, rk, None, None))

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = APP_HOME / "shelf_watch" / "runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    locale, timezone_id = _resolve_region()

    rows: list[dict[str, Any]] = []
    total = len(work)
    done = 0

    # Cache the per-SKU attribute plan so we don't re-call the planner
    # once per variant.
    plans: dict[str, dict[str, Any]] = {}

    if on_progress:
        unique_skus = len({s for s, _, _, _ in work})
        unique_rs = len({r for _, r, _, _ in work})
        mode = "variants" if raw_variants else "skus×retailers"
        on_progress(
            "progress",
            f"**Shelf Watch starting** — {total} run(s) "
            f"({unique_skus} SKU(s), {unique_rs} retailer(s), {mode}). "
            f"Browser mode: {'headless' if headless else 'headed'}, "
            f"locale: {locale}, timezone: {timezone_id}.",
        )

    for sku, rk, variant_title, variant_url in work:
        # Plan attributes once per SKU — reused across every retailer / variant.
        if sku not in plans:
            if on_progress:
                on_progress("progress", f"Planning attributes for `{sku}`\u2026")
            plans[sku] = _plan_attributes(sku, on_progress=on_progress)
            if on_progress and plans[sku].get("attributes"):
                cat = plans[sku].get("category") or "(uncategorized)"
                keys = ", ".join(a["key"] for a in plans[sku]["attributes"])
                on_progress(
                    "progress",
                    f"Category planner: **{cat}** \u2014 will extract {keys}",
                )
        plan = plans[sku]

        done += 1
        cfg = _RETAILERS[rk]
        label = cfg["label"]
        start_url = cfg["start_url"]

        # Per-variant screenshot folder. When multiple variants share a
        # (sku, retailer) we suffix with a stable slug of the variant title.
        slug_base = f"{_slugify(sku)}__{rk}"
        if variant_title:
            shot_dir = run_dir / f"{slug_base}__{_slugify(variant_title)}"
        else:
            shot_dir = run_dir / slug_base

        target_label = f"`{variant_title}`" if variant_title else f"`{sku}`"
        if on_progress:
            via = " (direct PDP)" if variant_url else ""
            on_progress(
                "progress",
                f"[{done}/{total}] {label}: navigating to {target_label}{via}\u2026",
            )

        # Tighter user_task when we know the exact title to open.
        # When discovery captured a PDP URL, navigate there directly
        # and skip the search workflow entirely — saves the redundant
        # search-typing pass that just runs the same query as discovery.
        if variant_url:
            run_start_url = variant_url
            user_task = (
                f"You should already be on the {label} product page for "
                f'"{variant_title}". First verify the visible product '
                f"title matches (it should mention the SKU '{sku}'). If it "
                "does, scroll the entire PDP top-to-bottom (price, offers, "
                "delivery, specs, ratings) with at least 5 scroll steps of "
                "~600px so every section renders, then reply DONE. If the "
                "page is the wrong product, a 404, or a captcha/error, "
                "reply BLOCKED <short reason>. Do NOT navigate to search."
            )
        elif variant_title:
            run_start_url = start_url
            user_task = (
                f"On {label}'s site, search for '{sku}'. {cfg['search_hint']} "
                f"From the results, open the product whose title is "
                f"\"{variant_title}\" (or matches it as closely as possible). "
                "Then scroll through the entire PDP (price, offers, "
                "delivery, specs, ratings) and reply DONE."
            )
        else:
            run_start_url = start_url
            user_task = (
                f"Find the SKU '{sku}' on {label}'s site. "
                f"{cfg['search_hint']} Open the best-matching PDP, "
                "scroll through the entire page (price, offers, "
                "delivery, specs, ratings), then reply DONE."
            )

        # Pass 1 (CUA): navigate + scroll + scrape JSON-LD from the
        # rendered PDP. Extraction proper still happens in Pass 2 (vision)
        # below — the dom_extractor just gives us a structured ground-
        # truth source for the canonical fields (price, MRP, title,
        # in_stock, rating) so we can override vision when both have a
        # value, and optionally skip vision entirely when JSON-LD is
        # complete (controlled by `prefer_structured_extraction`).
        try:
            result = run_computer_use_task(
                instructions=_PER_RUN_INSTRUCTIONS,
                user_task=user_task,
                start_url=run_start_url,
                allow_domains=cfg["allow_domains"],
                max_iterations=max_iter,
                headless=headless,
                locale=locale,
                timezone_id=timezone_id,
                screenshot_dir=shot_dir,
                on_progress=on_progress,
                dom_extractor=_extract_pdp_structured,
            )
        except Exception as ex:
            logger.exception("compare_shelf_prices: harness raised")
            rows.append({
                "sku": sku, "retailer": rk, "retailer_label": label,
                "variant_title": variant_title,
                "blocked": True, "reason": f"harness_error: {ex}",
            })
            continue

        row: dict[str, Any] = {
            "sku": sku,
            "retailer": rk,
            "retailer_label": label,
            "variant_title": variant_title,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "iterations": result.iterations,
            "screenshots_dir": str(shot_dir),
            "visited_urls": result.visited_urls,
        }

        # Detect navigator-side blocks (BLOCKED <reason> reply or
        # harness signal). The new prompt asks for "DONE" or
        # "BLOCKED <reason>"; tolerate the legacy JSON form too.
        final_text = (result.final_text or "").strip()
        nav_blocked = result.blocked
        nav_reason = result.block_reason
        if not nav_blocked:
            upper = final_text.upper()
            if upper.startswith("BLOCKED"):
                nav_blocked = True
                nav_reason = final_text[7:].strip(": ").strip() or "blocked"
            else:
                # Legacy JSON {"blocked": true, "reason": "..."}
                legacy = _extract_json_payload(final_text) or {}
                if isinstance(legacy, dict) and legacy.get("blocked"):
                    nav_blocked = True
                    nav_reason = legacy.get("reason") or "model_blocked"

        if nav_blocked:
            row["blocked"] = True
            row["reason"] = nav_reason or "harness_blocked"
            rows.append(row)
            if on_progress:
                on_progress(
                    "progress",
                    f"[{done}/{total}] {label}: \u26a0 navigation blocked ({row['reason']})",
                )
            continue

        # JSON-LD pre-fill from the rendered PDP. The dom_extractor on
        # the harness call returned the page's structured Product schema
        # (when present). We use it as ground truth for canonical fields
        # below \u2014 and, when complete, can skip the vision pass entirely
        # under `prefer_structured_extraction`.
        structured = (
            result.extracted_data if isinstance(result.extracted_data, dict) else {}
        )
        jsonld_canonical = _canonical_from_structured(structured)
        jsonld_complete = (
            jsonld_canonical.get("price_inr") is not None
            and bool(jsonld_canonical.get("product_title"))
        )

        pdp_url = result.visited_urls[-1] if result.visited_urls else None

        # Fast path: when JSON-LD covered the canonical fields and the
        # caller opted into structured-only extraction, skip vision
        # entirely. Bank offers / exchange / delivery columns will be
        # empty for this row \u2014 that's the trade-off for sub-second runs.
        if prefer_structured and jsonld_complete:
            if on_progress:
                on_progress(
                    "progress",
                    f"[{done}/{total}] {label}: JSON-LD captured canonical "
                    f"fields \u2014 skipping vision pass",
                )
            row["blocked"] = False
            row.update(jsonld_canonical)
            # Derive discount when both prices came through.
            p_val, m_val = row.get("price_inr"), row.get("mrp_inr")
            if (
                isinstance(p_val, (int, float))
                and isinstance(m_val, (int, float))
                and m_val > p_val
            ):
                row["discount_pct"] = int(round((m_val - p_val) * 100 / m_val))
            if not row.get("url") and pdp_url:
                row["url"] = pdp_url
            row["variant_title"] = variant_title
            row["extraction_method"] = "jsonld"
            rows.append(row)
            if on_progress:
                price_str = (
                    f"\u20b9{row['price_inr']:,}" if isinstance(row.get("price_inr"), int)
                    else "(price not read)"
                )
                on_progress(
                    "progress",
                    f"[{done}/{total}] {label}: {price_str} \u2014 "
                    f"{row.get('product_title') or variant_title or sku} (json-ld)",
                )
            continue

        # Pass 2 (vision LLM): extract structured fields from the
        # screenshots the navigator captured.
        if on_progress:
            on_progress(
                "progress",
                f"[{done}/{total}] {label}: extracting from "
                f"{len(result.screenshots)} screenshot(s)\u2026",
            )
        payload = _extract_from_screenshots(
            sku=sku,
            screenshots=result.screenshots,
            plan=plan,
            pdp_url=pdp_url,
            on_progress=on_progress,
        )
        logger.info(
            "compare_shelf_prices: %s/%s extractor parsed=%r blocked=%s",
            rk, _slugify(sku), payload, payload.get("blocked"),
        )

        if payload.get("blocked"):
            row["blocked"] = True
            row["reason"] = payload.get("reason") or "extractor_blocked"
        else:
            row["blocked"] = False
            row.update(_normalize_payload(payload))
            # JSON-LD wins for canonical fields when both have a value \u2014
            # structured ground truth beats vision OCR on numeric fields,
            # and vision still contributes the bank_offers / exchange /
            # delivery / seller / warranty enrichments it does best.
            for k in ("price_inr", "mrp_inr", "in_stock", "product_title",
                      "rating", "rating_count"):
                if jsonld_canonical.get(k) is not None:
                    row[k] = jsonld_canonical[k]
            # Re-derive discount when both prices ended up coming from
            # JSON-LD (the original vision-derived discount may not
            # match the post-merge price/MRP).
            p_val, m_val = row.get("price_inr"), row.get("mrp_inr")
            if (
                isinstance(p_val, (int, float))
                and isinstance(m_val, (int, float))
                and m_val > p_val
            ):
                row["discount_pct"] = int(round((m_val - p_val) * 100 / m_val))
            if not row.get("url") and pdp_url:
                row["url"] = pdp_url
            # Preserve the user-confirmed variant_title even after
            # _normalize_payload runs (which might overwrite product_title
            # but never variant_title).
            row["variant_title"] = variant_title
            row["extraction_method"] = (
                "jsonld+vision" if jsonld_canonical else "vision"
            )
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
                    f"{row.get('product_title') or variant_title or sku}",
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
