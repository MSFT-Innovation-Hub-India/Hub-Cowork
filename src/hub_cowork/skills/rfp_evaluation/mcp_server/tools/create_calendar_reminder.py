"""
Tool: create_calendar_reminder

Create a calendar entry in the signed-in user's own Outlook calendar by
sending an .ics invite via Azure Communication Services. Used by the RFP
evaluation skill to drop deadline reminders (Q&A deadline, proposal due)
into the user's calendar without going through WorkIQ.

Supports multiple VALARM reminder offsets (e.g. 1 day before + 3 days
before) so Outlook will pop notifications at each requested point.
"""

import base64
import logging
import uuid
from datetime import datetime, timezone

from hub_cowork.core import outlook_helper
from hub_cowork.core.outlook_helper import (
    ACS_SENDER_ADDRESS,
    LOCAL_TIMEZONE,
    _get_email_client,
    _resolve_organizer,
    _to_ics_datetime,
)

logger = logging.getLogger("hub_se_agent")


SCHEMA = {
    "type": "function",
    "name": "create_calendar_reminder",
    "description": (
        "Create a calendar event/reminder in the signed-in user's own Outlook "
        "calendar by sending an .ics invite via Azure Communication Services. "
        "Use this for deadline reminders (e.g. RFP Q&A deadline, proposal due "
        "date). Reminders are delivered to the user's mailbox; the user sees "
        "them in their calendar with one or more pop-up alerts at the offsets "
        "you specify."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Calendar event title (subject line of the invite).",
            },
            "start_time": {
                "type": "string",
                "description": "Event start in 'YYYY-MM-DD HH:MM' (24h, local time).",
            },
            "end_time": {
                "type": "string",
                "description": (
                    "Event end in 'YYYY-MM-DD HH:MM' (24h). For a deadline "
                    "marker, use start + 30 minutes."
                ),
            },
            "description": {
                "type": "string",
                "description": "Body / description shown in the calendar event.",
            },
            "reminder_minutes_before": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "List of pop-up reminder offsets in minutes before the "
                    "event. Example: [1440, 4320] for 1 day and 3 days before. "
                    "Pass [] for no reminder (Outlook will apply its default)."
                ),
            },
            "category": {
                "type": "string",
                "description": "Optional Outlook category label (e.g. 'RFP').",
            },
            "high_importance": {
                "type": "boolean",
                "description": "Mark the event as high importance.",
            },
        },
        "required": ["title", "start_time", "end_time", "description"],
    },
}


def _build_ics_with_alarms(
    subject: str,
    start: str,
    end: str,
    description: str,
    organizer_name: str,
    organizer_email: str,
    reminder_minutes_before: list[int],
    category: str = "",
    high_importance: bool = False,
    timezone_id: str = "",
) -> str:
    if not timezone_id:
        timezone_id = LOCAL_TIMEZONE

    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_ics = _to_ics_datetime(start)
    end_ics = _to_ics_datetime(end)

    desc_escaped = description.replace("\n", "\\n").replace(",", "\\,")

    # The user is both organizer AND attendee — this lands the event in their
    # own mailbox without requiring anyone else to accept.
    attendee_line = (
        f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;RSVP=FALSE:"
        f"mailto:{organizer_email}\r\n"
    )

    alarm_blocks = ""
    for minutes in reminder_minutes_before or []:
        alarm_blocks += (
            "BEGIN:VALARM\r\n"
            "ACTION:DISPLAY\r\n"
            f"DESCRIPTION:{subject}\r\n"
            f"TRIGGER:-PT{int(minutes)}M\r\n"
            "END:VALARM\r\n"
        )

    importance_line = "X-MICROSOFT-CDO-IMPORTANCE:2\r\n" if high_importance else "X-MICROSOFT-CDO-IMPORTANCE:1\r\n"
    priority_line = "PRIORITY:1\r\n" if high_importance else "PRIORITY:5\r\n"
    categories_line = f"CATEGORIES:{category}\r\n" if category else ""

    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//HubCowork//RFPReminder//EN\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{now}\r\n"
        f"DTSTART;TZID={timezone_id}:{start_ics}\r\n"
        f"DTEND;TZID={timezone_id}:{end_ics}\r\n"
        f"SUMMARY:{subject}\r\n"
        f"DESCRIPTION:{desc_escaped}\r\n"
        f"ORGANIZER;CN={organizer_name}:mailto:{organizer_email}\r\n"
        f"{attendee_line}"
        f"{categories_line}"
        "SEQUENCE:0\r\n"
        "STATUS:CONFIRMED\r\n"
        "TRANSP:OPAQUE\r\n"
        "X-MICROSOFT-CDO-BUSYSTATUS:FREE\r\n"
        "X-MICROSOFT-CDO-INTENDEDSTATUS:FREE\r\n"
        "X-MICROSOFT-CDO-ALLDAYEVENT:FALSE\r\n"
        f"{importance_line}"
        f"{priority_line}"
        "X-MICROSOFT-CDO-INSTTYPE:0\r\n"
        f"{alarm_blocks}"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    return ics


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    title = arguments["title"]
    start_time = arguments["start_time"]
    end_time = arguments["end_time"]
    description = arguments["description"]
    reminder_minutes_before = arguments.get("reminder_minutes_before") or []
    category = arguments.get("category", "")
    high_importance = bool(arguments.get("high_importance", False))

    try:
        organizer_name, organizer_email = _resolve_organizer()
    except RuntimeError as e:
        return f"Error: {e}"

    if on_progress:
        on_progress(
            "tool",
            f"Creating calendar reminder via ACS: {title} ({start_time})",
        )

    try:
        ics_content = _build_ics_with_alarms(
            subject=title,
            start=start_time,
            end=end_time,
            description=description,
            organizer_name=organizer_name,
            organizer_email=organizer_email,
            reminder_minutes_before=reminder_minutes_before,
            category=category,
            high_importance=high_importance,
        )
        ics_base64 = base64.b64encode(ics_content.encode("utf-8")).decode("utf-8")

        message = {
            "senderAddress": ACS_SENDER_ADDRESS,
            "recipients": {
                "to": [{"address": organizer_email}],
            },
            "content": {
                "subject": title,
                "plainText": (
                    f"Calendar reminder: {title}\n\n"
                    f"When: {start_time} (local time)\n\n"
                    f"{description}\n\n"
                    f"Open the attached invite to add this to your calendar."
                ),
                "html": (
                    f"<p><strong>Calendar reminder:</strong> {title}</p>"
                    f"<p><strong>When:</strong> {start_time} (local time)</p>"
                    f"<p>{description.replace(chr(10), '<br>')}</p>"
                    f"<p>Open the attached invite to add this to your calendar.</p>"
                ),
            },
            "attachments": [
                {
                    "name": "reminder.ics",
                    "contentType": "text/calendar; method=REQUEST; charset=UTF-8",
                    "contentInBase64": ics_base64,
                }
            ],
        }

        logger.info("[ACS] Sending calendar reminder: %s", title)
        logger.info("      To (self): %s", organizer_email)
        if reminder_minutes_before:
            logger.info("      Alarms (mins before): %s", reminder_minutes_before)

        client = _get_email_client()
        poller = client.begin_send(message)
        result = poller.result()
        msg_id = result.get("id", "unknown")

        alarm_summary = (
            ", ".join(f"{m} min before" for m in reminder_minutes_before)
            if reminder_minutes_before
            else "Outlook default"
        )
        return (
            f"Calendar reminder created via ACS.\n"
            f"Title: {title}\n"
            f"When: {start_time}\n"
            f"Sent to (self): {organizer_email}\n"
            f"Reminders: {alarm_summary}\n"
            f"Message ID: {msg_id}"
        )

    except Exception as e:
        logger.error("[create_calendar_reminder] Failed: %s", e, exc_info=True)
        return f"Error creating calendar reminder: {e}"
