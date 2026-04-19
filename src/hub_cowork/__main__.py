"""Entry point for `python -m hub_cowork`.

Applies any user-supplied environment overrides from hub_config.json
(`_env_overrides` key) to `os.environ` BEFORE importing the agent host,
so that downstream `load_dotenv(override=False)` calls and module-level
env reads pick up the UI-edited values.

Then loads the user's .env (if any) followed by the packaged
.env.defaults — both with override=False, so the precedence is:
  1. _env_overrides from Settings UI  (highest)
  2. user .env in the working dir
  3. shipped .env.defaults             (lowest)
"""

import os
from pathlib import Path


def _apply_env_overrides() -> None:
    try:
        from hub_cowork.core.hub_config import load as _load_cfg
        cfg = _load_cfg()
    except Exception:
        return
    overrides = cfg.get("_env_overrides") or {}
    if not isinstance(overrides, dict):
        return
    for key, value in overrides.items():
        if not isinstance(key, str) or not key:
            continue
        if value is None:
            continue
        sval = str(value)
        if sval == "":
            continue  # treat empty string as "leave unset"
        os.environ[key] = sval


def _load_env_files() -> None:
    """Load user .env then packaged .env.defaults, both as fallbacks."""
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    # User .env in CWD — never overrides values already set (overrides win).
    try:
        load_dotenv(override=False)
    except Exception:
        pass
    # Packaged defaults shipped inside the wheel/install.
    try:
        defaults_path = Path(__file__).parent / "assets" / ".env.defaults"
        if defaults_path.is_file():
            load_dotenv(dotenv_path=defaults_path, override=False)
    except Exception:
        pass


_apply_env_overrides()
_load_env_files()

from hub_cowork.host.desktop_host import main  # noqa: E402


if __name__ == "__main__":
    main()
