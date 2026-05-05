"""
Tool: discover_shelf_variants

Two-pass *discovery* sweep — one per (SKU × retailer) — that returns the
list of products on each retailer that match the user's description.

  Pass 1 — Computer-Use navigates the retailer's homepage → search box →
           hits Enter → scrolls the results page top-to-bottom (once or
           twice). It NEVER clicks into a PDP. It replies DONE or
           BLOCKED <reason>.

  Pass 2 — A single vision LLM call reads the results-page screenshots
           together with the user's SKU description and returns up to N
           plausible variants per retailer. Each variant comes back
           with a verbatim `title`, an integer `match_pct` (0-100) fit
           score, a short `gaps` note explaining what differs from the
           user's request, and an integer `position` in the result list.
           The user — not the LLM — picks the final variant; the score
           is presented so they can decide.

The skill calls this BEFORE `compare_shelf_prices` so the user can
confirm which variants to deep-scrape. Cheap relative to the full
scrape: ~1 CUA session and 1 vision call per (SKU, retailer), no PDP
visit, no per-variant extraction.
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
# Reuse the retailer registry, region resolver, headless default, slug,
# and JSON-extraction helpers from the sibling deep-scrape tool so we
# always stay in lockstep with whatever the user has configured.
from hub_cowork.skills.shelf_watch.tools._compare import (
    _RETAILERS,
    _resolve_region,
    _resolve_headless_default,
    _slugify,
    _extract_json_payload,
)

logger = logging.getLogger("hub_se_agent")


_DEFAULT_MAX_VARIANTS = 3


# JS that returns visible product-card anchors from the search-results
# page. Filters to anchors whose href looks like a PDP link (contains a
# `/p/` segment, which both Croma and Reliance Digital use, OR is a
# direct product slug). Also captures the most useful title text — the
# most specific element inside the anchor (longest text node typically
# is the product title).
_CARD_LINK_JS = """
() => {
  const out = [];
  const seen = new Set();
  const anchors = document.querySelectorAll('a[href]');
  for (const a of anchors) {
    let href = a.href || '';
    if (!href) continue;
    // Common PDP URL shapes across Croma / Reliance Digital / similar.
    // Match either an explicit `/p/` segment or any path with at least
    // 3 hyphenated slug words (heuristic for product slugs).
    const isPdpLike =
      /\\/p\\//i.test(href) ||
      /\\/product\\//i.test(href) ||
      /-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+/i.test(href);
    if (!isPdpLike) continue;
    if (seen.has(href)) continue;
    // Skip empty / icon-only anchors.
    const text = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!text || text.length < 8) continue;
    seen.add(href);
    out.push({ url: href, title: text.slice(0, 300) });
    if (out.length >= 50) break;
  }
  return out;
}
"""


async def _extract_card_links(page) -> list[dict[str, str]]:
    """DOM extractor passed to the CUA harness. Returns visible PDP-like
    anchors from the search-results page so the triage LLM can pair
    titles with URLs and the deep-scrape can `goto` the PDP directly.
    """
    try:
        return await page.evaluate(_CARD_LINK_JS)
    except Exception as ex:
        logger.warning("discover_shelf_variants: card-link scrape failed (%s)", ex)
        return []


def _resolve_max_variants_default() -> int:
    """Pull `shelf_watch_max_variants_per_retailer` from hub_config."""
    try:
        from hub_cowork.core import hub_config
        cfg = hub_config.load()
        v = int(cfg.get("shelf_watch_max_variants_per_retailer") or _DEFAULT_MAX_VARIANTS)
        return max(1, min(v, 10))   # sanity cap
    except Exception:
        return _DEFAULT_MAX_VARIANTS


_DISCOVERY_INSTRUCTIONS = """\
You are a browser navigator running inside a real Chromium browser.
Your ONLY job is to land on the retailer's SEARCH RESULTS PAGE for the
requested SKU and scroll through it so a separate vision agent can read
the result list. You do NOT need to extract anything yourself, and you
MUST NOT click into any individual product detail page.

RULES (non-negotiable):
1. Stay on the retailer's own domain (and its CDN / storefront subdomains).
   Do not visit search engines, ad redirects, payment partners, or third-party
   comparison sites.
2. Do NOT log in. Do NOT add anything to cart. Do NOT click any product card.
3. Do NOT attempt to solve any CAPTCHA. If you see one, stop and reply
   exactly: BLOCKED captcha
4. Do NOT type a pin-code if a delivery widget asks for one. Just leave it.
5. If a location / cookie / app-install / newsletter popup blocks the page,
   dismiss it (close button or "Maybe later") and continue.

WORKFLOW:
1. The current page is the retailer's HOMEPAGE.
2. Find the search box, type the SKU exactly as given, press Enter.
3. Wait for the results grid to render.
4. Scroll the results page slowly with at least 3 scroll actions of ~600px
   each, pausing briefly between scrolls so each row of results renders.
   Cover at least the first 6-8 results (or all results if fewer).
5. Once you have scrolled past the visible results area, reply with
   exactly one word: DONE
6. If the search returns zero results, reply exactly: BLOCKED no_results

Do NOT click any product card. Do NOT navigate to a PDP. Do NOT return
any JSON or product data. The downstream pipeline reads the screenshots
you captured. Your reply must be either DONE or BLOCKED <reason>. Aim
for 5-9 computer-tool turns total.
"""


_TRIAGE_PROMPT = """\
You are a meticulous retail catalogue analyst. You will be shown a
sequence of screenshots captured by a navigator agent as it scrolled
through ONE search-results page on an Indian retail website. Alongside
the screenshots you will receive a JSON list of candidate product-card
links scraped from the same page (`title` text + absolute `url`). Your
job is to identify products on that results page that plausibly match
the SKU description the user gave, and pair each with the right URL
from the candidate list.

OUTPUT (final assistant message — JSON only, no prose, no markdown fences):

{
  "matches": [
    {
      "title": "<verbatim product title as shown on the results card>",
      "url": "<the absolute URL from the candidate list whose title matches this card; null if no candidate matches>",
      "match_pct": <integer 0-100 — your overall fit score, see below>,
      "gaps": "<one short clause: what attributes line up AND what the "
              "user mentioned that this listing does NOT satisfy. Empty "
              "string if it's a clean match.>",
      "position": <1-based integer rank in the result list, top-to-bottom>,
      "price_hint_inr": <integer rupees if a price is visible on the card, else null>
    },
    ...
  ]
}

HOW TO PAIR title → url:
The candidate list is in the same top-to-bottom order as the cards on
the page. Pick the candidate whose title has the strongest word overlap
with the card's title (model number, brand, key spec). When in doubt
prefer the candidate at the same position. Only set `url` to null when
no candidate plausibly matches.

HOW TO SCORE (use your own judgment — no hard-coded attribute list):
Read the SKU description as a human shopper would. Identify the
attributes the user actually specified (could be brand, size, capacity,
storage, model line, panel type, color, generation, anything). For each
candidate on the results page, weigh how many of those attributes the
listing satisfies and how central each one is. Score the overall fit
0-100 where:
  - 100 = every specified attribute matches; the listing is exactly
          what the user described.
  -  80 = strong match on the dominant attributes (brand + the main
          spec like size or capacity); a softer attribute may be
          unspecified on the card or substituted with a near-equivalent
          (e.g. user said "LED" and the listing is "QLED" / "NanoCell"
          which are still LED-backlit).
  -  60 = brand and one major spec match; one secondary attribute the
          user named is missing or different.
  -  40 = brand matches but a major spec is off, or vice-versa.
  - <40 = clearly a different product (different brand, wildly
          different size/category, refurbished/accessory/bundle).

INCLUSION RULE:
Include every candidate scoring >= 40 in `matches`. The user — not
you — picks the final variant; your job is to surface plausible
options with honest scores so they can choose. ALWAYS prefer including
a borderline candidate with a clear `gaps` note over silently dropping
it. Skip only refurbished, used, accessory, bundle, warranty-only
listings, and obvious mismatches (<40).

LIMITS:
- Return AT MOST {max_variants} matches. If more qualify, keep the
  highest-scoring ones; tie-break on in-stock first.
- If ZERO products on the results page score >= 40, return:
    {"matches": []}
- If the screenshots show ONLY a captcha, error page, or no results at
  all, return: {"blocked": true, "reason": "<short reason>"}

Use the EXACT product title text you can read on the card — do not
summarize or paraphrase. The title will be used downstream to tell the
navigator which card to click.
"""


def _triage_results(
    sku: str,
    screenshots: list[Path],
    *,
    max_variants: int,
    retailer_label: str,
    candidate_links: list[dict[str, str]] | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """Run a single vision LLM call over the search-results screenshots.

    `candidate_links` is the list of {title, url} pairs scraped from the
    DOM by the discovery harness. The triage LLM picks the matching URL
    for each variant from this list so the deep-scrape step can `goto`
    the PDP directly without re-running the search.
    """
    if not screenshots:
        return {"blocked": True, "reason": "no_screenshots"}

    try:
        from hub_cowork.core.agent_core import get_responses_client, CHAT_MODEL
    except Exception as ex:
        logger.warning("discover_shelf_variants: client import failed (%s)", ex)
        return {"blocked": True, "reason": f"client_import_error: {ex}"}

    image_parts: list[dict[str, Any]] = []
    for shot in screenshots:
        try:
            data = shot.read_bytes()
            if not data:
                continue
            b64 = base64.b64encode(data).decode("ascii")
        except Exception as ex:
            logger.warning("discover_shelf_variants: cannot read %s (%s)", shot, ex)
            continue
        image_parts.append({
            "type": "input_image",
            "image_url": f"data:image/png;base64,{b64}",
            "detail": "high",
        })

    if not image_parts:
        return {"blocked": True, "reason": "screenshots_unreadable"}

    prompt = _TRIAGE_PROMPT.replace("{max_variants}", str(max_variants))
    # Trim the candidate-link list defensively: a search results page
    # can hold dozens of anchors, but we only need enough to cover the
    # cards that are visible in the screenshots. Keep the first 30 and
    # only the fields the prompt asks for.
    trimmed_links: list[dict[str, str]] = []
    for c in (candidate_links or [])[:30]:
        if not isinstance(c, dict):
            continue
        url = (c.get("url") or "").strip()
        title = (c.get("title") or "").strip()
        if not url or not title:
            continue
        trimmed_links.append({"title": title[:200], "url": url})
    links_blob = json.dumps(trimmed_links, ensure_ascii=False)

    user_content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                f"Retailer: {retailer_label}\n"
                f"SKU description from the user: {sku}\n\n"
                f"Candidate product-card links scraped from the page "
                f"(top-to-bottom, JSON):\n{links_blob}\n\n"
                f"Below are {len(image_parts)} screenshot(s) of the search-results "
                "page captured top-to-bottom. Identify up to "
                f"{max_variants} matching products, pair each with the "
                "correct `url` from the candidate list above, and return "
                "JSON as instructed."
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
            return {"blocked": True, "reason": "triage_non_dict"}
        return payload
    except Exception as ex:
        logger.exception("discover_shelf_variants: vision triage failed")
        return {"blocked": True, "reason": f"triage_error: {ex}"}


SCHEMA = {
    "type": "function",
    "name": "discover_shelf_variants",
    "description": (
        "Discovery sweep for the shelf-watch skill: for each (SKU × "
        "retailer), drive Chromium to the retailer's search-results page "
        "and use a vision LLM to identify up to N product variants that "
        "match the user's SKU description (brand + size + capacity + "
        "model line). Returns the candidate list so the skill can ask "
        "the user which variants to deep-scrape with `compare_shelf_prices`. "
        "No PDP visits, no extraction. Cheap and fast relative to the "
        "full scrape."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skus": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of SKU descriptions to discover.",
            },
            "retailers": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Retailer keys to query. Defaults to all configured "
                    "retailers."
                ),
            },
            "max_variants_per_retailer": {
                "type": "integer",
                "description": (
                    "Hard cap on how many variants each retailer may "
                    "contribute per SKU. Defaults to the "
                    "`shelf_watch_max_variants_per_retailer` config "
                    "value (default 3, capped at 10)."
                ),
            },
            "headless": {
                "type": "boolean",
                "description": (
                    "Launch Chromium without a visible window. Defaults "
                    "to the `shelf_watch_headless` setting."
                ),
            },
            "max_iterations_per_run": {
                "type": "integer",
                "description": (
                    "Safety cap on computer_call turns per (SKU, "
                    "retailer) pair. Default 20 — discovery is shorter "
                    "than full PDP capture."
                ),
            },
        },
        "required": [],
    },
}


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    skus: list[str] = arguments.get("skus") or []
    if not skus:
        return error(
            "discover_shelf_variants",
            "config",
            "At least one SKU is required for discovery.",
        )
    retailer_keys: list[str] = arguments.get("retailers") or list(_RETAILERS.keys())
    headless: bool = bool(arguments.get("headless", _resolve_headless_default()))
    max_iter: int = int(arguments.get("max_iterations_per_run", 20))
    max_variants: int = int(
        arguments.get("max_variants_per_retailer") or _resolve_max_variants_default()
    )
    max_variants = max(1, min(max_variants, 10))

    unknown = [r for r in retailer_keys if r not in _RETAILERS]
    if unknown:
        return error(
            "discover_shelf_variants",
            "config",
            f"Unknown retailer(s): {unknown}. Supported: {list(_RETAILERS.keys())}",
        )

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = APP_HOME / "shelf_watch" / "discovery" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    locale, timezone_id = _resolve_region()

    discoveries: list[dict[str, Any]] = []
    total = len(skus) * len(retailer_keys)
    done = 0

    if on_progress:
        on_progress(
            "progress",
            f"**Variant discovery starting** — {len(skus)} SKU(s) × "
            f"{len(retailer_keys)} retailer(s) = {total} sweep(s). "
            f"Up to {max_variants} variant(s) per retailer.",
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
                    f"[{done}/{total}] {label}: searching for `{sku}`\u2026",
                )

            entry: dict[str, Any] = {
                "sku": sku,
                "retailer": rk,
                "retailer_label": label,
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "screenshots_dir": str(shot_dir),
                "matches": [],
            }

            # Pass 1 (CUA): navigate to search results, scroll. Don't click
            # into any PDP.
            try:
                result = run_computer_use_task(
                    instructions=_DISCOVERY_INSTRUCTIONS,
                    user_task=(
                        f"Find the search results for '{sku}' on "
                        f"{label}'s site. {cfg['search_hint']} Once the "
                        "results grid is visible, scroll down to reveal "
                        "the full first page of results, then reply DONE. "
                        "Do NOT click into any product."
                    ),
                    start_url=start_url,
                    allow_domains=cfg["allow_domains"],
                    max_iterations=max_iter,
                    headless=headless,
                    locale=locale,
                    timezone_id=timezone_id,
                    screenshot_dir=shot_dir,
                    on_progress=on_progress,
                    dom_extractor=_extract_card_links,
                )
            except Exception as ex:
                logger.exception("discover_shelf_variants: harness raised")
                entry["blocked"] = True
                entry["reason"] = f"harness_error: {ex}"
                discoveries.append(entry)
                continue

            entry["iterations"] = result.iterations
            entry["visited_urls"] = result.visited_urls

            final_text = (result.final_text or "").strip()
            nav_blocked = result.blocked
            nav_reason = result.block_reason
            if not nav_blocked:
                upper = final_text.upper()
                if upper.startswith("BLOCKED"):
                    nav_blocked = True
                    nav_reason = final_text[7:].strip(": ").strip() or "blocked"

            if nav_blocked:
                entry["blocked"] = True
                entry["reason"] = nav_reason or "harness_blocked"
                discoveries.append(entry)
                if on_progress:
                    on_progress(
                        "progress",
                        f"[{done}/{total}] {label}: \u26a0 discovery blocked ({entry['reason']})",
                    )
                continue

            # Pass 2: triage the results screenshots.
            if on_progress:
                on_progress(
                    "progress",
                    f"[{done}/{total}] {label}: triaging "
                    f"{len(result.screenshots)} result-page screenshot(s)\u2026",
                )

            triage = _triage_results(
                sku=sku,
                screenshots=result.screenshots,
                max_variants=max_variants,
                retailer_label=label,
                candidate_links=result.extracted_data if isinstance(result.extracted_data, list) else None,
                on_progress=on_progress,
            )

            if triage.get("blocked"):
                entry["blocked"] = True
                entry["reason"] = triage.get("reason") or "triage_blocked"
                discoveries.append(entry)
                if on_progress:
                    on_progress(
                        "progress",
                        f"[{done}/{total}] {label}: \u26a0 triage blocked ({entry['reason']})",
                    )
                continue

            raw_matches = triage.get("matches") or []
            cleaned: list[dict[str, Any]] = []
            for m in raw_matches[:max_variants]:
                if not isinstance(m, dict):
                    continue
                title = (m.get("title") or "").strip()
                if not title:
                    continue
                # match_pct is the new fit score (0-100). Tolerate older
                # `why_match`-only payloads by leaving the score absent
                # rather than synthesising one.
                pct_raw = m.get("match_pct")
                try:
                    match_pct = int(pct_raw) if pct_raw is not None else None
                except (TypeError, ValueError):
                    match_pct = None
                if match_pct is not None:
                    match_pct = max(0, min(100, match_pct))
                gaps = (m.get("gaps") or m.get("why_match") or "").strip()
                url = (m.get("url") or "").strip() or None
                cleaned.append({
                    "title": title,
                    "url": url,
                    "match_pct": match_pct,
                    "gaps": gaps,
                    "position": int(m.get("position") or 0) or None,
                    "price_hint_inr": m.get("price_hint_inr"),
                })
            # Highest-scoring first so the skill can present them in
            # decreasing order of confidence.
            cleaned.sort(key=lambda c: (c.get("match_pct") is None, -(c.get("match_pct") or 0)))
            entry["matches"] = cleaned
            entry["blocked"] = False
            discoveries.append(entry)

            if on_progress:
                if not cleaned:
                    on_progress(
                        "progress",
                        f"[{done}/{total}] {label}: 0 matches found for `{sku}`",
                    )
                else:
                    bullets = "; ".join(
                        f"{i+1}. {m['title'][:60]}" for i, m in enumerate(cleaned)
                    )
                    on_progress(
                        "progress",
                        f"[{done}/{total}] {label}: {len(cleaned)} match(es) — {bullets}",
                    )

    # Persist for audit + downstream tools.
    out_json = run_dir / "discovery.json"
    try:
        out_json.write_text(
            json.dumps(
                {"timestamp": timestamp, "discoveries": discoveries},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as ex:
        logger.warning("discover_shelf_variants: failed to write %s (%s)", out_json, ex)

    payload = {
        "timestamp": timestamp,
        "run_dir": str(run_dir),
        "discovery_json_path": str(out_json),
        "max_variants_per_retailer": max_variants,
        "discoveries": discoveries,
    }
    return ok("discover_shelf_variants", json.dumps(payload, ensure_ascii=False))
