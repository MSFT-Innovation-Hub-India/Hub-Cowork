"""
Tool: send_email

Send an email via Azure Communication Services on behalf of the logged-in user.
Uses the same ACS infrastructure as the meeting-invites tool.
"""

import logging

logger = logging.getLogger("hub_se_agent")

SCHEMA = {
    "type": "function",
    "name": "send_email",
    "description": (
        "Send an email to one or more recipients via Azure Communication Services. "
        "The email is sent on behalf of the currently signed-in user (reply-to set "
        "to their address). Use this for sharing documents, notifications, or any "
        "outbound email the agent needs to send."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Email subject line.",
            },
            "body": {
                "type": "string",
                "description": (
                    "Email body in HTML. Use <p>, <strong>, <a href>, <ul>/<li> "
                    "for formatting. A plain-text fallback is generated automatically."
                ),
            },
            "recipients": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of recipient email addresses.",
            },
        },
        "required": ["subject", "body", "recipients"],
    },
}


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    """Send an email via ACS."""
    subject = arguments["subject"]
    body = arguments["body"]
    recipients = arguments["recipients"]

    if not recipients:
        return "Error: No recipients provided."

    if on_progress:
        on_progress("tool", f"Sending email to {len(recipients)} recipient(s): {subject}")

    try:
        from hub_cowork.core import outlook_helper
        msg_id = outlook_helper.send_email(
            subject=subject,
            body_html=body,
            recipients=recipients,
        )
        result = (
            f"Email sent successfully.\n"
            f"Subject: {subject}\n"
            f"Recipients: {', '.join(recipients)}\n"
            f"Message ID: {msg_id}"
        )
        logger.info("[send_email] %s", result)
        return result

    except RuntimeError as e:
        # Credential not set — user hasn't signed in yet
        return f"Error: {e}"
    except Exception as e:
        logger.error("[send_email] Failed: %s", e, exc_info=True)
        return f"Error sending email: {e}"
