"""
Generic Computer-Use harness — Azure OpenAI gpt-5.4 + Playwright.

This module is **use-case-agnostic**. It owns:
  * Playwright Chromium lifecycle (headed by default).
  * The Responses-API computer-use loop (screenshot → actions → screenshot).
  * Action execution (click / type / scroll / drag / keypress / wait).
  * Domain allow-list policing and safety-check handling.

Skills consume it by calling `run_computer_use_task(...)` with three
things:
  1. `instructions`  — system prompt (skill persona + rules).
  2. `user_task`     — natural-language description of the goal.
  3. `start_url`     — first page to land on.
  4. `allow_domains` — hosts the agent is permitted to visit.

The model decides every click, scroll, and keystroke. This file never
embeds any retailer- or task-specific knowledge.

Auth + model selection reuse `agent_core.get_responses_client()` and
`agent_core.CHAT_MODEL`, so the WAM / token-refresh path applies for
free and there is exactly one place to change the model.

Reference: https://learn.microsoft.com/en-in/azure/foundry-classic/openai/how-to/computer-use
           https://developers.openai.com/api/docs/guides/tools-computer-use
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PWTimeout

logger = logging.getLogger("hub_se_agent")


# Recommended display resolution per OpenAI / Azure docs (best click accuracy).
DISPLAY_WIDTH = 1440
DISPLAY_HEIGHT = 900


# Maps the model's key names to Playwright's. Lifted from the MS sample;
# extend as needed without touching callers.
KEY_MAPPING: dict[str, str] = {
    "/": "Slash", "\\": "Backslash",
    "alt": "Alt", "option": "Alt",
    "arrowdown": "ArrowDown", "down": "ArrowDown",
    "arrowleft": "ArrowLeft", "left": "ArrowLeft",
    "arrowright": "ArrowRight", "right": "ArrowRight",
    "arrowup": "ArrowUp", "up": "ArrowUp",
    "backspace": "Backspace",
    "ctrl": "Control", "control": "Control",
    "cmd": "Meta", "command": "Meta", "meta": "Meta", "win": "Meta", "super": "Meta",
    "delete": "Delete",
    "enter": "Enter", "return": "Return",
    "esc": "Escape", "escape": "Escape",
    "shift": "Shift",
    "space": " ",
    "tab": "Tab",
    "pagedown": "PageDown", "pageup": "PageUp",
    "home": "Home", "end": "End",
    "insert": "Insert",
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
    "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8",
    "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
}


@dataclass
class ComputerUseResult:
    """What the harness returns after a single task run."""

    final_text: str                       # last assistant message (model's answer)
    iterations: int                       # how many computer_call turns ran
    screenshots: list[Path] = field(default_factory=list)
    blocked: bool = False                 # captcha / 403 / safety-check abort
    block_reason: str | None = None
    visited_urls: list[str] = field(default_factory=list)


def _validate_xy(x: int | None, y: int | None) -> tuple[int, int]:
    """Clamp coordinates to viewport so a misaimed click can't escape."""
    return (
        max(0, min(int(x or 0), DISPLAY_WIDTH)),
        max(0, min(int(y or 0), DISPLAY_HEIGHT)),
    )


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_allowed(host: str, allow_domains: list[str]) -> bool:
    if not host:
        return False
    host = host.lower()
    for allowed in allow_domains:
        a = allowed.lower().lstrip(".")
        if host == a or host.endswith("." + a):
            return True
    return False


async def _handle_action(page: Any, action: dict[str, Any]) -> None:
    """Execute one model-emitted UI action against the live Playwright page."""
    action_type = action.get("type")

    if action_type == "click":
        button = action.get("button", "left")
        x, y = _validate_xy(action.get("x"), action.get("y"))
        if button == "back":
            await page.go_back()
        elif button == "forward":
            await page.go_forward()
        elif button == "wheel":
            await page.mouse.wheel(x, y)
        else:
            btn = {"left": "left", "right": "right", "middle": "middle"}.get(button, "left")
            await page.mouse.click(x, y, button=btn)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except PWTimeout:
                pass

    elif action_type == "double_click":
        x, y = _validate_xy(action.get("x"), action.get("y"))
        await page.mouse.dblclick(x, y)

    elif action_type == "drag":
        path = action.get("path", [])
        if len(path) < 2:
            return
        sx, sy = _validate_xy(path[0].get("x"), path[0].get("y"))
        await page.mouse.move(sx, sy)
        await page.mouse.down()
        for pt in path[1:]:
            px, py = _validate_xy(pt.get("x"), pt.get("y"))
            await page.mouse.move(px, py)
        await page.mouse.up()

    elif action_type == "move":
        x, y = _validate_xy(action.get("x"), action.get("y"))
        await page.mouse.move(x, y)

    elif action_type == "scroll":
        scroll_x = int(action.get("scroll_x", 0))
        scroll_y = int(action.get("scroll_y", 0))
        x, y = _validate_xy(action.get("x"), action.get("y"))
        await page.mouse.move(x, y)
        await page.evaluate(
            f"window.scrollBy({{left: {scroll_x}, top: {scroll_y}, behavior: 'smooth'}});"
        )

    elif action_type == "keypress":
        keys = action.get("keys", [])
        mapped = [KEY_MAPPING.get(str(k).lower(), str(k)) for k in keys]
        if len(mapped) > 1:
            for k in mapped:
                await page.keyboard.down(k)
            await asyncio.sleep(0.1)
            for k in reversed(mapped):
                await page.keyboard.up(k)
        else:
            for k in mapped:
                await page.keyboard.press(k)

    elif action_type == "type":
        await page.keyboard.type(action.get("text", ""), delay=20)

    elif action_type == "wait":
        ms = int(action.get("ms", 1000))
        await asyncio.sleep(ms / 1000)

    elif action_type == "screenshot":
        # No-op — the loop captures a screenshot after every action batch.
        pass

    else:
        logger.warning("computer_use: unknown action type=%s", action_type)


async def _wait_for_render(page: Any, *, timeout_ms: int = 6000) -> None:
    """Best-effort wait for the page to settle before screenshotting.

    Modern retail / SaaS pages are SPAs: `domcontentloaded` fires long
    before prices, images, and primary copy hydrate via XHR. Capturing
    a screenshot immediately leaves the model staring at skeletons or
    spinners and it reports "no data".

    Strategy (all soft — timeouts are swallowed):
      1. Wait for `networkidle` (500 ms of no in-flight requests).
      2. Tiny final settle delay so late paints land.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWTimeout:
        pass
    except Exception:
        pass
    await asyncio.sleep(0.4)


async def _take_screenshot(page: Any, save_to: Path | None = None) -> str:
    """Capture viewport, return base64. Optionally persist to disk.

    Callers should `await _wait_for_render(page)` first when they need
    the page to be fully hydrated (post-navigation, post-click).
    """
    png = await page.screenshot(full_page=False)
    if save_to is not None:
        try:
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_bytes(png)
        except Exception as ex:
            logger.warning("computer_use: screenshot save failed (%s)", ex)
    return base64.b64encode(png).decode("ascii")


async def _run_async(
    *,
    instructions: str,
    user_task: str,
    start_url: str,
    allow_domains: list[str],
    max_iterations: int,
    headless: bool,
    locale: str | None,
    timezone_id: str | None,
    screenshot_dir: Path | None,
    on_progress: Callable[[str, str], None] | None,
) -> ComputerUseResult:
    """Async core. Public entry point is the sync `run_computer_use_task`."""
    from playwright.async_api import async_playwright

    # Lazy import: avoids circular dep at module load (agent_core imports
    # tools, tools may eventually import this).
    from hub_cowork.core.agent_core import get_responses_client, CHAT_MODEL

    def _progress(kind: str, msg: str) -> None:
        if on_progress:
            try:
                on_progress(kind, msg)
            except Exception:
                logger.debug("computer_use: on_progress raised", exc_info=True)

    client = get_responses_client()
    model = CHAT_MODEL
    visited: list[str] = []
    screenshots: list[Path] = []

    _progress("progress", f"Launching Chromium ({'headless' if headless else 'headed'})…")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                f"--window-size={DISPLAY_WIDTH},{DISPLAY_HEIGHT}",
                "--disable-extensions",
                # Suppress Chromium-level permission/notification prompts that
                # are not part of the page DOM and therefore cannot be
                # dismissed by the model with a click. Sites like Croma and
                # Reliance Digital trigger geolocation/notification prompts
                # on first load, which would otherwise occlude the PDP in
                # every screenshot.
                "--disable-notifications",
                "--deny-permission-prompts",
            ],
        )
        try:
            # Build context kwargs without locale/timezone keys when the
            # caller didn't specify them — that way we inherit Chromium's
            # defaults instead of forcing a region the skill doesn't care
            # about. Permission suppression below stays unconditional
            # because it is a usability fix that benefits every CUA task.
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": DISPLAY_WIDTH, "height": DISPLAY_HEIGHT},
                "accept_downloads": False,
                "permissions": [],
            }
            if locale:
                context_kwargs["locale"] = locale
            if timezone_id:
                context_kwargs["timezone_id"] = timezone_id
            context = await browser.new_context(**context_kwargs)
            # Belt-and-suspenders: even if a future Chromium build changes
            # the launch-flag behaviour, this explicit grant of an empty
            # permission set keeps prompts suppressed.
            try:
                await context.grant_permissions([])
            except Exception:
                pass
            page = await context.new_page()

            _progress("progress", f"Navigating to {start_url}")
            await page.goto(start_url, wait_until="domcontentloaded")
            await _wait_for_render(page, timeout_ms=10000)
            visited.append(page.url)

            # Initial request: send screenshot + task in one user turn.
            shot_path = (screenshot_dir / "step-000.png") if screenshot_dir else None
            shot_b64 = await _take_screenshot(page, shot_path)
            if shot_path:
                screenshots.append(shot_path)

            response = client.responses.create(
                model=model,
                tools=[{"type": "computer"}],
                instructions=instructions,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": user_task},
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{shot_b64}",
                            "detail": "high",
                        },
                    ],
                }],
                reasoning={"summary": "concise"},
            )

            iterations = 0
            blocked = False
            block_reason: str | None = None
            final_text = ""

            for step in range(1, max_iterations + 1):
                if not response.output:
                    logger.info("computer_use[step=%d]: empty response.output — exiting loop", step)
                    break
                iterations = step

                # Diagnostic: what did the model emit this turn?
                output_types = [getattr(it, "type", "?") for it in response.output]
                logger.info(
                    "computer_use[step=%d] url=%s output=%s",
                    step, page.url, output_types,
                )

                # Capture any free-text output as the running "final answer".
                for item in response.output:
                    item_type = getattr(item, "type", None)
                    if item_type in ("message", "text"):
                        try:
                            content = getattr(item, "content", None)
                            if isinstance(content, list):
                                for c in content:
                                    txt = getattr(c, "text", None)
                                    if txt:
                                        final_text = txt
                            elif isinstance(content, str) and content:
                                final_text = content
                            else:
                                txt = getattr(item, "text", None)
                                if txt:
                                    final_text = txt
                        except Exception:
                            pass

                computer_calls = [
                    item for item in response.output
                    if getattr(item, "type", None) == "computer_call"
                ]
                if not computer_calls:
                    # Model is done — no more UI actions requested.
                    logger.info(
                        "computer_use[step=%d]: no more computer_calls — done. final_text len=%d preview=%r",
                        step, len(final_text or ""), (final_text or "")[:300],
                    )
                    break

                call = computer_calls[0]
                call_id = call.call_id
                actions = list(call.actions or [])

                # Safety-check handling: auto-ack only the benign one
                # (irrelevant_domain on a host that *is* on our allow list).
                acknowledged: list[Any] = []
                pending = list(getattr(call, "pending_safety_checks", None) or [])
                if pending:
                    current_host = _host_of(page.url)
                    for chk in pending:
                        code = getattr(chk, "code", "")
                        if code == "irrelevant_domain" and _is_allowed(current_host, allow_domains):
                            acknowledged.append(chk)
                            logger.info("computer_use: auto-ack irrelevant_domain on %s", current_host)
                        else:
                            blocked = True
                            block_reason = f"safety_check:{code}"
                            _progress("progress", f"⚠ Safety check `{code}` — aborting task")
                            break
                    if blocked:
                        break

                # Execute the action batch.
                _progress("progress", f"Step {step}: executing {len(actions)} action(s)")
                try:
                    await page.bring_to_front()
                    for action in actions:
                        # `actions` items are pydantic models on the new SDK
                        # but plain dicts on older ones. Normalize.
                        if not isinstance(action, dict):
                            try:
                                action = action.model_dump()  # pydantic v2
                            except Exception:
                                action = dict(action)         # best-effort

                        await _handle_action(page, action)

                        # Domain allow-list guard (soft): if the latest
                        # navigation hopped off-list, navigate back to the
                        # last allowed URL and let the model see the bounce.
                        host = _host_of(page.url)
                        if host and not _is_allowed(host, allow_domains):
                            logger.warning(
                                "computer_use: off-allowlist nav to %s — going back", host
                            )
                            try:
                                await page.go_back(wait_until="domcontentloaded", timeout=5000)
                            except Exception:
                                if visited:
                                    await page.goto(visited[-1], wait_until="domcontentloaded")

                        # Detect new tabs after clicks (links with target=_blank).
                        if action.get("type") == "click":
                            await asyncio.sleep(random.uniform(1.0, 2.5))
                            pages = page.context.pages
                            if len(pages) > 1:
                                newest = pages[-1]
                                if newest is not page and newest.url not in ("about:blank", ""):
                                    if _is_allowed(_host_of(newest.url), allow_domains):
                                        page = newest
                                    else:
                                        await newest.close()
                        elif action.get("type") != "wait":
                            await asyncio.sleep(random.uniform(0.4, 1.0))

                except Exception as ex:
                    logger.exception("computer_use: action execution failed")
                    block_reason = f"action_error: {ex}"
                    blocked = True
                    break

                # Detect anti-bot interstitials and bail soft.
                low_url = page.url.lower()
                if any(tok in low_url for tok in ("/captcha", "/challenge", "/_Incapsula_Resource", "/_sec/")):
                    blocked = True
                    block_reason = f"interstitial: {page.url}"
                    _progress("progress", f"⚠ Bot challenge detected at {page.url} — aborting")
                    break

                if page.url not in visited:
                    visited.append(page.url)

                # Wait for SPA content (XHR-loaded prices, images, copy)
                # to render before screenshotting. Without this, the model
                # sees skeletons/spinners and reports "no data".
                await _wait_for_render(page, timeout_ms=6000)

                # Capture next screenshot, send back as computer_call_output.
                shot_path = (screenshot_dir / f"step-{step:03d}.png") if screenshot_dir else None
                shot_b64 = await _take_screenshot(page, shot_path)
                if shot_path:
                    screenshots.append(shot_path)

                input_item: dict[str, Any] = {
                    "type": "computer_call_output",
                    "call_id": call_id,
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": f"data:image/png;base64,{shot_b64}",
                        "detail": "high",
                    },
                }
                if acknowledged:
                    input_item["acknowledged_safety_checks"] = [
                        {"id": c.id, "code": c.code, "message": c.message}
                        for c in acknowledged
                    ]

                try:
                    response = client.responses.create(
                        model=model,
                        previous_response_id=response.id,
                        tools=[{"type": "computer"}],
                        input=[input_item],
                    )
                except Exception as ex:
                    logger.exception("computer_use: Responses API call failed")
                    block_reason = f"api_error: {ex}"
                    blocked = True
                    break

            if iterations >= max_iterations:
                _progress("progress", f"Reached max iterations ({max_iterations})")

            logger.info(
                "computer_use: task complete iters=%d blocked=%s reason=%s final_text=%r",
                iterations, blocked, block_reason, (final_text or "")[:500],
            )

            return ComputerUseResult(
                final_text=final_text or "",
                iterations=iterations,
                screenshots=screenshots,
                blocked=blocked,
                block_reason=block_reason,
                visited_urls=visited,
            )

        finally:
            try:
                await context.close()
            except Exception:
                pass
            await browser.close()


def run_computer_use_task(
    *,
    instructions: str,
    user_task: str,
    start_url: str,
    allow_domains: list[str],
    max_iterations: int = 25,
    headless: bool = False,
    locale: str | None = None,
    timezone_id: str | None = None,
    screenshot_dir: Path | None = None,
    on_progress: Callable[[str, str], None] | None = None,
) -> ComputerUseResult:
    """
    Run a single computer-use task in a fresh Chromium session.

    Sync wrapper around the async core so tool handlers (which run on
    sync worker threads under `ExecutorPool`) can call this directly.
    A fresh asyncio loop is created per call — Playwright requires it.

    Args:
        instructions:    System prompt (skill persona + rules + JSON output
                         contract if any). Sent as the Responses-API
                         `instructions` parameter.
        user_task:       Natural-language goal for this run.
        start_url:       First URL to navigate to. Must be on `allow_domains`.
        allow_domains:   Hostnames the agent may visit. Off-list navigations
                         are softly bounced (go-back). Subdomains allowed.
        max_iterations:  Cap on computer_call turns. Default 25.
        headless:        False (default) launches a visible Chromium window.
                         True is supported but vanilla headless will likely
                         be flagged by retailer bot defenses — pair with
                         stealth patches before relying on it.
        locale:          Optional BCP-47 locale (e.g. "en-IN", "en-US",
                         "de-DE") set on the browser context. Pass when
                         the target sites render currency / formatting
                         per-region. None inherits Chromium's defaults.
        timezone_id:     Optional IANA timezone (e.g. "Asia/Kolkata",
                         "America/Los_Angeles"). Pass alongside `locale`
                         to give sites a consistent regional fingerprint.
        screenshot_dir:  If provided, every screenshot is written here as
                         step-NNN.png for debugging / audit.
        on_progress:     Optional `(kind, message)` callback, forwarded
                         to the executor's progress channel.

    Returns:
        `ComputerUseResult`. `blocked=True` when a safety check fired,
        an interstitial was detected, or an action / API call failed.
    """
    return asyncio.run(_run_async(
        instructions=instructions,
        user_task=user_task,
        start_url=start_url,
        allow_domains=allow_domains,
        max_iterations=max_iterations,
        headless=headless,
        locale=locale,
        timezone_id=timezone_id,
        screenshot_dir=screenshot_dir,
        on_progress=on_progress,
    ))
