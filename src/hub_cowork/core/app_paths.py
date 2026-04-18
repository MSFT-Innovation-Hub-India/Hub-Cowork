"""
Central app-path resolution for the Hub Cowork fork.

Everything that needs a user-scoped directory (logs, auth record, thread
store, config overrides, engagement context files, etc.) routes through
this module so it is trivial to keep this fork's data separate from the
original `hub-se-agent` install.

Override with the `HUB_COWORK_HOME` environment variable if you want the
data to live somewhere else (e.g. a OneDrive-synced folder).
"""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_home() -> Path:
    env = os.environ.get("HUB_COWORK_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hub-cowork"


APP_HOME: Path = _resolve_home()
APP_HOME.mkdir(parents=True, exist_ok=True)

# Well-known subpaths. Each caller is still free to create its own nested
# directory, but these are the conventional locations.
LOG_FILE = APP_HOME / "agent.log"
AUTH_RECORD_PATH = APP_HOME / "auth_record.json"
USER_CONFIG_PATH = APP_HOME / "hub_config.json"
THREADS_DIR = APP_HOME / "threads"
ENGAGEMENT_CONTEXT_DIR = APP_HOME / "engagement_context"


# Branding constants kept here so there is ONE place to tweak if someone
# wants to stand up another parallel fork. Used by the tray, toast, taskbar
# AppUserModelID, window title, and UI chrome.
APP_DISPLAY_NAME = "Hub Cowork"
APP_USER_MODEL_ID = "Microsoft.HubCowork"
WINDOW_TITLE = "Hub Cowork"
