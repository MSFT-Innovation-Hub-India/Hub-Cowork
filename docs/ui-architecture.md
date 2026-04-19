# UI Architecture — pywebview + WebView2

## TL;DR

Hub Cowork is a **native Windows desktop app** whose window contents are
rendered with HTML / CSS / vanilla JavaScript inside an embedded
**WebView2** (Chromium / Edge) instance, hosted by **pywebview**. The
Python backend (LLM router, skills, tools, WorkIQ CLI, MSAL credential,
ACS, Redis bridge) and the UI run in the **same process**; they
communicate over a local-loopback WebSocket on `127.0.0.1:18080`.

There is no Node.js, no npm/Vite build step, no second runtime to
install or update, no Electron-style bundled Chromium. WebView2 is part
of Windows 10 / 11.

## Why this is still a "native app"

"Native" describes how the app integrates with the OS, not what
technology paints the pixels inside the window. Hub Cowork has:

- A real Win32 top-level window (own HWND, taskbar entry, custom icon).
- A system-tray icon (raw Win32 via `ctypes`) with show/hide/quit menu.
- Toast notifications via `winotify` (Windows Action Center).
- A session-scoped named mutex for single-instance behavior.
- Stable `AppUserModelID` so Windows groups taskbar items correctly.
- A no-console `pythonw.exe` runtime in production (no terminal flash).
- Local files opened via `os.startfile` / OS handlers.

The same pattern (embedded webview rendering an HTML UI inside a native
window shell) is what **VS Code, Microsoft Teams, Slack, Discord,
GitHub Desktop, Azure Data Studio, 1Password, Notion, and Postman**
ship as "native desktop apps".

## Component boundaries

```
┌──────────────────────── pythonw.exe (single process) ────────────────────────┐
│                                                                              │
│  ┌────────────────────────┐         ┌──────────────────────────────────┐     │
│  │  pywebview window      │  WS     │  Python backend                  │     │
│  │  ┌──────────────────┐  │ ◄────►  │  • agent_core (LLM router)       │     │
│  │  │  WebView2        │  │ 18080   │  • thread_manager + executor     │     │
│  │  │  (Chromium/Edge) │  │         │  • skills / tools                │     │
│  │  │                  │  │         │  • WorkIQ CLI subprocess         │     │
│  │  │  chat_ui.html    │  │         │  • MSAL credential               │     │
│  │  │  chat_ui.css     │  │         │  • ACS email + ICS               │     │
│  │  │  chat_ui.js      │  │         │  • Redis bridge (optional)       │     │
│  │  └──────────────────┘  │         └──────────────────────────────────┘     │
│  └────────────────────────┘                                                  │
│                                                                              │
│  Win32 tray icon  ◄─ ctypes ─►  user32/shell32 (no extra deps)               │
└──────────────────────────────────────────────────────────────────────────────┘
```

The window is created on the main thread (pywebview requirement on
Windows). Everything else — WebSocket server, HTTP server for toast
clicks, executor pool, Redis poller, tray message pump — runs on
daemon threads owned by the same Python process.

## UI assets

The UI is plain web technology with **no build step**:

| File | Approx. size | Role |
|---|---|---|
| `src/hub_cowork/assets/chat_ui.html` | ~180 lines | Markup + `<link>` / `<script src>` |
| `src/hub_cowork/assets/chat_ui.css`  | ~560 lines | All styles |
| `src/hub_cowork/assets/chat_ui.js`   | ~2,100 lines | State, WebSocket client, render functions, Markdown renderer |

`chat_ui.js` uses a single mutable `state` object and imperative
re-render functions. `document.createElement` + `.textContent` are used
throughout — no `innerHTML` with user input — so the surface is XSS-safe
by construction.

pywebview serves the HTML from disk; the relative `<link>` and
`<script src>` references resolve against the file's own directory
inside WebView2.

## Alternatives considered

### 1. Electron + React (or any web framework)

- **Pros:** Mature component model, huge ecosystem.
- **Cons:** Bundles a private copy of Chromium (~150 MB) plus a Node.js
  runtime. Requires a Node toolchain to develop the UI. The Python
  backend would have to run as a **sidecar process** spawned from
  Electron, with stdio or socket IPC — strictly more complexity than
  what we have today.
- **Verdict:** Rejected. The size/complexity cost is not justified for
  a single-window single-user internal tool.

### 2. Tauri + React

- **Pros:** Uses system WebView2 like pywebview does, so much smaller
  than Electron. Memory-safe Rust shell.
- **Cons:** Adds a Rust toolchain *and* Node.js. Python backend still
  has to run as a sidecar with IPC. Same architectural cost as Electron
  without Electron's ecosystem.
- **Verdict:** Rejected for the same sidecar-IPC reason.

### 3. React Native for Windows

- **Pros:** Component model.
- **Cons:** Targets WinUI/XAML, not a webview, so the existing HTML/CSS
  is unusable — full UI rewrite. Smaller library ecosystem on the
  Windows fork. Still needs a Node toolchain. Still leaves Python as a
  sidecar process.
- **Verdict:** Wrong tool. RN is mobile-first; the Windows fork is a
  Microsoft port for a different problem space.

### 4. WinUI 3 / WPF / WinForms (true native widgets)

- **Pros:** Real native widgets, best OS integration, no browser engine.
- **Cons:** C# / XAML stack — different language from the backend.
  Would require either rewriting backend in .NET or running Python as
  a sidecar with IPC. Rich text rendering (Markdown, code blocks, HTML
  tables we already get for free in a webview) is significantly more
  work in XAML.
- **Verdict:** Rejected. Python is the right backend language for this
  app (LLM SDKs, MSAL, WorkIQ CLI, Redis Entra-ID auth all have
  first-class Python support); a C# UI on top of a Python sidecar would
  double the surface area.

### 5. Streamlit / Gradio / FastAPI + browser tab

- **Pros:** Pure Python, fast to prototype.
- **Cons:** Lives in a browser tab — no tray icon, no toast
  notifications, no single-instance enforcement, no native window
  identity. Not a desktop app.
- **Verdict:** Rejected. Hub Cowork needs to feel like an app the user
  launches at login and minimizes to the tray, not a tab they have to
  remember to keep open.

### 6. pywebview + vanilla JS (chosen)

- **Pros:** Single Python process. No second runtime. WebView2 is
  already on every modern Windows install. No build step, no
  `node_modules`. Trivially packageable as a one-file
  PyInstaller bundle later. Native tray, toasts, taskbar identity, all
  without leaving Python.
- **Cons:** No component framework, so the UI is plain JS and grows as
  one or a few files. Mitigated by extracting CSS and JS into separate
  assets (already done).
- **Verdict:** Chosen.

## When this choice should be revisited

Switch only if one of these becomes true:

- **Multiple developers** start working on the UI in parallel — at that
  point introduce **Preact + htm** in a `<script>` tag (component
  model, JSX-like syntax via tagged templates, ~10 KB, still no build
  step, still inside pywebview).
- The UI needs **rich third-party components** (data grids, charts,
  date pickers) faster than they can be hand-rolled.
- The same UI must also ship as a **browser-tab web app** for non-Windows
  users — at that point a real frontend framework + a separate Python
  HTTP backend starts to pay off, and pywebview becomes just the
  desktop shell wrapping the same web build.

None of these are true today.

## Related design notes

- Window is created hidden and shown only on demand (tray click or
  toast click) — see `_on_shown` in
  `src/hub_cowork/host/desktop_host.py`.
- The taskbar icon is forced via `WM_SETICON` after the window is
  shown so it doesn't fall back to the generic `pythonw.exe` Python
  icon.
- The WebSocket protocol (message `type` discriminator, server-push for
  thread updates and log streaming) is documented in the main README's
  *WebSocket Protocol* section.
