"""
Hub Cowork — Single-process launcher.

Runs completely invisibly:
  • WebSocket server runs in a background thread
  • pywebview window starts HIDDEN
  • System tray icon: left-click to show/hide, right-click for menu
  • Toast notifications appear regardless of UI visibility
  • No console window until you summon it via tray icon or toast click

This is the "hub-cowork" fork of the original hub-se-agent. Every
user-facing identifier (window title, tray class, AppUserModelID, toast
app_id, data directory) is distinct from the upstream project so both can
run side-by-side on the same Windows session without collisions.

Launch:  python -m hub_cowork                  (entry point)
   or:   pythonw -m hub_cowork                 (invisible, no console)
"""

import asyncio
import ctypes
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import websockets
from websockets.asyncio.server import serve
import webview

# ---------------------------------------------------------------------------
# Logging — file + (optionally) console
# ---------------------------------------------------------------------------

LOG_DIR = Path.home()  # placeholder, replaced below from app_paths
from hub_cowork.core.app_paths import (
    APP_HOME as LOG_DIR,
    LOG_FILE,
    APP_DISPLAY_NAME,
    APP_USER_MODEL_ID,
    WINDOW_TITLE,
)
LOG_DIR.mkdir(exist_ok=True)
_TOAST_SCRIPT = LOG_DIR / "_toast.ps1"
_SCRIPT_DIR = Path(__file__).resolve().parent
_ASSETS_DIR = _SCRIPT_DIR.parent / "assets"  # src/hub_cowork/assets
_HTML_PATH = _ASSETS_DIR / "chat_ui.html"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("hub_se_agent")

from hub_cowork.core.agent_core import (
    run_agent, run_skill, check_azure_auth, run_az_login, reset_qa_history,
    get_loaded_skills, route, get_skill, get_credential, run_agent_on_thread,
    generate_thread_title,
)
from hub_cowork.core.outlook_helper import _resolve_organizer
from hub_cowork.core.thread_manager import (
    get_manager as get_thread_manager,
    current_thread_id as _current_thread_id,
    SYSTEM_THREAD_ID,
)
from hub_cowork.core.thread_executor import get_pool as get_thread_pool
from hub_cowork.core import hub_config

import uuid

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
HOST = "127.0.0.1"
PORT = 18080

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_clients: set = set()
_loop: asyncio.AbstractEventLoop | None = None

# ---------------------------------------------------------------------------
# WebSocket log handler — streams log lines to the UI in real-time
# ---------------------------------------------------------------------------

_LOG_RING_SIZE = 500
_log_ring: list[dict] = []          # ring buffer of recent log entries
_log_ring_lock = threading.Lock()


class _WebSocketLogHandler(logging.Handler):
    """Logging handler that broadcasts every log record to connected UI clients.

    Each entry is tagged with the originating `thread_id` (read from the
    `current_thread_id` ContextVar set by the executor worker). Entries with
    no thread context go to the `system` bucket so the UI can still show them.
    Per-thread entries are also appended to the thread's persisted `code_log`.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tid = _current_thread_id.get() or SYSTEM_THREAD_ID
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "msg": record.getMessage(),
                "thread_id": tid,
            }
            with _log_ring_lock:
                _log_ring.append(entry)
                if len(_log_ring) > _LOG_RING_SIZE:
                    del _log_ring[: len(_log_ring) - _LOG_RING_SIZE]
            # Persist per-thread (non-system).
            if tid and tid != SYSTEM_THREAD_ID:
                try:
                    get_thread_manager().append_code_log(tid, entry)
                except Exception:
                    pass
            # Broadcast to UI.
            if _loop and _clients:
                _broadcast({"type": "log_entry", "entry": entry})
        except Exception:
            pass  # never break the app because of log streaming


_ws_log_handler = _WebSocketLogHandler()
_ws_log_handler.setLevel(logging.INFO)
_ws_log_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"))
logging.getLogger("hub_se_agent").addHandler(_ws_log_handler)
_window = None  # pywebview window reference


# ---------------------------------------------------------------------------
# Toast notifications
# ---------------------------------------------------------------------------

def notify(title: str, message: str):
    """Show a native desktop notification."""
    try:
        if IS_MAC:
            safe_t = title.replace("\\", "\\\\").replace('"', '\\"')
            safe_m = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen([
                "osascript", "-e",
                f'display notification "{safe_m}" with title "{safe_t}"',
            ])
        elif IS_WIN:
            from winotify import Notification
            icon_path = _ASSETS_DIR / "agent_icon.png"
            # Note: no `launch=` URL. winotify's launch hands the URL to the
            # default browser via Windows shell, which spawns a tab even when
            # the target is our own loopback /show endpoint. To avoid that
            # browser flash on toast click, we leave clicks as a no-op and
            # rely on the tray icon to bring the window forward.
            toast = Notification(
                app_id=APP_DISPLAY_NAME,
                title=title,
                msg=message[:300],
                icon=str(icon_path) if icon_path.exists() else "",
            )
            toast.show()
        else:
            subprocess.Popen(["notify-send", title, message])
    except Exception as e:
        logger.warning("Notification failed: %s", e)


# ---------------------------------------------------------------------------
# Broadcast to connected UI (WebSocket)
# ---------------------------------------------------------------------------

def _broadcast(message: dict):
    # Hook unread events to drive the tray icon badge so the user has a
    # visual cue even when the window is hidden.
    try:
        if message.get("type") == "thread_unread":
            _bump_unread()
    except Exception:
        pass
    data = json.dumps(message)
    if _loop is None:
        return
    for ws in list(_clients):
        asyncio.run_coroutine_threadsafe(_safe_send(ws, data), _loop)


async def _safe_send(ws, data: str):
    try:
        await ws.send(data)
    except Exception:
        _clients.discard(ws)


# ---------------------------------------------------------------------------
# Local UI dispatch — threads + executor
# ---------------------------------------------------------------------------

def _ws_thread_summary(thread) -> dict:
    """Build a summary payload the UI can render in the thread list."""
    s = thread.summary()
    s["skill_name"] = thread.skill_name
    s["has_awaiting"] = (thread.status == "awaiting_user")
    return s


def _broadcast_thread_update(thread_id: str):
    """Push an updated summary of a single thread to the UI."""
    tm = get_thread_manager()
    t = tm.get(thread_id)
    if t is None:
        return
    _broadcast({"type": "thread_updated", "thread": _ws_thread_summary(t)})


def _create_new_thread(user_input: str, source: str = "ui",
                       external_user: str | None = None) -> str:
    """Create a fresh thread, route it, submit to executor. Returns thread_id."""
    tm = get_thread_manager()
    pool = get_thread_pool()

    title = user_input.strip()[:60] or "Untitled"
    thread = tm.create(title=title, source=source, external_user=external_user)
    try:
        skill_name = route(user_input)
        if skill_name and skill_name != "none":
            tm.set_skill(thread.id, skill_name)
    except Exception as e:
        logger.error("Router failed for new thread: %s", e, exc_info=True)
    request_id = uuid.uuid4().hex[:8]
    pool.submit(thread.id, user_input, request_id=request_id)
    _broadcast({"type": "thread_created", "thread": _ws_thread_summary(thread)})
    # Generate a nicer LLM-derived title in the background so the initial
    # broadcast (which uses the raw user input) returns immediately.
    def _retitle(tid: str, text: str) -> None:
        try:
            new_title = generate_thread_title(text)
            if not new_title:
                return
            tm.update_title(tid, new_title)
            t = tm.get(tid)
            if t is not None:
                _broadcast({"type": "thread_updated", "thread": _ws_thread_summary(t)})
        except Exception as e:
            logger.warning("Background title generation failed for %s: %s", tid, e)
    threading.Thread(target=_retitle, args=(thread.id, user_input), daemon=True).start()
    return thread.id


def _send_to_existing_thread(thread_id: str, user_input: str) -> bool:
    """Append a new user turn to an existing thread and dispatch.

    Returns False if the thread is unknown.
    """
    tm = get_thread_manager()
    pool = get_thread_pool()
    thread = tm.get(thread_id)
    if thread is None:
        return False
    if thread.status == "archived":
        tm.unarchive(thread_id)
    elif thread.status not in ("running", "awaiting_user"):
        tm.set_status(thread_id, "active")
    request_id = uuid.uuid4().hex[:8]
    pool.submit(thread_id, user_input, request_id=request_id)
    return True


def _relaunch_self() -> None:
    """Spawn a fresh detached `python -m hub_cowork` and exit this process.

    Used by the Settings → "Restart agent" button so env-override changes
    take effect without the user opening a terminal.
    """
    try:
        time.sleep(0.4)  # give the WS ack a moment to flush
        # On Windows, DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP keeps the
        # child alive after we exit, with no console attachment.
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            )
        subprocess.Popen(
            [sys.executable, "-m", "hub_cowork"],
            cwd=os.getcwd(),
            close_fds=True,
            creationflags=creationflags,
        )
    except Exception as e:
        logger.error("Failed to relaunch: %s", e, exc_info=True)
    finally:
        # Hard-exit — the tray, WS server, and pywebview all need to die so
        # the new process can rebind port 18080.
        os._exit(0)


def _run_system_query(request_id: str, user_input: str):
    """Cross-thread system query (e.g. 'what's running?'). Runs inline.

    Always uses the `system` pseudo-thread for logging/scoping. Never creates
    a real ConversationThread."""
    _broadcast({"type": "system_query_started", "request_id": request_id})

    def on_progress(kind: str, message: str):
        _broadcast({"type": "system_query_progress", "request_id": request_id,
                    "kind": kind, "message": message})

    # Run on an ephemeral ConversationThread so the skill executor's
    # state-mutation calls (append_message, set_skill, etc.) work, but the
    # thread is never persisted, never broadcast, and never appears in the
    # task list. The UI keeps the Q&A in a separate `state.systemMessages`
    # buffer driven by the system_query_* events.
    tm = get_thread_manager()
    ephemeral = tm.create(title="(system query)", source="system",
                          ephemeral=True)
    token = _current_thread_id.set(SYSTEM_THREAD_ID)
    try:
        try:
            result = run_agent_on_thread(ephemeral, user_input,
                                         on_progress=on_progress)
            _broadcast({"type": "system_query_complete", "request_id": request_id,
                        "result": result})
        except Exception as e:
            logger.error("System query [%s] failed: %s", request_id, e, exc_info=True)
            _broadcast({"type": "system_query_error", "request_id": request_id,
                        "error": str(e)[:500]})
    finally:
        _current_thread_id.reset(token)
        tm.dispose_ephemeral(ephemeral.id)


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def _handler(ws):
    _clients.add(ws)
    logger.info("UI connected (%d client(s))", len(_clients))
    try:
        auth_ok, _ = check_azure_auth()
        if auth_ok:
            try:
                name, email = _resolve_organizer()
                await ws.send(json.dumps({
                    "type": "auth_status", "ok": True,
                    "user": f"{name} <{email}>",
                }))
            except Exception:
                await ws.send(json.dumps({
                    "type": "auth_status", "ok": True, "user": "Authenticated",
                }))
        else:
            await ws.send(json.dumps({"type": "auth_status", "ok": False}))

        # Send loaded skills to the UI
        await ws.send(json.dumps({
            "type": "skills_list",
            "skills": get_loaded_skills(),
        }))

        # First-run / per-user config nudge — flag any required-per-user
        # env vars that are still empty so the UI can prompt the user to
        # open Settings → Configuration. Shared infra ships in
        # .env.defaults; these three intentionally do not.
        try:
            missing = [
                k for k in ("RFP_OUTPUT_FOLDER", "RFP_SHARE_RECIPIENTS")
                if not os.environ.get(k, "").strip()
            ]
            if missing:
                await ws.send(json.dumps({
                    "type": "config_warning",
                    "missing": missing,
                }))
        except Exception:
            pass

        # Send current service connectivity snapshot so the UI can paint
        # green/red dots immediately on connect (without waiting for the
        # next state change to arrive).
        try:
            from hub_cowork.core.service_status import get_monitor as _get_svc_monitor
            await ws.send(json.dumps(_get_svc_monitor().snapshot()))
        except Exception:
            pass

        # Send the current thread list so the UI can render the left pane.
        tm = get_thread_manager()
        await ws.send(json.dumps({
            "type": "threads_list",
            "threads": [_ws_thread_summary(t) for t in tm.list()],
        }))

        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get("type")

            # ── Thread lifecycle ──
            if mtype == "create_thread":
                user_input = (msg.get("input") or "").strip()
                if user_input:
                    _create_new_thread(user_input, source="ui")
            elif mtype == "send_to_thread":
                thread_id = (msg.get("thread_id") or "").strip()
                user_input = (msg.get("input") or "").strip()
                if thread_id and user_input:
                    ok = _send_to_existing_thread(thread_id, user_input)
                    if not ok:
                        await ws.send(json.dumps({
                            "type": "error",
                            "message": f"Thread {thread_id} not found",
                        }))
            elif mtype == "list_threads":
                await ws.send(json.dumps({
                    "type": "threads_list",
                    "threads": [_ws_thread_summary(t) for t in tm.list()],
                }))
            elif mtype == "get_thread":
                thread_id = (msg.get("thread_id") or "").strip()
                t = tm.get(thread_id)
                if t:
                    await ws.send(json.dumps({
                        "type": "thread_detail",
                        "thread": t.to_dict(),
                    }))
                else:
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": f"Thread {thread_id} not found",
                    }))
            elif mtype == "archive_thread":
                thread_id = (msg.get("thread_id") or "").strip()
                if tm.archive(thread_id):
                    await ws.send(json.dumps({
                        "type": "thread_archived", "thread_id": thread_id,
                    }))
            elif mtype == "cancel_thread":
                thread_id = (msg.get("thread_id") or "").strip()
                pool = get_thread_pool()
                ok = pool.cancel(thread_id)
                await ws.send(json.dumps({
                    "type": "cancel_ack",
                    "thread_id": thread_id,
                    "ok": ok,
                }))
            elif mtype == "unarchive_thread":
                thread_id = (msg.get("thread_id") or "").strip()
                if tm.unarchive(thread_id):
                    t = tm.get(thread_id)
                    if t:
                        await ws.send(json.dumps({
                            "type": "thread_unarchived",
                            "thread": _ws_thread_summary(t),
                        }))
            elif mtype == "list_archived_threads":
                await ws.send(json.dumps({
                    "type": "archived_threads_list",
                    "threads": tm.list_archived_summaries(),
                }))
            elif mtype == "delete_thread":
                thread_id = (msg.get("thread_id") or "").strip()
                if tm.delete(thread_id):
                    await ws.send(json.dumps({
                        "type": "thread_deleted", "thread_id": thread_id,
                    }))

            # ── System query (cross-thread, no persistence) ──
            elif mtype == "system_query":
                user_input = (msg.get("input") or "").strip()
                if user_input:
                    request_id = uuid.uuid4().hex[:8]
                    threading.Thread(
                        target=_run_system_query,
                        args=(request_id, user_input),
                        daemon=True,
                    ).start()

            # ── Legacy fallback (old UI builds) ──
            elif mtype == "task":
                user_input = (msg.get("input") or "").strip()
                if user_input:
                    _create_new_thread(user_input, source="ui")

            # ── Open a local file with the OS handler ──
            elif mtype == "open_file":
                raw_path = (msg.get("path") or "").strip()
                if raw_path:
                    try:
                        p = Path(raw_path)
                        if p.is_file():
                            if sys.platform == "win32":
                                os.startfile(str(p))  # noqa: PTH123
                            elif sys.platform == "darwin":
                                subprocess.Popen(["open", str(p)])
                            else:
                                subprocess.Popen(["xdg-open", str(p)])
                            logger.info("Opened file via UI request: %s", p)
                        else:
                            logger.warning("open_file: path not found: %s", raw_path)
                    except Exception as e:
                        logger.warning("open_file failed for %s: %s", raw_path, e)

            # ── Auth / config ──
            elif mtype == "signin":
                threading.Thread(target=_handle_signin, daemon=True).start()
            elif mtype == "clear_history":
                # Legacy — reset the implicit "qa" history. Individual threads
                # can be cleared via delete_thread.
                reset_qa_history()
                await ws.send(json.dumps({
                    "type": "history_cleared",
                    "message": "Legacy QA history cleared.",
                }))
            elif mtype == "get_logs":
                with _log_ring_lock:
                    snapshot = list(_log_ring)
                thread_filter = msg.get("thread_id")
                if thread_filter:
                    snapshot = [e for e in snapshot if e.get("thread_id") == thread_filter]
                await ws.send(json.dumps({
                    "type": "log_history",
                    "entries": snapshot,
                }))
            elif mtype == "get_config":
                config = hub_config.load()
                # Snapshot of currently-active env values for keys the UI knows
                # about. Lets the Settings editor pre-fill with whatever the
                # agent is actually running on (from .env / .env.defaults /
                # _env_overrides) instead of showing blanks.
                _env_prefixes = (
                    "AZURE_", "ACS_", "WORKIQ_", "FOUNDRYIQ_",
                    "FOUNDRY_", "RFP_", "GRAPH_", "AZ_REDIS_",
                    "REDIS_", "RESOURCE_",
                )
                env_current = {
                    k: v for k, v in os.environ.items()
                    if k.startswith(_env_prefixes)
                }
                config["_env_current"] = env_current
                await ws.send(json.dumps({
                    "type": "config_data",
                    "config": config,
                }))
            elif mtype == "save_config":
                try:
                    incoming = msg.get("config", {}) or {}
                    # Strip transient _env_current snapshot so it never
                    # leaks into the persisted user config file.
                    if isinstance(incoming, dict):
                        incoming.pop("_env_current", None)
                    hub_config.save(incoming)
                    await ws.send(json.dumps({
                        "type": "config_saved",
                        "ok": True,
                    }))
                except Exception as e:
                    await ws.send(json.dumps({
                        "type": "config_saved",
                        "ok": False,
                        "error": str(e),
                    }))
            elif mtype == "validate_speakers":
                req_id = (msg.get("request_id") or "").strip() or None
                names = msg.get("names") or []
                if not isinstance(names, list):
                    names = []
                # Run in a worker thread — WorkIQ CLI can take >10s and we
                # don't want to block the event loop or other clients.
                from hub_cowork.host.ui_actions import run_validate_speakers
                threading.Thread(
                    target=run_validate_speakers,
                    args=(req_id, [str(n) for n in names]),
                    kwargs={"broadcast": _broadcast},
                    daemon=True,
                ).start()
            elif mtype == "window_minimize":
                if _window is not None:
                    _window.minimize()
            elif mtype == "window_maximize":
                if _window is not None:
                    _window.maximize()
            elif mtype == "window_restore":
                if _window is not None:
                    _window.restore()
            elif mtype == "window_hide":
                _hide_window()
                if _window is not None:
                    _window._agent_hidden = True
            elif mtype == "restart":
                # Spawn a fresh process and exit. Used by Settings →
                # "Restart agent" so the user can apply env-var changes
                # without leaving the UI.
                logger.info("UI requested restart — relaunching")
                await ws.send(json.dumps({"type": "restart_ack"}))
                threading.Thread(target=_relaunch_self, daemon=True).start()
    except websockets.ConnectionClosed:
        pass
    finally:
        _clients.discard(ws)
        logger.info("UI disconnected (%d client(s))", len(_clients))


def _handle_signin():
    _broadcast({"type": "progress", "kind": "step",
                "message": "Opening browser for Azure sign-in..."})
    notify(APP_DISPLAY_NAME, "Opening browser for Azure sign-in...")
    ok, msg = run_az_login()
    _broadcast({"type": "signin_status", "ok": ok, "message": msg})
    if ok:
        logger.info("Azure sign-in succeeded: %s", msg)
        notify("Azure Sign-in", msg)
        # Update auth status for all connected clients
        try:
            name, email = _resolve_organizer()
            _broadcast({"type": "auth_status", "ok": True,
                        "user": f"{name} <{email}>"})
        except Exception:
            _broadcast({"type": "auth_status", "ok": True,
                        "user": "Authenticated"})
    else:
        logger.warning("Azure sign-in failed: %s", msg)
        notify("Azure Sign-in Failed", msg)


# ---------------------------------------------------------------------------
# WebSocket server + HTTP handler for toast clicks (runs in background thread)
# ---------------------------------------------------------------------------

HTTP_PORT = PORT + 1  # 18081

def _run_server():
    """Start the asyncio event loop + WebSocket server + HTTP server in this thread."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    async def _serve():
        async with serve(_handler, HOST, PORT):
            logger.info("WebSocket server listening on ws://%s:%d", HOST, PORT)

            # Tiny HTTP server for toast click-to-open
            from http.server import HTTPServer, BaseHTTPRequestHandler

            class _ShowHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    _show_window()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body><script>window.close()</script>Opening Hub Cowork...</body></html>")

                def log_message(self, *args):
                    pass  # suppress HTTP logs

            http_server = HTTPServer((HOST, HTTP_PORT), _ShowHandler)
            http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
            http_thread.start()
            logger.info("HTTP server for toast clicks on http://%s:%d", HOST, HTTP_PORT)

            await asyncio.Future()  # run forever

    _loop.run_until_complete(_serve())


# ---------------------------------------------------------------------------
# Window show/hide
# ---------------------------------------------------------------------------

def _fix_frameless_resize():
    """Re-add WS_SIZEBOX + WS_MAXIMIZEBOX to the frameless window.

    frameless=True sets WS_POPUP which strips the resize border. Restoring
    these two style bits gives back edge/corner drag-resize and the OS
    maximize behaviour without bringing back the native title bar.
    """
    if not IS_WIN or _window is None:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        GWL_STYLE    = -16
        WS_SIZEBOX   = 0x00040000
        WS_MAXIMIZEBOX = 0x00010000
        SWP_NOMOVE      = 0x0002
        SWP_NOSIZE      = 0x0001
        SWP_NOZORDER    = 0x0004
        SWP_FRAMECHANGED = 0x0020
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            user32.SetWindowLongW(hwnd, GWL_STYLE,
                                  style | WS_SIZEBOX | WS_MAXIMIZEBOX)
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
            logger.info("Frameless resize border restored (hwnd=%s)", hwnd)
    except Exception as e:
        logger.warning("Failed to restore frameless resize border: %s", e)


def _set_taskbar_icon():
    """Force our custom icon on the pywebview HWND so the taskbar shows it
    instead of the default pythonw.exe Python icon."""
    if not IS_WIN or _window is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        WM_SETICON = 0x0080
        ICON_BIG = 1
        ICON_SMALL = 0
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040

        ico = str(_ASSETS_DIR / "agent_icon.ico")

        # Load the .ico as big (32x32) and small (16x16) HICON handles
        hicon_big = user32.LoadImageW(
            None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE
        )
        hicon_small = user32.LoadImageW(
            None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
        )

        # Find the top-level window by title
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd and hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hwnd and hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)

        logger.info("Taskbar icon set via Win32 (hwnd=%s)", hwnd)

        # Register the HWND with the auth helper so future WAM (broker)
        # credential prompts are parented to our app window instead of
        # appearing as a free-floating dialog.
        try:
            from hub_cowork.core.auth_credential import set_parent_window_handle
            if hwnd:
                set_parent_window_handle(int(hwnd))
        except Exception as e:
            logger.debug("Could not register parent HWND for auth: %s", e)

        # Restore resize handles stripped by frameless=True
        _fix_frameless_resize()
    except Exception as e:
        logger.warning("Failed to set taskbar icon: %s", e)


def _show_window():
    """Show the pywebview window (thread-safe)."""
    if _window is not None:
        try:
            _window.show()
            _set_taskbar_icon()
        except Exception:
            pass


def _hide_window():
    """Hide the pywebview window (thread-safe)."""
    if _window is not None:
        try:
            _window.hide()
        except Exception:
            pass


def _toggle_window():
    """Toggle the pywebview window visibility."""
    if _window is None:
        return
    try:
        # pywebview doesn't have a reliable .hidden property on all
        # backends, so we track it ourselves
        if getattr(_window, '_agent_hidden', True):
            _window.show()
            _window._agent_hidden = False
            # Surfacing the window dismisses any pending tray indicator.
            _clear_unread()
        else:
            _window.hide()
            _window._agent_hidden = True
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Unread (Teams) tray-badge tracking
# ---------------------------------------------------------------------------

_unread_count = 0


def _bump_unread():
    """Increment the unread counter and refresh the tray icon badge."""
    global _unread_count
    _unread_count += 1
    if _tray:
        n = _unread_count
        tip = (f"{APP_DISPLAY_NAME} — {n} new message" + ("s" if n != 1 else ""))
        try:
            _tray.set_badge(True, tooltip=tip)
        except Exception as e:
            logger.warning("Tray badge set failed: %s", e)


def _clear_unread():
    """Reset the unread counter and remove the tray icon badge."""
    global _unread_count
    if _unread_count == 0 and _tray and not getattr(_tray, "_badge_on", False):
        return
    _unread_count = 0
    if _tray:
        try:
            _tray.set_badge(False, tooltip=APP_DISPLAY_NAME)
        except Exception as e:
            logger.warning("Tray badge clear failed: %s", e)


# ---------------------------------------------------------------------------
# System tray icon
# ---------------------------------------------------------------------------

_tray: "TrayIcon | None" = None


def _setup_tray():
    """Start the Win32 system tray icon (Windows only)."""
    global _tray
    if not IS_WIN:
        return
    try:
        from hub_cowork.host.tray_icon import TrayIcon
        icon_path = str(_ASSETS_DIR / "agent_icon.ico")
        _tray = TrayIcon(
            on_show=_toggle_window,
            on_quit=_quit_app,
            icon_path=icon_path,
            tooltip=APP_DISPLAY_NAME,
        )
        _tray.start()
        logger.info("System tray icon started")
    except Exception as e:
        logger.warning("Could not start tray icon: %s", e)


def _quit_app():
    """Cleanly shut down the agent."""
    logger.info("Quit requested from tray menu")
    if _tray:
        _tray.stop()
    if _window:
        _window.destroy()
    os._exit(0)


# ---------------------------------------------------------------------------
# pywebview lifecycle hooks
# ---------------------------------------------------------------------------

def _on_shown():
    """Called when the pywebview window is first shown."""
    global _window
    # Immediately hide — we only want the window visible on demand
    if _window is not None:
        _window.hide()
        _window._agent_hidden = True


def _on_closing():
    """Intercept window close — hide instead of quitting."""
    _hide_window()
    if _window is not None:
        _window._agent_hidden = True
    return False  # prevent actual close


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _acquire_single_instance_lock():
    """Ensure only one Hub Cowork instance runs at a time.

    Uses a named Windows mutex (visible across processes in the same session).
    If another instance is already holding the mutex, this process exits
    immediately instead of creating a second tray icon + port collision.

    The handle is intentionally kept in a module-level global so it survives
    for the lifetime of the process. Windows releases the mutex automatically
    on process exit.
    """
    if not IS_WIN:
        return  # non-Windows: no-op
    global _SINGLE_INSTANCE_HANDLE
    ERROR_ALREADY_EXISTS = 183
    # Session-scoped name (Local\) so users don't interfere with each other,
    # and so this doesn't collide with the upstream hub-se-agent fork.
    name = "Local\\HubCowork-SingleInstance-v1"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, name)
    last_err = ctypes.get_last_error()
    if not handle:
        # Couldn't create — proceed rather than block startup.
        return
    if last_err == ERROR_ALREADY_EXISTS:
        logger.warning(
            "Another Hub Cowork instance is already running — exiting this one."
        )
        # Best-effort toast so the user understands why nothing new appeared.
        try:
            notify(APP_DISPLAY_NAME, "Already running — look for the tray icon.")
        except Exception:
            pass
        kernel32.CloseHandle(handle)
        sys.exit(0)
    _SINGLE_INSTANCE_HANDLE = handle


_SINGLE_INSTANCE_HANDLE = None


# Engagement-context scratchpad files (one per customer) accumulate forever
# unless cleaned up. Anything older than this is dropped on startup.
_ENGAGEMENT_CONTEXT_TTL_DAYS = 30


def _purge_stale_engagement_context():
    """Delete engagement_context/*.json files older than the TTL.

    The 4-phase agenda workflow uses these files to pass state between
    phases. They're keyed by customer name and reused across re-runs, but
    distinct customers add new files. After the TTL the customer's
    workflow is almost certainly finished — re-running just regenerates
    the file from a fresh briefing fetch.
    """
    try:
        from hub_cowork.core.app_paths import ENGAGEMENT_CONTEXT_DIR
    except Exception:
        return
    if not ENGAGEMENT_CONTEXT_DIR.exists():
        return
    cutoff = time.time() - _ENGAGEMENT_CONTEXT_TTL_DAYS * 86400
    removed = 0
    for path in ENGAGEMENT_CONTEXT_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except Exception as e:
            logger.warning("Could not purge stale context %s: %s", path.name, e)
    if removed:
        logger.info(
            "Purged %d engagement_context file(s) older than %d days",
            removed, _ENGAGEMENT_CONTEXT_TTL_DAYS,
        )

def main():
    global _window

    # Bail out early if a prior instance is still alive.
    _acquire_single_instance_lock()

    logger.info("=" * 50)
    logger.info("%s starting (single-process mode)", APP_DISPLAY_NAME)
    logger.info("Log: %s", LOG_FILE)
    logger.info("=" * 50)

    # Housekeeping: age out stale engagement_context scratchpad files so
    # they don't accumulate forever (one per customer ever processed).
    _purge_stale_engagement_context()

    # 1. Wire the thread executor to broadcast and notify.
    pool = get_thread_pool()
    pool.configure(on_broadcast=_broadcast, on_notify=notify,
                   on_show_window=_show_window)

    # 1a. Start the service-connectivity monitor so the UI can render
    # green/red dots per external service (WorkIQ, FoundryIQ, Fabric,
    # Redis+Teams channel). Every tool envelope + the Redis bridge
    # already feed state into this monitor via service_status.mark*().
    try:
        from hub_cowork.core.service_status import get_monitor as _get_svc_monitor
        _svc_monitor = _get_svc_monitor()
        _svc_monitor.set_broadcast(_broadcast)
        _svc_monitor.start_probes()
    except Exception as e:
        logger.warning("Service status monitor failed to start: %s", e)

    # Forward thread state changes from ThreadManager to the UI.
    tm = get_thread_manager()
    def _on_thread_event(event: str, thread_id: str, payload: dict):
        # ThreadManager emits (event, thread_id, payload). We only need to
        # push a fresh summary for events that actually mutate the thread
        # list (status, title, skill). Message/progress appenders already
        # broadcast their own dedicated messages elsewhere, so we skip them
        # here to avoid duplicating traffic.
        if event in ("thread_message", "thread_progress"):
            return
        t = tm.get(thread_id)
        if t is None:
            return
        _broadcast({
            "type": "thread_updated",
            "event": event,
            "thread": _ws_thread_summary(t),
        })
    tm.add_observer(_on_thread_event)

    # 1b. Start Redis bridge if configured (optional — remote task delivery)
    _redis_bridge = None
    redis_endpoint = os.environ.get("AZ_REDIS_CACHE_ENDPOINT")
    if redis_endpoint:
        try:
            from hub_cowork.host.redis_bridge import RedisBridge
            name, email = _resolve_organizer()
            ttl = int(os.environ.get("REDIS_SESSION_TTL_SECONDS", "86400"))
            namespace = os.environ.get("REDIS_NAMESPACE")
            _redis_bridge = RedisBridge(
                user_email=email,
                user_name=name,
                endpoint=redis_endpoint,
                credential=get_credential(),
                ttl=ttl,
                namespace=namespace,
            )
            # Wire the executor's per-thread completion callback to relay
            # remote-sourced replies back through Redis.
            pool.configure(on_thread_reply=_redis_bridge.on_thread_reply)
            _redis_bridge.start(on_broadcast=_broadcast)
        except Exception as e:
            logger.warning("Redis bridge failed to start: %s — running in local-only mode", e)
    else:
        logger.info("Redis bridge disabled — AZ_REDIS_CACHE_ENDPOINT not set")
        try:
            from hub_cowork.core.service_status import get_monitor as _get_svc_monitor
            _get_svc_monitor().mark("redis_teams", "unconfigured",
                                    "AZ_REDIS_CACHE_ENDPOINT not set")
        except Exception:
            pass

    # 2. Start WebSocket server in a background thread
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    # 3. Start system tray icon (left-click to show/hide, right-click for menu)
    _setup_tray()

    # 4. (No startup toast — the tray icon is the "I'm running" signal.
    #     Toasts are reserved for things the user needs to react to:
    #     HITL awaiting-confirmation and hard errors.)

    # 5. Tell Windows this is a distinct app (not generic pythonw.exe)
    #    so the taskbar shows our custom icon instead of the Python icon,
    #    AND so Windows groups our taskbar entry independently of any other
    #    fork running on the same session (e.g. the upstream hub-se-agent).
    if IS_WIN:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )

    # 6. Disable Chromium GPU acceleration to reduce memory and heat.
    #    WebView2 respects this env var for additional Chromium flags.
    if IS_WIN:
        os.environ.setdefault(
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
            "--disable-gpu --disable-gpu-compositing",
        )

    # 7. Create the pywebview window (starts hidden)
    _window = webview.create_window(
        title=WINDOW_TITLE,
        url=str(_HTML_PATH),
        width=1200,
        height=720,
        resizable=True,
        text_select=True,
        on_top=False,
        hidden=True,
        frameless=True,
    )
    _window._agent_hidden = True
    _window.events.closing += _on_closing

    def _on_shown():
        _set_taskbar_icon()

    _window.events.shown += _on_shown

    # 8. Start pywebview event loop (blocks until process exits)
    _icon_path = _ASSETS_DIR / "agent_icon.ico"
    webview.start(debug=False, private_mode=True,
                  icon=str(_icon_path) if _icon_path.exists() else None)


if __name__ == "__main__":
    main()

