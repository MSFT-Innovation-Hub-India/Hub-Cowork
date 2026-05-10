"""
Tool: create_meeting_invites
Create .ics calendar invites and send them to speakers via ACS email.
"""

import logging

from hub_cowork.core.outlook_helper import create_outlook_meeting

logger = logging.getLogger("hub_se_agent")


SCHEMA = {
    "type": "function",
    "name": "create_meeting_invites",
    "description": (
        "Create draft (unsent) meeting invites in Outlook for each "
        "speaker session. The invites will appear in the user's Outlook "
        "calendar for review before sending."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "customer_name": {
                "type": "string",
                "description": "Customer or event name for the meeting subject.",
            },
            "sessions": {
                "type": "array",
                "description": "Array of session objects to create invites for.",
                "items": {
                    "type": "object",
                    "properties": {
                        "speaker_name": {"type": "string"},
                        "speaker_email": {"type": "string"},
                        "topic": {"type": "string"},
                        "start_time": {
                            "type": "string",
                            "description": "Start time in YYYY-MM-DD HH:MM format (24h).",
                        },
                        "end_time": {
                            "type": "string",
                            "description": "End time in YYYY-MM-DD HH:MM format (24h).",
                        },
                    },
                    "required": [
                        "speaker_name",
                        "speaker_email",
                        "topic",
                        "start_time",
                        "end_time",
                    ],
                },
            },
        },
        "required": ["customer_name", "sessions"],
    },
}


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    """Create Outlook meetings and return a summary."""
    customer_name = arguments["customer_name"]
    sessions = arguments["sessions"]
    if on_progress:
        on_progress("tool", f"Creating {len(sessions)} meeting invite(s)...")
    results = []
    for s in sessions:
        try:
            subject = f"{customer_name} — {s['topic']}"
            body = (
                f"Customer Engagement: {customer_name}\n"
                f"Speaker: {s['speaker_name']}\n"
                f"Topic: {s['topic']}\n\n"
                f"This is an auto-generated invite. Please review before sending."
            )
            create_outlook_meeting(
                subject=subject,
                start=s["start_time"],
                end=s["end_time"],
                recipients=[s["speaker_email"]],
                body=body,
            )
            results.append(f"OK: {subject} -> {s['speaker_email']}")
        except Exception as e:
            results.append(f"FAILED: {s['speaker_name']} — {e}")
    return "\n".join(results)
