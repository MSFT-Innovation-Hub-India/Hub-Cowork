import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from azure.communication.email import EmailClient
from azure.identity import InteractiveBrowserCredential
from dotenv import load_dotenv

logger = logging.getLogger("hub_se_agent")

load_dotenv(override=False)


def _detect_timezone() -> str:
    """Return the IANA timezone ID for the local system.

    Priority:
      1. AGENT_TIMEZONE env var (explicit override)
      2. OS-level timezone via tzlocal
      3. Fallback to UTC
    """
    env_tz = os.environ.get("AGENT_TIMEZONE")
    if env_tz:
        logger.info("Using timezone from AGENT_TIMEZONE: %s", env_tz)
        return env_tz
    try:
        from tzlocal import get_localzone
        tz = get_localzone()
        tz_name = str(tz)
        logger.info("Detected system timezone: %s", tz_name)
        return tz_name
    except Exception as e:
        logger.warning("Could not detect system timezone (%s), falling back to UTC", e)
        return "UTC"


LOCAL_TIMEZONE = _detect_timezone()

ACS_ENDPOINT = os.environ["ACS_ENDPOINT"]
ACS_SENDER_ADDRESS = os.environ["ACS_SENDER_ADDRESS"]

_email_client: EmailClient | None = None
_credential: InteractiveBrowserCredential | None = None
_organizer_name: str | None = None
_organizer_email: str | None = None


def set_credential(credential: InteractiveBrowserCredential):
    """Set the shared credential instance (called by agent_core)."""
    global _credential
    _credential = credential


def _get_credential() -> InteractiveBrowserCredential:
    if _credential is None:
        raise RuntimeError("Credential not set. Call set_credential() first.")
    return _credential


def _resolve_organizer() -> tuple[str, str]:
    """Extract the logged-in user's display name and email from their Azure AD token."""
    global _organizer_name, _organizer_email
    if _organizer_name and _organizer_email:
        return _organizer_name, _organizer_email

    credential = _get_credential()
    token = credential.get_token("https://communication.azure.com/.default")

    # Decode JWT payload (middle segment) without verification — we just need claims
    payload = token.token.split(".")[1]
    # Fix base64 padding
    payload += "=" * (4 - len(payload) % 4)
    claims = json.loads(base64.b64decode(payload))

    _organizer_name = claims.get("name", "Hub SE Agent")
    _organizer_email = claims.get("upn") or claims.get("preferred_username") or claims.get("email", "unknown@unknown.com")

    logger.info("[Auth] Organizer resolved: %s <%s>", _organizer_name, _organizer_email)
    return _organizer_name, _organizer_email


def _get_email_client() -> EmailClient:
    """Get or create the ACS email client using the logged-in user's credential."""
    global _email_client
    if _email_client is None:
        _email_client = EmailClient(
            endpoint=ACS_ENDPOINT,
            credential=_get_credential(),
        )
    return _email_client


def _to_ics_datetime(dt_str: str) -> str:
    """Convert 'YYYY-MM-DD HH:MM' to iCalendar format '20260323T093000'."""
    dt = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M")
    # Sanity check: business events should be between 7:00 and 22:00
    if dt.hour < 7 or dt.hour >= 22:
        logger.warning(
            "Suspicious time %s (outside 07:00-22:00) — possible AM/PM error",
            dt_str,
        )
    return dt.strftime("%Y%m%dT%H%M%S")


def _build_ics(subject: str, start: str, end: str, recipients: list[str], body: str,
               location: str = "", timezone_id: str = "") -> str:
    """Build an .ics calendar invite string with the organizer set to the logged-in user."""
    if not timezone_id:
        timezone_id = LOCAL_TIMEZONE
    organizer_name, organizer_email = _resolve_organizer()

    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_ics = _to_ics_datetime(start)
    end_ics = _to_ics_datetime(end)

    attendee_lines = ""
    for email in recipients:
        attendee_lines += f"ATTENDEE;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{email}\r\n"

    ics_body = body.replace("\n", "\\n").replace(",", "\\,")

    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//MeetingAgent//EN\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{now}\r\n"
        f"DTSTART;TZID={timezone_id}:{start_ics}\r\n"
        f"DTEND;TZID={timezone_id}:{end_ics}\r\n"
        f"SUMMARY:{subject}\r\n"
        f"DESCRIPTION:{ics_body}\r\n"
        f"ORGANIZER;CN={organizer_name}:mailto:{organizer_email}\r\n"
        f"{attendee_lines}"
        f"LOCATION:{location}\r\n"
        "SEQUENCE:0\r\n"
        "STATUS:CONFIRMED\r\n"
        "TRANSP:OPAQUE\r\n"
        "X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE\r\n"
        "X-MICROSOFT-CDO-INTENDEDSTATUS:BUSY\r\n"
        "X-MICROSOFT-CDO-ALLDAYEVENT:FALSE\r\n"
        "X-MICROSOFT-CDO-IMPORTANCE:1\r\n"
        "X-MICROSOFT-CDO-INSTTYPE:0\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    return ics


def create_outlook_meeting(subject: str, start: str, end: str, recipients: list[str], body: str, location: str = ""):
    """
    Send a calendar invite via Azure Communication Services email.
    The .ics is attached as text/calendar so Outlook renders it as a proper
    meeting request with Accept/Decline. The organizer is set to the
    logged-in user so responses go to their mailbox.
    """
    organizer_name, organizer_email = _resolve_organizer()
    ics_content = _build_ics(subject, start, end, recipients, body, location)
    ics_base64 = base64.b64encode(ics_content.encode("utf-8")).decode("utf-8")

    # Build the email with .ics attachment
    message = {
        "senderAddress": ACS_SENDER_ADDRESS,
        "recipients": {
            "to": [{"address": addr} for addr in recipients],
        },
        "content": {
            "subject": subject,
            "plainText": (
                f"You have been invited to: {subject}\n\n"
                f"{body}\n\n"
                f"Organizer: {organizer_name} ({organizer_email})\n"
                f"Please open the attached calendar invite to accept or decline."
            ),
            "html": (
                f"<p>You have been invited to: <strong>{subject}</strong></p>"
                f"<p>{body.replace(chr(10), '<br>')}</p>"
                f"<p><strong>Organizer:</strong> {organizer_name} ({organizer_email})</p>"
                f"<p>Please open the attached calendar invite to accept or decline.</p>"
            ),
        },
        "attachments": [
            {
                "name": "invite.ics",
                "contentType": "text/calendar; method=REQUEST; charset=UTF-8",
                "contentInBase64": ics_base64,
            }
        ],
    }

    logger.info("[ACS] Sending invite: %s", subject)
    logger.info("      To: %s", ', '.join(recipients))
    logger.info("      Organizer: %s <%s>", organizer_name, organizer_email)

    client = _get_email_client()
    poller = client.begin_send(message)
    result = poller.result()

    logger.info("[ACS] Sent successfully (message_id: %s...)", result.get('id', 'unknown')[:30])


def send_email(subject: str, body_html: str, recipients: list[str],
               body_plain: str | None = None) -> str:
    """
    Send a plain email via Azure Communication Services on behalf of the
    logged-in user.  Returns the ACS message ID on success.
    """
    organizer_name, organizer_email = _resolve_organizer()

    if not body_plain:
        # Strip HTML tags for a basic plain-text fallback
        import re
        body_plain = re.sub(r"<[^>]+>", "", body_html)

    message = {
        "senderAddress": ACS_SENDER_ADDRESS,
        "replyTo": [{"address": organizer_email, "displayName": organizer_name}],
        "recipients": {
            "to": [{"address": addr} for addr in recipients],
        },
        "content": {
            "subject": subject,
            "plainText": body_plain,
            "html": body_html,
        },
    }

    logger.info("[ACS] Sending email: %s", subject)
    logger.info("      To: %s", ", ".join(recipients))
    logger.info("      On behalf of: %s <%s>", organizer_name, organizer_email)

    client = _get_email_client()
    poller = client.begin_send(message)
    result = poller.result()

    msg_id = result.get("id", "unknown")
    logger.info("[ACS] Email sent (message_id: %s...)", msg_id[:30])
    return msg_id
