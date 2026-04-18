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
    return json.dumps(config, indent=2)
