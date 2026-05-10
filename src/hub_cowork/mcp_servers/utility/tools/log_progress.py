"""
Tool: log_progress
Log a formatted progress update for the user to see in real time.
"""

import logging

logger = logging.getLogger("hub_se_agent")


SCHEMA = {
    "type": "function",
    "name": "log_progress",
    "description": (
        "Log a formatted progress update for the user to see. Call this "
        "after each major step to show what was retrieved or decided. "
        "Use markdown formatting (tables, lists, bold)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "step_title": {
                "type": "string",
                "description": (
                    "Short title for this step, e.g. 'Agenda Retrieved', "
                    "'Speakers Filtered', 'Emails Resolved'."
                ),
            },
            "details": {
                "type": "string",
                "description": (
                    "Formatted markdown summary of what was found or "
                    "decided in this step."
                ),
            },
            "milestone": {
                "type": "boolean",
                "description": (
                    "If true, display as a prominent phase milestone "
                    "banner (e.g. phase completion). Defaults to false."
                ),
            },
        },
        "required": ["step_title", "details"],
    },
}


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    """Log a formatted progress update."""
    step_title = arguments["step_title"]
    details = arguments["details"]
    is_milestone = arguments.get("milestone", False)
    header = f"┌─ {step_title}"
    separator = "│"
    footer = f"└{'─' * 60}"
    body = "\n".join(f"│  {line}" for line in details.splitlines())
    msg = f"{header}\n{separator}\n{body}\n{separator}\n{footer}"
    logger.info("\n%s\n", msg)
    if on_progress:
        if is_milestone:
            on_progress("milestone", step_title)
        else:
            # Send the full markdown details to the UI, not just the title
            full_content = f"**{step_title}**\n\n{details}"
            on_progress("progress", full_content)
    return "Logged."
