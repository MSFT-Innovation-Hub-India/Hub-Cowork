"""
Hub Config — load / save user-editable configuration.

Defaults ship in hub_config.default.json (checked into repo).
User overrides are stored in the app home (see `app_paths.USER_CONFIG_PATH`,
default `~/.hub-cowork/hub_config.json`) and are never checked in.
On load, defaults are read first, then user overrides are merged on top.
"""

import json
import logging
from pathlib import Path

from hub_cowork.core.app_paths import APP_HOME, USER_CONFIG_PATH

logger = logging.getLogger("hub_se_agent")

_SCRIPT_DIR = Path(__file__).resolve().parent
# Defaults live in src/hub_cowork/assets/hub_config.default.json
_DEFAULT_PATH = _SCRIPT_DIR.parent / "assets" / "hub_config.default.json"
_USER_DIR = APP_HOME
_USER_PATH = USER_CONFIG_PATH


def _derive_speakers_by_topic(topic_catalog: list[dict]) -> list[dict]:
    """Build a lightweight speakers-by-topic view from the rich topic catalog."""
    derived = []
    for item in topic_catalog:
        if not isinstance(item, dict):
            continue
        topic = item.get("topic_category") or item.get("topic") or ""
        speakers = []
        for speaker in item.get("speakers", []):
            if isinstance(speaker, dict):
                name = str(speaker.get("name", "")).strip()
                if name:
                    speakers.append(name)
            elif isinstance(speaker, str):
                name = speaker.strip()
                if name:
                    speakers.append(name)
        if topic:
            derived.append({"topic": topic, "speakers": speakers})
    return derived


def load() -> dict:
    """Return merged config (defaults + user overrides)."""
    config = {}
    # Load defaults
    if _DEFAULT_PATH.exists():
        try:
            config = json.loads(_DEFAULT_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to read default config: %s", e)

    # Overlay user overrides
    if _USER_PATH.exists():
        try:
            overrides = json.loads(_USER_PATH.read_text(encoding="utf-8"))
            config.update(overrides)
        except Exception as e:
            logger.warning("Failed to read user config: %s", e)

    topic_catalog = config.get("topic_catalog")
    if isinstance(topic_catalog, list):
        config["speakers_by_topic"] = _derive_speakers_by_topic(topic_catalog)

    return config


def save(config: dict) -> None:
    """Persist user config to the app home (`app_paths.USER_CONFIG_PATH`)."""
    config_to_save = dict(config)
    # Persist only the richer catalog to avoid manual duplication.
    config_to_save.pop("speakers_by_topic", None)
    _USER_DIR.mkdir(parents=True, exist_ok=True)
    _USER_PATH.write_text(
        json.dumps(config_to_save, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("User config saved to %s", _USER_PATH)
