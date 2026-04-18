"""Hub Cowork — a Windows desktop AI agent.

This package bundles the desktop host, agent runtime, shared tools, and
skill folders into a single importable package.

Layout:
    hub_cowork/
        core/    — runtime: config, paths, auth, thread manager, executor
        host/    — desktop process: WebSocket server, pywebview, tray, Redis
        tools/   — shared tool implementations (cross-skill)
        skills/  — skill YAMLs + skill-private tools (portable folders)
        assets/  — HTML, icons, default config JSON

Entry points:
    python -m hub_cowork                 → launch the desktop host
    python -m hub_cowork.host.console    → launch the console REPL
"""
