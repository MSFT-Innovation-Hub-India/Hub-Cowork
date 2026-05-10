"""Tool: shelf_watch_run — single orchestrator for the Shelf Watch skill.

This tool is the *only* shelf-watch capability the agent sees. It owns
all deterministic flow: SKU plausibility check, discovery sweep,
match-score gating, variant disambiguation prompting, deep scrape,
report build. The agent's job is purely conversational — translate
the user's natural-language reply into one of a small set of
structured `user_choice` tokens, then call this tool again.

Flow (multi-turn, state in `_session.py`):

    1. First call (no saved session) →
         a. Optional plausibility check on the SKU strings.
         b. If concerns → save state, return needs_plausibility_confirmation.
         c. Else → run discovery → analyse matches.
         d. If any (sku, retailer) needs disambiguation → save state,
            return needs_disambiguation.
         e. Else → deep scrape → build report → return complete.

    2. Continuation (saved session, user_choice supplied) →
         awaiting_plausibility:
            user_choice ∈ {accept_suggestions, keep_original, cancel}
            (when accept_suggestions, the agent re-passes corrected `skus`)
         awaiting_disambiguation:
            user_choice ∈ {proceed, include_borderline, top_only,
                           custom, cancel}
            (when custom, the agent supplies `selected_variants`)

Returns one of four envelopes (inside the standard `ok(...)` wrapper):

    {"stage": "needs_plausibility_confirmation",
     "concerns": [{"sku", "issue", "suggested"}, ...],
     "options": [...],
     "prompt_hint": "..."}

    {"stage": "needs_disambiguation",
     "summary": [{"sku", "retailers": [{"label", "strong":[...],
                                        "borderline":[...]}]}, ...],
     "all_borderline_skus": [...],
     "default_variant_count": int,
     "options": [...],
     "prompt_hint": "..."}

    {"stage": "complete",
     "report_markdown": "...",
     "previous_run_timestamp": str | None,
     "rows_captured": int,
     "rows_blocked": int}

    {"stage": "cancelled"}

Strong = match_pct >= 80. Borderline = 40 <= match_pct < 80.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from hub_cowork.mcp_servers._tool_result import ok, error

from hub_cowork.skills.shelf_watch.mcp_server.tools import _session
from hub_cowork.skills.shelf_watch.mcp_server.tools import _discover
from hub_cowork.skills.shelf_watch.mcp_server.tools import _compare
from hub_cowork.skills.shelf_watch.mcp_server.tools import _report

logger = logging.getLogger("hub_se_agent")

_STRONG_MATCH_THRESHOLD = 80
_BORDERLINE_MIN = 40

_DEFAULT_SKUS = [
    "Apple iPhone 16 128GB Black",
    "Samsung 55 inch QN90D Neo QLED 4K TV",
    "LG 7kg Front Load Washing Machine FHV1207Z2B",
]
_DEFAULT_RETAILERS = ["croma", "reliance_digital"]

_RETAILER_LABELS = {
    "croma": "Croma",
    "reliance_digital": "Reliance Digital",
}


SCHEMA = {
    "type": "function",
    "name": "shelf_watch_run",
    "description": (
        "Run the Shelf Watch competitive-pricing comparison for a small "
        "set of consumer-electronics SKUs across Croma and Reliance "
        "Digital. This is a multi-turn orchestrator: it may return "
        "intermediate envelopes asking the user to confirm SKU "
        "interpretation or to disambiguate between multiple matching "
        "variants. The agent's only job between calls is to translate "
        "the user's reply into the next `user_choice` token. "
        "Returns a JSON envelope whose `stage` field is one of: "
        "`needs_plausibility_confirmation`, `needs_disambiguation`, "
        "`complete`, `cancelled`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skus": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Consumer-electronics SKU descriptions to compare. "
                    "Required on the first call. On continuation calls "
                    "this is an optional override — pass it when the "
                    "user has corrected a SKU string (e.g. accepted a "
                    "plausibility suggestion). When omitted on a "
                    "continuation, the saved scope is reused."
                ),
            },
            "retailers": {
                "type": "array",
                "items": {"type": "string", "enum": list(_RETAILER_LABELS)},
                "description": (
                    "Retailer keys to scrape. Defaults to both supported "
                    "sites (`croma` and `reliance_digital`)."
                ),
            },
            "headless": {
                "type": "boolean",
                "description": (
                    "Launch Chromium without a visible window. Default "
                    "false (headed) so the user can watch the runs."
                ),
            },
            "user_choice": {
                "type": "string",
                "enum": [
                    "accept_suggestions",
                    "keep_original",
                    "proceed",
                    "include_borderline",
                    "top_only",
                    "custom",
                    "cancel",
                ],
                "description": (
                    "Required on continuation calls. Translates the "
                    "user's natural-language reply into a structured "
                    "choice. `accept_suggestions` / `keep_original` / "
                    "`cancel` answer a `needs_plausibility_confirmation`. "
                    "`proceed` (= strong matches only, the default) / "
                    "`include_borderline` / `top_only` / `custom` / "
                    "`cancel` answer a `needs_disambiguation`. Use "
                    "`custom` when the user hand-picks a subset and pass "
                    "the picks via `selected_variants`."
                ),
            },
            "selected_variants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sku": {"type": "string"},
                        "retailer": {"type": "string"},
                        "variant_title": {"type": "string"},
                        "variant_url": {"type": "string"},
                    },
                    "required": ["sku", "retailer", "variant_title"],
                },
                "description": (
                    "Hand-picked variants to deep-scrape. Used only when "
                    "`user_choice` is `custom`. Each entry must have "
                    "been present in the prior `needs_disambiguation` "
                    "summary; copy `variant_title` and `variant_url` "
                    "verbatim from there."
                ),
            },
            "skip_plausibility_check": {
                "type": "boolean",
                "description": (
                    "Skip the LLM-based SKU plausibility pre-flight. "
                    "Default false (the check runs)."
                ),
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unwrap_envelope(envelope_str: str) -> dict[str, Any]:
    """Parse the standard `ok(tool, json_payload)` wrapper from a helper."""
    try:
        outer = json.loads(envelope_str)
    except Exception as ex:
        raise RuntimeError(f"helper returned non-JSON: {ex}") from ex
    if outer.get("status") != "ok":
        # Surface the helper's error verbatim.
        raise RuntimeError(
            outer.get("error", {}).get("message")
            or outer.get("data")
            or "helper returned non-ok envelope"
        )
    data = outer.get("data")
    if isinstance(data, str):
        try:
            return json.loads(data)
        except Exception:
            return {"_raw": data}
    return data or {}


def _classify_matches(matches: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split discovery matches into strong vs borderline."""
    strong: list[dict[str, Any]] = []
    borderline: list[dict[str, Any]] = []
    for m in matches or []:
        pct = m.get("match_pct")
        if isinstance(pct, (int, float)):
            if pct >= _STRONG_MATCH_THRESHOLD:
                strong.append(m)
            elif pct >= _BORDERLINE_MIN:
                borderline.append(m)
            # else: dropped (LLM judged it a clear mismatch)
        else:
            # No score → treat as borderline so the user decides.
            borderline.append(m)
    return {"strong": strong, "borderline": borderline}


def _check_plausibility(
    skus: list[str], *, on_progress=None
) -> list[dict[str, str]]:
    """LLM pre-flight: spot SKU strings that obviously cannot exist as
    written (e.g. "LG HD LED TV 65 inch" — 720p panels don't ship at
    65"). Returns a list of concern dicts; empty when all plausible.

    Uses the small chat model. Best-effort: any failure returns [] so
    the orchestrator continues rather than blocking on a dependency.
    """
    if not skus:
        return []
    try:
        from hub_cowork.core.agent_core import get_responses_client, CHAT_MODEL_SMALL
    except Exception as ex:
        logger.warning("shelf_watch_run: plausibility client import failed (%s)", ex)
        return []

    prompt = (
        "You are a retail catalogue analyst. For each SKU description "
        "below, decide whether the product as written PLAUSIBLY EXISTS "
        "in the Indian consumer-electronics market (Croma, Reliance "
        "Digital, Amazon India). Flag a SKU ONLY when it is clearly "
        "impossible or would not match any real listing, e.g.:\n"
        '  - "HD" (720p) at 55"+ — large TVs are 4K UHD or 8K\n'
        '  - "8K" below ~55" — 8K panels do not ship at small sizes\n'
        '  - QLED attributed to LG, OLED to Samsung\'s mass tier\n'
        '  - Storage that does not exist for a model line ("iPhone 16 64GB")\n'
        '  - Capacity that does not match form factor ("12kg semi-automatic")\n\n'
        "Do NOT flag SKUs that are merely vague — only flag what is "
        "implausible. For each flagged SKU, propose the closest realistic "
        "alternative.\n\n"
        "Return STRICT JSON, no prose, no markdown fences:\n"
        '  {"concerns": [{"sku": "<exact input>", "issue": "<one short clause>", '
        '"suggested": "<plausible alternative>"}]}\n'
        'When everything is plausible, return: {"concerns": []}'
    )
    user_text = "SKUs to check:\n" + "\n".join(f"- {s}" for s in skus)

    try:
        client = get_responses_client()
        resp = client.responses.create(
            model=CHAT_MODEL_SMALL,
            instructions=prompt,
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": user_text}
            ]}],
            tools=[],
        )
        text = ""
        for item in resp.output:
            if getattr(item, "type", None) == "message":
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", None) == "output_text":
                        text += getattr(part, "text", "") or ""
        # Tolerant JSON extraction.
        text = text.strip()
        if text.startswith("```"):
            # Strip fences.
            lines = text.splitlines()
            text = "\n".join(l for l in lines if not l.startswith("```"))
        payload = json.loads(text) if text else {}
        concerns = payload.get("concerns") or []
        cleaned: list[dict[str, str]] = []
        for c in concerns:
            if not isinstance(c, dict):
                continue
            sku = (c.get("sku") or "").strip()
            issue = (c.get("issue") or "").strip()
            suggested = (c.get("suggested") or "").strip()
            if sku and issue:
                cleaned.append({"sku": sku, "issue": issue, "suggested": suggested})
        return cleaned
    except Exception as ex:
        logger.warning("shelf_watch_run: plausibility check failed (%s)", ex)
        return []


def _build_summary(discoveries: list[dict[str, Any]]) -> dict[str, Any]:
    """Group discoveries by SKU and pre-classify matches for the agent.

    Returns a dict ready to be embedded in the needs_disambiguation
    envelope: the agent renders it as markdown, applies no thresholds.
    """
    by_sku: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for d in discoveries:
        sku = d.get("sku") or ""
        if sku not in by_sku:
            by_sku[sku] = []
            order.append(sku)
        by_sku[sku].append(d)

    summary: list[dict[str, Any]] = []
    all_borderline_skus: list[str] = []
    default_count = 0

    for sku in order:
        retailers_out: list[dict[str, Any]] = []
        sku_has_strong = False
        for d in by_sku[sku]:
            rk = d.get("retailer") or ""
            label = d.get("retailer_label") or _RETAILER_LABELS.get(rk, rk)
            if d.get("blocked"):
                retailers_out.append({
                    "retailer": rk,
                    "label": label,
                    "blocked": True,
                    "reason": d.get("reason") or "blocked",
                    "strong": [],
                    "borderline": [],
                })
                continue
            classified = _classify_matches(d.get("matches") or [])
            strong = classified["strong"]
            borderline = classified["borderline"]
            if strong:
                sku_has_strong = True
                default_count += len(strong)
            retailers_out.append({
                "retailer": rk,
                "label": label,
                "blocked": False,
                "strong": strong,
                "borderline": borderline,
            })
        if not sku_has_strong:
            all_borderline_skus.append(sku)
        summary.append({"sku": sku, "retailers": retailers_out})

    return {
        "summary": summary,
        "all_borderline_skus": all_borderline_skus,
        "default_variant_count": default_count,
    }


def _select_variants(
    discoveries: list[dict[str, Any]],
    user_choice: str,
    custom: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Apply the user's choice to discovery results → variant list for
    the deep-scrape tool. Each variant: {sku, retailer, variant_title,
    variant_url}.
    """
    if user_choice == "custom":
        return [
            {
                "sku": (v.get("sku") or "").strip(),
                "retailer": (v.get("retailer") or "").strip(),
                "variant_title": (v.get("variant_title") or "").strip(),
                "variant_url": (v.get("variant_url") or "").strip() or None,
            }
            for v in (custom or [])
            if v.get("sku") and v.get("retailer") and v.get("variant_title")
        ]

    out: list[dict[str, Any]] = []
    for d in discoveries:
        if d.get("blocked"):
            continue
        sku = d.get("sku") or ""
        rk = d.get("retailer") or ""
        matches = d.get("matches") or []
        classified = _classify_matches(matches)
        strong = classified["strong"]
        borderline = classified["borderline"]

        if user_choice == "proceed":
            picks = strong
        elif user_choice == "include_borderline":
            picks = strong + borderline
        elif user_choice == "top_only":
            ranked = sorted(
                strong + borderline,
                key=lambda m: -(m.get("match_pct") or 0),
            )
            picks = ranked[:1]
        else:
            picks = strong  # safe default

        for m in picks:
            title = (m.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "sku": sku,
                "retailer": rk,
                "variant_title": title,
                "variant_url": (m.get("url") or "").strip() or None,
            })
    return out


def _normalize_skus(raw: Any) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for s in raw:
        if isinstance(s, str):
            v = s.strip()
            if v:
                out.append(v)
    return out


def _normalize_retailers(raw: Any) -> list[str]:
    if not raw:
        return list(_DEFAULT_RETAILERS)
    out: list[str] = []
    for r in raw:
        if isinstance(r, str):
            r = r.strip().lower()
            if r in _RETAILER_LABELS and r not in out:
                out.append(r)
    return out or list(_DEFAULT_RETAILERS)


# ---------------------------------------------------------------------------
# Stage handlers
# ---------------------------------------------------------------------------


def _run_discovery(
    state: dict[str, Any], on_progress=None
) -> tuple[list[dict[str, Any]], str | None]:
    """Invoke the discovery helper. Returns (discoveries, error_msg)."""
    try:
        envelope = _discover.handle(
            {
                "skus": state["skus"],
                "retailers": state["retailers"],
                "headless": state.get("headless", False),
            },
            on_progress=on_progress,
        )
        payload = _unwrap_envelope(envelope)
        return payload.get("discoveries") or [], None
    except Exception as ex:
        logger.exception("shelf_watch_run: discovery failed")
        return [], str(ex)


def _run_compare_and_report(
    state: dict[str, Any],
    variants: list[dict[str, Any]],
    on_progress=None,
) -> tuple[dict[str, Any], str | None]:
    """Run deep-scrape + report build. Returns (complete_envelope, error)."""
    try:
        compare_env = _compare.handle(
            {
                "variants": variants,
                "headless": state.get("headless", False),
            },
            on_progress=on_progress,
        )
        compare_payload = _unwrap_envelope(compare_env)
        rows = compare_payload.get("rows") or []
        if not rows:
            return {}, "deep-scrape returned no rows"

        report_env = _report.handle(
            {"rows": rows},
            on_progress=on_progress,
        )
        report_payload = _unwrap_envelope(report_env)

        rows_blocked = sum(1 for r in rows if r.get("blocked"))
        return {
            "stage": "complete",
            "report_markdown": report_payload.get("markdown") or "",
            "previous_run_timestamp": report_payload.get("previous_run_timestamp"),
            "rows_captured": len(rows) - rows_blocked,
            "rows_blocked": rows_blocked,
        }, None
    except Exception as ex:
        logger.exception("shelf_watch_run: compare/report failed")
        return {}, str(ex)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    state = _session.load()
    user_choice = arguments.get("user_choice")
    skus_arg = _normalize_skus(arguments.get("skus"))
    retailers_arg = _normalize_retailers(arguments.get("retailers"))
    headless_arg = bool(arguments.get("headless", False))
    skip_plausibility = bool(arguments.get("skip_plausibility_check", False))
    custom_variants = arguments.get("selected_variants") or []

    # Cancel from any stage.
    if user_choice == "cancel":
        _session.clear()
        return ok(
            "shelf_watch_run",
            json.dumps({"stage": "cancelled"}, ensure_ascii=False),
        )

    # ------------------------------------------------------------------
    # CONTINUATION — saved session present
    # ------------------------------------------------------------------
    if state.get("stage"):
        stage = state["stage"]

        if stage == "awaiting_plausibility":
            if user_choice == "accept_suggestions":
                # Agent should have re-passed corrected SKUs.
                if not skus_arg:
                    # Fall back to suggested values from concerns.
                    skus_arg = [
                        c.get("suggested") or c.get("sku")
                        for c in state.get("concerns", [])
                        if c.get("suggested") or c.get("sku")
                    ]
                state["skus"] = skus_arg or state.get("skus", [])
            elif user_choice == "keep_original":
                pass  # state["skus"] already holds the originals
            else:
                return error(
                    "shelf_watch_run",
                    "input",
                    "Plausibility confirmation needs `user_choice` of "
                    "`accept_suggestions`, `keep_original`, or `cancel`.",
                )
            # Continue to discovery.
            state.pop("concerns", None)
            return _continue_after_plausibility(state, on_progress)

        if stage == "awaiting_disambiguation":
            if user_choice not in {"proceed", "include_borderline", "top_only", "custom"}:
                return error(
                    "shelf_watch_run",
                    "input",
                    "Disambiguation needs `user_choice` of `proceed`, "
                    "`include_borderline`, `top_only`, `custom`, or `cancel`.",
                )
            variants = _select_variants(
                state.get("discoveries", []), user_choice, custom_variants
            )
            if not variants:
                _session.clear()
                return error(
                    "shelf_watch_run",
                    "input",
                    "No variants selected — nothing to scrape.",
                )
            envelope, err = _run_compare_and_report(state, variants, on_progress)
            if err:
                _session.clear()
                return error("shelf_watch_run", "external", err)
            _session.clear()
            return ok(
                "shelf_watch_run",
                json.dumps(envelope, ensure_ascii=False),
            )

        # Unknown stage → reset.
        logger.warning("shelf_watch_run: unknown saved stage %r — resetting", stage)
        _session.clear()
        state = {}

    # ------------------------------------------------------------------
    # FIRST CALL — no saved session
    # ------------------------------------------------------------------
    skus = skus_arg or list(_DEFAULT_SKUS)
    state = {
        "skus": skus,
        "retailers": retailers_arg,
        "headless": headless_arg,
    }

    # Optional plausibility pre-flight.
    if not skip_plausibility:
        if on_progress:
            on_progress("step", f"Checking plausibility of {len(skus)} SKU(s)\u2026")
        concerns = _check_plausibility(skus, on_progress=on_progress)
        if concerns:
            state["stage"] = "awaiting_plausibility"
            state["concerns"] = concerns
            _session.save(state)
            return ok(
                "shelf_watch_run",
                json.dumps({
                    "stage": "needs_plausibility_confirmation",
                    "concerns": concerns,
                    "options": ["accept_suggestions", "keep_original", "cancel"],
                    "prompt_hint": (
                        "Show each concern and its suggested correction. "
                        "Ask the user whether to use the suggestions, keep "
                        "their originals, or cancel. Translate the reply "
                        "into `user_choice` and re-call. When the user "
                        "accepts suggestions, also pass the corrected "
                        "list as `skus`."
                    ),
                }, ensure_ascii=False),
            )

    return _continue_after_plausibility(state, on_progress)


def _continue_after_plausibility(state: dict[str, Any], on_progress) -> str:
    """Run discovery, then either dispatch to disambiguation or
    deep-scrape directly when no ambiguity remains.
    """
    if on_progress:
        on_progress(
            "milestone",
            f"Starting discovery sweep \u2014 {len(state['skus'])} SKU(s) "
            f"\u00d7 {len(state['retailers'])} retailer(s)",
        )

    discoveries, err = _run_discovery(state, on_progress)
    if err:
        _session.clear()
        return error("shelf_watch_run", "external", f"discovery failed: {err}")

    state["discoveries"] = discoveries
    summary = _build_summary(discoveries)

    # Decide: do we have a clean strong-match path, or do we need
    # to involve the user?
    needs_user = False
    for sku_block in summary["summary"]:
        for r in sku_block["retailers"]:
            if r.get("blocked"):
                continue
            n_strong = len(r.get("strong") or [])
            n_borderline = len(r.get("borderline") or [])
            # User input needed when:
            #   - >1 strong match on any retailer (real ambiguity), OR
            #   - 0 strong matches but borderline candidates exist.
            if n_strong > 1 or (n_strong == 0 and n_borderline > 0):
                needs_user = True
                break
        if needs_user:
            break

    if not needs_user and summary["default_variant_count"] == 0:
        # Nothing to scrape at all.
        _session.clear()
        return ok(
            "shelf_watch_run",
            json.dumps({
                "stage": "complete",
                "report_markdown": (
                    "# Shelf Watch\n\n"
                    "_No matching products found on any retailer for the "
                    "requested SKUs. Try a different spelling or a more "
                    "specific model number._\n"
                ),
                "previous_run_timestamp": None,
                "rows_captured": 0,
                "rows_blocked": 0,
            }, ensure_ascii=False),
        )

    if needs_user:
        state["stage"] = "awaiting_disambiguation"
        _session.save(state)
        return ok(
            "shelf_watch_run",
            json.dumps({
                "stage": "needs_disambiguation",
                "summary": summary["summary"],
                "all_borderline_skus": summary["all_borderline_skus"],
                "default_variant_count": summary["default_variant_count"],
                "options": [
                    "proceed",
                    "include_borderline",
                    "top_only",
                    "custom",
                    "cancel",
                ],
                "prompt_hint": (
                    "Render the summary as markdown grouped by SKU. For "
                    "each retailer show its strong matches first, then "
                    "borderline (call out that borderline are skipped by "
                    "default). Mention any SKUs in `all_borderline_skus` "
                    "as 'no strong match — likely the SKU as written "
                    "doesn't exist on these sites'. Ask the user to "
                    "reply: `go` (proceed = strong only), `include "
                    "borderline`, `top only`, or hand-pick. Translate "
                    "the reply into `user_choice`. For custom picks, "
                    "build `selected_variants` from the summary entries."
                ),
            }, ensure_ascii=False),
        )

    # No ambiguity — go straight to deep scrape.
    variants = _select_variants(discoveries, "proceed", None)
    envelope, err = _run_compare_and_report(state, variants, on_progress)
    _session.clear()
    if err:
        return error("shelf_watch_run", "external", err)
    return ok(
        "shelf_watch_run",
        json.dumps(envelope, ensure_ascii=False),
    )
