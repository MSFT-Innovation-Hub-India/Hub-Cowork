"""
Tool: query_workiq
Query the user's Microsoft 365 data via WorkIQ CLI.
"""

import logging
import subprocess
import sys

from ._tool_result import ok, no_data, error

logger = logging.getLogger("hub_se_agent")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _decode(b: bytes | None) -> str:
    """
    Decode a captured stdout/stderr byte stream from workiq.

    On Windows, workiq.exe writes output in the active ANSI code page (e.g.
    cp1252) rather than UTF-8 when stdout is redirected. If we let Python's
    subprocess decode with `encoding='utf-8'`, the reader thread raises
    UnicodeDecodeError on the first non-ASCII byte (em dash, smart quote,
    accented char, etc.), the exception is swallowed inside the reader
    thread, and `result.stdout` ends up as the empty string -- producing the
    silent "0 chars, rc=0" symptom seen from the agent. Capturing as bytes
    and decoding here with `errors='replace'` is tolerant of either encoding.
    """
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        # Fall back to cp1252 (Windows ANSI in en-US locales). If that also
        # fails, replace bad bytes so we never lose the whole response.
        try:
            return b.decode("cp1252")
        except UnicodeDecodeError:
            return b.decode("utf-8", errors="replace")


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


# Note: we intentionally do NOT sniff prose for "no matching data" phrases.
# Deciding whether a free-text answer from workiq means "empty result" vs
# "here is the answer" is a semantic judgment that belongs in the calling
# skill's LLM, not in this tool. The tool only reports objective signals:
#   - transport failure  -> status: "error"
#   - completely empty stdout -> status: "no_data"  (structural)
#   - anything else -> status: "ok" with the raw response
# Skill instructions tell the LLM to read `ok` content and recognize when
# the assistant text says it couldn't find the data.


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
    """Run WorkIQ CLI and return the output as a JSON envelope string.

    See `_tool_result.ENVELOPE_DESCRIPTION` for the contract.
    """
    tool = "query_workiq"
    question = arguments["question"]
    if not workiq_cli:
        return error(
            tool, "config",
            "WorkIQ CLI not found. Install it or set WORKIQ_PATH in .env.",
        )
    logger.info("[WorkIQ] Querying: %s", question[:200])
    if on_progress:
        # Send the full question — the UI can truncate for display but will
        # show the complete text in the tooltip and Progress tab.
        on_progress("tool", f"Querying WorkIQ: {question}")
    # Sanitize Unicode chars that cause mojibake on Windows CLI
    question = _sanitize_for_cli(question)
    try:
        # Capture as bytes (text=False) and decode ourselves with a tolerant
        # fallback chain -- workiq.exe writes cp1252 when stdout is redirected.
        # Windows command line limit is ~8191 chars. For long questions,
        # pipe via stdin in interactive mode instead of using -q argument.
        if len(question) > 7000:
            logger.info("[WorkIQ] Question too long for CLI arg (%d chars), using stdin", len(question))
            result = subprocess.run(
                [workiq_cli, "ask"],
                input=question.encode("utf-8") + b"\n",
                capture_output=True,
                timeout=180,
                creationflags=_NO_WINDOW,
            )
        else:
            result = subprocess.run(
                [workiq_cli, "ask", "-q", question],
                capture_output=True,
                timeout=120,
                creationflags=_NO_WINDOW,
            )
        stderr_text = _decode(result.stderr).strip()
        if result.returncode != 0:
            return error(
                tool, "remote",
                f"WorkIQ CLI exited with code {result.returncode}: "
                f"{stderr_text or '(no stderr)'}",
            )
        output = _decode(result.stdout).strip()
        logger.info(
            "[WorkIQ] Response received (stdout=%d chars, stderr=%d chars, rc=%d)",
            len(output), len(stderr_text), result.returncode,
        )
        if stderr_text:
            logger.info("[WorkIQ] stderr: %s", stderr_text[:2000])
        # If stdout is empty but stderr has content, surface stderr so the model
        # sees what workiq actually said (some workiq paths write to stderr when
        # stdout is not a TTY).
        if not output and stderr_text:
            output = stderr_text
        if on_progress:
            on_progress("tool", f"WorkIQ responded ({len(output)} chars)")
        if not output:
            logger.info("[WorkIQ] Empty response (structural no_data)")
            return no_data(tool, "(empty response)", query=question)
        return ok(tool, output)
    except subprocess.TimeoutExpired:
        return error(
            tool, "timeout",
            "WorkIQ CLI timed out after 120 seconds. The service did not "
            "respond in time; this is a transport failure, not an empty result.",
        )
    except FileNotFoundError as e:
        return error(tool, "config", f"WorkIQ CLI binary not found: {e}")
    except Exception as e:
        logger.error("[WorkIQ] Unexpected failure: %s", e, exc_info=True)
        return error(tool, "unexpected", f"Failed to call WorkIQ: {e}")
