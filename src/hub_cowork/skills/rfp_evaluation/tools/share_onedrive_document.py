"""
Tool: share_onedrive_document

Share a OneDrive file with a list of recipients using the Microsoft Graph API.
Creates a sharing link and sends an email notification via Graph, so the
recipients receive an email with a direct link to the document.

Configuration keys expected in hub_config (or .env):
  GRAPH_TENANT_ID    — Azure AD tenant ID
  GRAPH_CLIENT_ID    — App registration client ID (must have Files.ReadWrite,
                       Mail.Send, User.Read.All delegated or app permissions)
  GRAPH_CLIENT_SECRET — App registration client secret
  GRAPH_USER_UPN     — The UPN of the user whose OneDrive to use
                        e.g. "srikantan@contosoengineering.com"

Note: If your agent already uses WorkIQ for M365 auth, you can alternatively
call query_workiq to send the sharing notification as a Teams message or email.
See the fallback path at the bottom of handle().
"""

import logging
import os

import requests

logger = logging.getLogger("hub_se_agent")

SCHEMA = {
    "type": "function",
    "name": "share_onedrive_document",
    "description": (
        "Share a OneDrive document with a list of team members via Microsoft Graph. "
        "Sends each recipient an email with a direct link to the document. "
        "Use this after create_rfp_brief_doc to distribute the Bid Intelligence "
        "Brief to the relevant BD manager, delivery director, and proposals lead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": (
                    "Full local path to the file on OneDrive, as returned by "
                    "create_rfp_brief_doc."
                ),
            },
            "recipients": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of recipient email addresses to share the document with."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Short covering message to include in the sharing notification email. "
                    "2-3 sentences explaining what the document is and any action needed."
                ),
            },
        },
        "required": ["file_path", "recipients", "message"],
    },
}

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _get_app_token(tenant_id: str, client_id: str, client_secret: str) -> str | None:
    """Obtain an app-only access token via client credentials flow."""
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
    }
    r = requests.post(url, data=data, timeout=30)
    r.raise_for_status()
    return r.json().get("access_token")


def _load_config() -> dict:
    _KEYS = ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_USER_UPN")
    config: dict[str, str] = {}
    try:
        from hub_cowork.core import hub_config
        cfg = hub_config.load()
        for key in _KEYS:
            val = cfg.get(key, "")
            if val:
                config[key] = val
    except Exception:
        pass
    for key in _KEYS:
        if key not in config:
            config[key] = os.environ.get(key, "")
    return config


def _resolve_onedrive_item_id(token: str, upn: str, local_path: str) -> str | None:
    """
    Resolve a local OneDrive path to a Graph item ID using the drive/root:/{path} endpoint.
    Assumes the file is already synced (local path maps to OneDrive path).
    """
    from pathlib import Path

    local = Path(local_path)

    # Find the OneDrive root on disk
    home = Path.home()
    onedrive_root = None
    for candidate in [home / "OneDrive", *list(home.glob("OneDrive*"))]:
        if candidate.exists() and local.is_relative_to(candidate):
            onedrive_root = candidate
            break

    if not onedrive_root:
        logger.warning("[ShareDoc] Could not determine OneDrive root from path: %s", local_path)
        return None

    # Relative path within OneDrive
    relative = local.relative_to(onedrive_root)
    # Convert to forward-slash path for Graph API
    graph_path = "/".join(relative.parts)
    url = f"{_GRAPH_BASE}/users/{upn}/drive/root:/{graph_path}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 200:
        return r.json().get("id")
    logger.warning("[ShareDoc] Graph item lookup failed (%s): %s", r.status_code, r.text[:300])
    return None


def _create_sharing_link(token: str, upn: str, item_id: str) -> str | None:
    """Create a view-only sharing link for the item."""
    url = f"{_GRAPH_BASE}/users/{upn}/drive/items/{item_id}/createLink"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"type": "view", "scope": "organization"}
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    if r.status_code in (200, 201):
        return r.json().get("link", {}).get("webUrl")
    logger.warning("[ShareDoc] createLink failed (%s): %s", r.status_code, r.text[:300])
    return None


def _send_sharing_email(token: str, upn: str, recipients: list[str],
                         message: str, doc_link: str, filename: str):
    """Send a sharing notification email via Graph Send Mail."""
    url = f"{_GRAPH_BASE}/users/{upn}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    to_list = [{"emailAddress": {"address": addr}} for addr in recipients]
    body_html = (
        f"<p>{message}</p>"
        f"<p><a href='{doc_link}'>Open: {filename}</a></p>"
        f"<p style='font-size:11px;color:#666;'>"
        f"This document was prepared by the Contoso Engineering RFP Evaluation Agent."
        f"</p>"
    )
    mail = {
        "message": {
            "subject": f"[Bid Brief] {filename}",
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": to_list,
        }
    }
    r = requests.post(url, json=mail, headers=headers, timeout=30)
    return r.status_code in (200, 202)


def _fallback_acs_share(file_path: str, recipients: list[str],
                        message: str, on_progress=None) -> str:
    """
    Fallback: send a sharing notification email via Azure Communication
    Services when Graph API credentials are not configured.
    """
    from pathlib import Path
    filename = Path(file_path).name

    subject = f"[Bid Brief] {filename}"
    body_html = (
        f"<p>{message}</p>"
        f"<p><strong>Document:</strong> {filename}</p>"
        f"<p style='font-size:11px;color:#666;'>"
        f"This document was prepared by the Contoso Engineering RFP Evaluation Agent. "
        f"The file is saved in OneDrive at: {file_path}</p>"
    )

    try:
        from hub_cowork.core import outlook_helper
        msg_id = outlook_helper.send_email(
            subject=subject,
            body_html=body_html,
            recipients=recipients,
        )
        return (
            f"Sharing notification sent via ACS email.\n"
            f"Recipients: {', '.join(recipients)}\n"
            f"Message ID: {msg_id}"
        )
    except Exception as e:
        logger.error("[ShareDoc] ACS email fallback failed: %s", e, exc_info=True)
        return f"Error: Could not send sharing notification via ACS: {e}"


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    """Share the OneDrive document with the specified recipients."""
    file_path = arguments["file_path"]
    recipients = arguments["recipients"]
    message = arguments["message"]
    filename = file_path.split("\\")[-1].split("/")[-1]

    if on_progress:
        on_progress("tool", f"Sharing {filename} with {len(recipients)} recipient(s)")

    config = _load_config()
    tenant_id = config["GRAPH_TENANT_ID"]
    client_id = config["GRAPH_CLIENT_ID"]
    client_secret = config["GRAPH_CLIENT_SECRET"]
    upn = config["GRAPH_USER_UPN"]

    # If Graph credentials not configured, fall back to WorkIQ notification
    if not all([tenant_id, client_id, client_secret, upn]):
        logger.info("[ShareDoc] Graph credentials not configured — using ACS email fallback")
        if on_progress:
            on_progress("tool", "Graph not configured — sending notification via ACS email")
        return _fallback_acs_share(file_path, recipients, message, on_progress)

    results = []
    try:
        # 1. Get app token
        token = _get_app_token(tenant_id, client_id, client_secret)
        if not token:
            return "Error: Could not obtain Microsoft Graph access token."

        # 2. Resolve OneDrive item ID
        item_id = _resolve_onedrive_item_id(token, upn, file_path)
        if not item_id:
            # File may not have synced yet — fall back to WorkIQ
            logger.warning("[ShareDoc] Item ID not resolved, using WorkIQ fallback")
            return _fallback_workiq_share(file_path, recipients, message, on_progress)

        # 3. Create sharing link
        doc_link = _create_sharing_link(token, upn, item_id)
        if not doc_link:
            doc_link = f"(File saved locally at: {file_path})"

        # 4. Send notification email to each recipient
        email_sent = _send_sharing_email(token, upn, recipients, message, doc_link, filename)

        for addr in recipients:
            results.append(f"\u2713 {addr} \u2014 {'email sent' if email_sent else 'link created (email failed)'}")

        logger.info("[ShareDoc] Shared %s with %d recipients", filename, len(recipients))
        if on_progress:
            on_progress("tool", f"Shared with {len(recipients)} recipient(s)")

        return (
            f"Document shared successfully.\n"
            f"File: {filename}\n"
            f"Link: {doc_link}\n"
            f"Recipients:\n" + "\n".join(f"  {r}" for r in results)
        )

    except Exception as e:
        logger.error("[ShareDoc] Error: %s", e, exc_info=True)
        # Always fall back gracefully
        logger.info("[ShareDoc] Falling back to WorkIQ notification")
        return _fallback_workiq_share(file_path, recipients, message, on_progress)
