"""
Tool: get_hub_config
Return the Innovation Hub user configuration (topic catalog, start time, hub name).
"""

import json

SCHEMA = {
    "type": "function",
    "name": "get_hub_config",
    "description": (
        "Return the Innovation Hub configuration including hub name, "
        "default session start time, and topic catalog. "
        "Used when building the engagement agenda."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def handle(arguments: dict, **kwargs) -> str:
    from hub_cowork.core import hub_config
    config = hub_config.load()

    # Settings-UI fields like RFP_SHARE_RECIPIENTS, RFP_OUTPUT_FOLDER, etc.
    # are saved into the `_env_overrides` block by the env editor — NOT as
    # top-level keys. Flatten them on top of the merged config so callers
    # (and the LLM) see one consistent view and don't have to know which
    # bucket a given setting was saved into.
    overrides = config.get("_env_overrides")
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            # Only overlay non-empty string values — preserves the legacy
            # top-level value if the user cleared the override field.
            if isinstance(v, str) and v.strip():
                config[k] = v

    return json.dumps(config, indent=2)
