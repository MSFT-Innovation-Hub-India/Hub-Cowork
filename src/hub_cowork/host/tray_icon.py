"""
System tray icon using raw Win32 APIs (ctypes).

Runs its own message pump in a background thread so it works alongside
pywebview without conflicts. No extra dependencies required.

This module is branded for the **Hub Cowork** fork. The window class name
and default tooltip are intentionally distinct from the original
`hub-se-agent` project so both trays can run side-by-side without the
second `RegisterClassW` call failing because the class is already
registered by the first process on the same user session.

Left-click  → show/hide the chat window
Right-click → context menu (Show / Quit)
"""

import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("hub_se_agent")

# Win32 constants
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 1
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_ICON = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_TIP = 0x00000004

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0008

IDM_SHOW = 1001
IDM_QUIT = 1002

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """Full Vista+ layout — cbSize must match what the OS expects."""
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),       # union with uTimeout
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class TrayIcon:
    """Win32 system-tray icon with its own message-pump thread."""

    def __init__(self, *, on_show, on_quit, icon_path: str | None = None,
                 tooltip: str = "Hub Cowork"):
        self._on_show = on_show
        self._on_quit = on_quit
        self._icon_path = icon_path
        self._tooltip = tooltip
        self._hwnd = None
        self._thread: threading.Thread | None = None
        # prevent garbage collection of the C callback
        self._wndproc_ref = WNDPROC(self._wndproc)

    # ------------------------------------------------------------------

    def start(self):
        """Start the tray icon in a background thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="tray-icon"
        )
        self._thread.start()

    def stop(self):
        """Remove the tray icon and close the hidden window."""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)

    # ------------------------------------------------------------------

    def _run(self):
        """Create hidden window, add tray icon, pump messages."""
        try:
            self._run_inner()
        except Exception as e:
            logger.error("Tray icon thread crashed: %s", e, exc_info=True)

    def _run_inner(self):
        hinstance = kernel32.GetModuleHandleW(None)

        # Register window class — name must be unique per-user across any
        # other tray app that might also be running (e.g. the original
        # hub-se-agent). Both forks therefore use distinct class names.
        class_name = "HubCoworkTrayClass"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        logger.info("Tray: RegisterClassW returned atom=%s", atom)

        # Create hidden message-only window
        self._hwnd = user32.CreateWindowExW(
            0, class_name, "Hub Cowork Tray", 0,
            0, 0, 0, 0,
            None, None, hinstance, None,
        )
        logger.info("Tray: CreateWindowExW hwnd=%s", self._hwnd)

        if not self._hwnd:
            logger.error("Tray: CreateWindowExW failed")
            return

        # Load icon
        hicon = None
        if self._icon_path and os.path.exists(self._icon_path):
            hicon = user32.LoadImageW(
                None, self._icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE
            )
        if not hicon:
            hicon = user32.LoadIconW(None, ctypes.cast(32512, wt.LPCWSTR))  # IDI_APPLICATION

        # Add tray icon
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = hicon
        nid.szTip = self._tooltip[:127]

        ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        if not ok:
            err = ctypes.get_last_error()
            logger.error("Shell_NotifyIconW(NIM_ADD) failed: ok=%s lastErr=%s "
                         "cbSize=%d hwnd=%s hIcon=%s",
                         ok, err, nid.cbSize, self._hwnd, hicon)
            return

        logger.info("System tray icon added (cbSize=%d, hwnd=%s)", nid.cbSize, self._hwnd)

        # Message loop
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # Cleanup
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        logger.info("System tray icon removed")

    # ------------------------------------------------------------------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            if lparam == WM_LBUTTONUP:
                self._on_show()
            elif lparam == WM_RBUTTONUP:
                self._show_menu(hwnd)
            return 0

        if msg == WM_COMMAND:
            cmd_id = wparam & 0xFFFF
            if cmd_id == IDM_SHOW:
                self._on_show()
            elif cmd_id == IDM_QUIT:
                self._on_quit()
            return 0

        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self, hwnd):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, IDM_SHOW, "Show / Hide")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, IDM_QUIT, "Quit")

        # Required so the menu dismisses when clicking elsewhere
        user32.SetForegroundWindow(hwnd)

        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.TrackPopupMenu(
            menu, TPM_LEFTALIGN | TPM_BOTTOMALIGN,
            pt.x, pt.y, 0, hwnd, None,
        )
        user32.DestroyMenu(menu)
