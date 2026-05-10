"""
Tool: resolve_speakers
Batch-resolve a list of speaker names to {name, role} via a single WorkIQ
CLI call. Returns per-name status so the UI can flag typos / unknown
people without hitting WorkIQ once per field.

Response shape:
    {
      "results": [
        {"input": "Srikantan Sankaran",
         "status": "matched" | "ambiguous" | "not_found" | "error",
         "matches": [{"name": "...", "role": "...", "upn": "..."}]},
        ...
      ]
    }
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys

logger = logging.getLogger("hub_se_agent")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


SCHEMA = {
    "type": "function",
    "name": "resolve_speakers",
    "description": (
        "Given a list of speaker display names, look each one up in the "
        "user's Microsoft 365 directory via WorkIQ and return their "
        "canonical name and job title. Use this to validate speaker "
        "entries in one batch instead of calling query_workiq per name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of speaker display names to resolve.",
            },
        },
        "required": ["names"],
    },
}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _build_prompt(names: list[str]) -> str:
    """Ask WorkIQ to resolve all names in one shot. Pins output to JSON."""
    numbered = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(names))
    return (
        "For EACH of the following person names, look them up in the "
        "organization's directory (Microsoft Graph / people search) and "
        "return their official display name, job title, and UPN/email "
        "if known.\n\n"
        f"Names:\n{numbered}\n\n"
        "Respond with ONLY a JSON object (no prose, no code fences) "
        "matching this exact shape:\n"
        "{\n"
        '  "results": [\n'
        '    {"input": "<the name I asked about>",\n'
        '     "matches": [\n'
        '       {"name": "<display name>", "role": "<job title>", '
        '"upn": "<email or empty string>"}\n'
        "     ]}\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- One entry in results[] per input name, in the same order.\n"
        "- If no person is found, return an empty matches array for that "
        "input.\n"
        "- If multiple people match (ambiguous), include all of them in "
        "matches (up to 5).\n"
        "- Never invent a person. If uncertain, return empty matches.\n"
        "- role must be the job title / designation only, not a sentence."
    )


def _extract_json(raw: str) -> dict | None:
    """Pull the first {...} JSON object out of a possibly-chatty response."""
    if not raw:
        return None
    # Strip markdown fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    # Find first balanced object.
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(raw)):
        c = raw[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _classify(matches: list[dict]) -> str:
    if not matches:
        return "not_found"
    if len(matches) == 1:
        return "matched"
    return "ambiguous"


def _normalize_matches(raw: object) -> list[dict]:
    """Coerce WorkIQ's match list into our {name, role, upn} schema."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or m.get("displayName") or "").strip()
        role = (m.get("role") or m.get("jobTitle") or m.get("title") or "").strip()
        upn = (m.get("upn") or m.get("mail") or m.get("email") or "").strip()
        if name:
            out.append({"name": name, "role": role, "upn": upn})
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve(names: list[str], *, workiq_cli: str | None,
            on_progress=None) -> dict:
    """Core callable. Used by the WebSocket handler and the tool handle()."""
    # Clean + dedupe while preserving order.
    seen: set[str] = set()
    cleaned: list[str] = []
    for n in names or []:
        s = (n or "").strip()
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            cleaned.append(s)

    if not cleaned:
        return {"results": []}

    if not workiq_cli:
        return {
            "results": [
                {"input": n, "status": "error",
                 "error": "workiq CLI not found",
                 "matches": []}
                for n in cleaned
            ],
        }

    prompt = _build_prompt(cleaned)
    if on_progress:
        on_progress("tool", f"Resolving {len(cleaned)} speaker(s) via WorkIQ…")

    try:
        proc = subprocess.run(
            [workiq_cli, "ask"],
            input=prompt + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {
            "results": [
                {"input": n, "status": "error",
                 "error": "WorkIQ timed out", "matches": []}
                for n in cleaned
            ],
        }
    except Exception as e:
        logger.exception("resolve_speakers: subprocess failed")
        return {
            "results": [
                {"input": n, "status": "error",
                 "error": str(e), "matches": []}
                for n in cleaned
            ],
        }

    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return {
            "results": [
                {"input": n, "status": "error",
                 "error": err, "matches": []}
                for n in cleaned
            ],
        }

    parsed = _extract_json(proc.stdout)
    raw_results = (parsed or {}).get("results")
    if not isinstance(raw_results, list):
        logger.warning(
            "resolve_speakers: WorkIQ output not JSON-parseable, raw=%r",
            (proc.stdout or "")[:400],
        )
        return {
            "results": [
                {"input": n, "status": "error",
                 "error": "Unable to parse WorkIQ response",
                 "matches": []}
                for n in cleaned
            ],
        }

    # Index WorkIQ results by input (case-insensitive).
    by_input: dict[str, list[dict]] = {}
    for r in raw_results:
        if not isinstance(r, dict):
            continue
        key = (r.get("input") or "").strip().lower()
        by_input[key] = _normalize_matches(r.get("matches"))

    final: list[dict] = []
    for n in cleaned:
        matches = by_input.get(n.lower(), [])
        final.append({
            "input": n,
            "status": _classify(matches),
            "matches": matches,
        })

    if on_progress:
        matched = sum(1 for r in final if r["status"] == "matched")
        on_progress("tool",
                    f"Speaker lookup complete: {matched}/{len(final)} matched")

    return {"results": final}


def handle(arguments: dict, *, on_progress=None, workiq_cli=None,
           **kwargs) -> str:
    """Tool entrypoint — returns a JSON string for the LLM."""
    names = arguments.get("names") or []
    if not isinstance(names, list):
        return json.dumps({"error": "names must be an array of strings"})
    result = resolve([str(n) for n in names],
                     workiq_cli=workiq_cli,
                     on_progress=on_progress)
    return json.dumps(result, ensure_ascii=False)
