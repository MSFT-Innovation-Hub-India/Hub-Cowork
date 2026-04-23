"""
Tool: query_workiq
Query the user's Microsoft 365 data via WorkIQ CLI.
"""

import logging
import subprocess
import sys

logger = logging.getLogger("hub_se_agent")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Unicode chars that cause mojibake when passed through CLI on Windows
_UNICODE_REPLACEMENTS = {
    "\u2014": "--",     # em dash —
    "\u2013": "-",      # en dash –
    "\u201c": '"',      # left double quote "
    "\u201d": '"',      # right double quote "
    "\u2018": "'",      # left single quote '
    "\u2019": "'",      # right single quote '
    "\u2192": "->",     # right arrow →
    "\u2190": "<-",     # left arrow ←
    "\u2026": "...",    # ellipsis …
    "\u2022": "-",      # bullet •
    "\u00b7": "-",      # middle dot ·
}


def _sanitize_for_cli(text: str) -> str:
    """Replace Unicode characters that cause encoding issues on Windows CLI."""
    for char, replacement in _UNICODE_REPLACEMENTS.items():
        text = text.replace(char, replacement)
    return text


SCHEMA = {
    "type": "function",
    "name": "query_workiq",
    "description": (
        "Query the user's Microsoft 365 data via WorkIQ CLI. Use this to "
        "retrieve agenda details, speakers, topics, time slots, email "
        "addresses, calendar events, documents, emails, contacts, and "
        "any other M365 data."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The natural language question to ask WorkIQ about "
                    "the user's M365 data."
                ),
            }
        },
        "required": ["question"],
    },
}


def handle(arguments: dict, *, on_progress=None, workiq_cli=None, **kwargs) -> str:
    """Run WorkIQ CLI and return the output."""
    question = arguments["question"]
    if not workiq_cli:
        return "Error: workiq CLI not found. Install it or set WORKIQ_PATH in .env"
    logger.info("[WorkIQ] Querying: %s", question[:200])
    if on_progress:
        # Send the full question — the UI can truncate for display but will
        # show the complete text in the tooltip and Progress tab.
        on_progress("tool", f"Querying WorkIQ: {question}")
    # Sanitize Unicode chars that cause mojibake on Windows CLI
    question = _sanitize_for_cli(question)
    try:
        # Windows command line limit is ~8191 chars. For long questions,
        # pipe via stdin in interactive mode instead of using -q argument.
        if len(question) > 7000:
            logger.info("[WorkIQ] Question too long for CLI arg (%d chars), using stdin", len(question))
            result = subprocess.run(
                [workiq_cli, "ask"],
                input=question + "\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
                creationflags=_NO_WINDOW,
            )
        else:
            result = subprocess.run(
                [workiq_cli, "ask", "-q", question],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
                creationflags=_NO_WINDOW,
            )
        if result.returncode != 0:
            return f"WorkIQ error (exit code {result.returncode}): {(result.stderr or '').strip()}"
        output = (result.stdout or "").strip()
        logger.info("[WorkIQ] Response received (%d chars)", len(output))
        if on_progress:
            on_progress("tool", f"WorkIQ responded ({len(output)} chars)")
        return output
    except subprocess.TimeoutExpired:
        return "WorkIQ timed out after 120 seconds."
    except Exception as e:
        return f"Failed to call WorkIQ: {e}"
