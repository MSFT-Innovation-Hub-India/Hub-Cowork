"""
Tool: engagement_context
Save and load structured engagement data between skill phases.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("hub_se_agent")

from hub_cowork.core.app_paths import ENGAGEMENT_CONTEXT_DIR as _CONTEXT_DIR
_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA = {
    "type": "function",
    "name": "engagement_context",
    "description": (
        "Save or load structured engagement context between phases of "
        "the agenda workflow. Use 'save' to persist metadata, notes, "
        "and goals after a phase. Use 'load' to retrieve context saved "
        "by a previous phase."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["save", "load"],
                "description": "Whether to save new data or load existing context.",
            },
            "customer_name": {
                "type": "string",
                "description": "Customer name (used as the context key).",
            },
            "data": {
                "type": "object",
                "description": (
                    "JSON object to save. Merged with any existing context "
                    "for this customer. Required for 'save', ignored for 'load'."
                ),
            },
        },
        "required": ["action", "customer_name"],
    },
}


def _context_path(customer_name: str) -> Path:
    """Return the file path for a customer's context."""
    safe = "".join(c if c.isalnum() else "_" for c in customer_name.strip().lower())
    return _CONTEXT_DIR / f"{safe}.json"


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    """Save or load engagement context."""
    action = arguments["action"]
    customer = arguments["customer_name"]
    path = _context_path(customer)

    if action == "save":
        new_data = arguments.get("data", {})
        if not new_data:
            return "Error: 'data' is required for save action."
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.update(new_data)
        existing["customer_name"] = customer
        path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("[EngagementContext] Saved for %s (%d keys)", customer, len(existing))
        return f"Context saved. Keys: {list(existing.keys())}"

    elif action == "load":
        if not path.exists():
            return (
                f"No engagement context found for '{customer}'. "
                "The briefing phase must run first."
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            logger.info("[EngagementContext] Loaded for %s (%d keys)", customer, len(data))
            return json.dumps(data, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"Error loading context: {e}"

    return f"Unknown action: {action}. Use 'save' or 'load'."
