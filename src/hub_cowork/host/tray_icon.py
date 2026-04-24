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
        # NIM_MODIFY state — set inside _run_inner once the icon is added.
        self._nid: NOTIFYICONDATAW | None = None
        self._hicon_base = None      # plain icon
        self._hicon_badged = None    # plain icon + red dot overlay (lazy)
        self._badge_on = False

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

        # Stash for NIM_MODIFY (badge updates).
        self._nid = nid
        self._hicon_base = hicon

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

    # ------------------------------------------------------------------
    # Badge / tooltip updates (callable from any thread)
    # ------------------------------------------------------------------

    def set_badge(self, on: bool, tooltip: str | None = None):
        """Show/hide a small red dot overlay on the tray icon, and
        optionally update the tooltip.

        Safe to call from any thread — Shell_NotifyIconW is thread-safe
        for our usage (single owner, NIM_MODIFY).
        """
        if not self._nid or not self._hicon_base:
            return  # tray not yet initialised
        try:
            if on:
                if not self._hicon_badged:
                    self._hicon_badged = self._make_badged_icon(self._hicon_base)
                hicon = self._hicon_badged or self._hicon_base
            else:
                hicon = self._hicon_base

            self._badge_on = bool(on)
            self._nid.hIcon = hicon
            self._nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
            if tooltip is not None:
                self._nid.szTip = tooltip[:127]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        except Exception as e:
            logger.warning("Tray set_badge failed: %s", e)

    def _make_badged_icon(self, base_hicon):
        """Compose a 32x32 icon from `base_hicon` with a red dot in the
        bottom-right. Returns a new HICON, or None on failure.

        Uses GDI directly so we don't pull in Pillow as a runtime dep.
        """
        try:
            gdi32 = ctypes.windll.gdi32
            ICON_SIZE = 32

            # 1) Memory DC + 32-bit DIB section to draw into.
            screen_dc = user32.GetDC(0)
            mem_dc = gdi32.CreateCompatibleDC(screen_dc)

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                    ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
                ]
            class BITMAPINFO(ctypes.Structure):
                _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

            bi = BITMAPINFO()
            bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bi.bmiHeader.biWidth = ICON_SIZE
            bi.bmiHeader.biHeight = ICON_SIZE  # bottom-up
            bi.bmiHeader.biPlanes = 1
            bi.bmiHeader.biBitCount = 32
            bi.bmiHeader.biCompression = 0  # BI_RGB

            bits_ptr = ctypes.c_void_p()
            color_bmp = gdi32.CreateDIBSection(
                mem_dc, ctypes.byref(bi), 0,  # DIB_RGB_COLORS
                ctypes.byref(bits_ptr), None, 0,
            )
            mask_bmp = gdi32.CreateBitmap(ICON_SIZE, ICON_SIZE, 1, 1, None)

            old_bmp = gdi32.SelectObject(mem_dc, color_bmp)

            # 2) Draw base icon onto DIB.
            DI_NORMAL = 0x0003
            user32.DrawIconEx(mem_dc, 0, 0, base_hicon,
                              ICON_SIZE, ICON_SIZE, 0, None, DI_NORMAL)

            # 3) Draw red dot in bottom-right corner.
            #    Use a solid red brush + null pen for a clean filled circle.
            RED = 0x000000FF  # 0x00BBGGRR (Win32 COLORREF)
            brush = gdi32.CreateSolidBrush(RED)
            white = gdi32.CreateSolidBrush(0x00FFFFFF)
            old_brush = gdi32.SelectObject(mem_dc, white)
            NULL_PEN = 8
            null_pen = gdi32.GetStockObject(NULL_PEN)
            old_pen = gdi32.SelectObject(mem_dc, null_pen)

            # White halo (12px) then red dot (10px) at bottom-right.
            gdi32.Ellipse(mem_dc, 19, 19, 32, 32)
            gdi32.SelectObject(mem_dc, brush)
            gdi32.Ellipse(mem_dc, 20, 20, 31, 31)

            # Cleanup brushes/pens.
            gdi32.SelectObject(mem_dc, old_brush)
            gdi32.SelectObject(mem_dc, old_pen)
            gdi32.DeleteObject(brush)
            gdi32.DeleteObject(white)

            # GDI doesn't write the alpha channel on 32bpp DIBs, so pixels
            # we just drew have A=0 and Windows composites them as fully
            # transparent (giving us a white-ish patch). Force A=0xFF on the
            # badge region so the colors come through. Bits are BGRA, bottom-up.
            try:
                gdi32.GdiFlush()  # ensure pending GDI ops are written to the DIB
                row_bytes = ICON_SIZE * 4
                buf = (ctypes.c_ubyte * (ICON_SIZE * row_bytes)).from_address(bits_ptr.value)
                # Badge region in icon coords: x in [19,32), y in [19,32).
                # In bottom-up DIB, row 0 is the BOTTOM, so y_icon=19 maps to
                # dib_row = ICON_SIZE - 1 - 19 = 12, down to dib_row = 0.
                for y_icon in range(19, 32):
                    dib_row = ICON_SIZE - 1 - y_icon
                    base = dib_row * row_bytes
                    for x in range(19, 32):
                        # Only force alpha if we actually painted (non-black).
                        # All four channels black = untouched corner pixels.
                        idx = base + x * 4
                        if buf[idx] | buf[idx + 1] | buf[idx + 2]:
                            buf[idx + 3] = 0xFF
            except Exception as alpha_err:
                logger.warning("Tray badge alpha-fix failed: %s", alpha_err)

            # 4) Build ICONINFO -> CreateIconIndirect -> HICON.
            class ICONINFO(ctypes.Structure):
                _fields_ = [
                    ("fIcon", wt.BOOL),
                    ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                    ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP),
                ]
            ii = ICONINFO()
            ii.fIcon = True
            ii.hbmMask = mask_bmp
            ii.hbmColor = color_bmp

            user32.CreateIconIndirect.restype = wt.HICON
            new_hicon = user32.CreateIconIndirect(ctypes.byref(ii))

            # GDI cleanup — CreateIconIndirect makes its own copies.
            gdi32.SelectObject(mem_dc, old_bmp)
            gdi32.DeleteObject(color_bmp)
            gdi32.DeleteObject(mask_bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, screen_dc)

            return new_hicon
        except Exception as e:
            logger.warning("Tray badge composition failed: %s", e)
            return None
