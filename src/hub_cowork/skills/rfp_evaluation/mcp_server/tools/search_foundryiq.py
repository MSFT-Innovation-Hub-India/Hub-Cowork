"""
Tool: search_foundryiq

Search the FoundryIQ knowledge base (Azure AI Search) for case study narratives,
client testimonials, and project execution stories.

CROSS-TENANT AUTH
-----------------
The Azure AI Search service lives in a RESOURCE tenant where the agent user is
a guest. Authentication must target that tenant explicitly, not the user's home
(corp) tenant. Two modes are supported:

  'browser' — InteractiveBrowserCredential with token caching to disk.
              First run opens a browser popup; subsequent runs are silent
              because the token is cached via TokenCachePersistenceOptions.
              Use this when running as a background/autonomous agent.

  'cli'     — AzureCliCredential targeting the resource tenant.
              Requires the user to have run:
                az login --tenant <RESOURCE_TENANT_ID>
              Use this in dev/test environments.

Configuration keys in hub_config.json or .env:
  FOUNDRYIQ_ENDPOINT      — e.g. "https://rfp-foundryiq-search.search.windows.net"
  FOUNDRYIQ_KB_NAME       — e.g. "rfp-knowledge-store"
  RESOURCE_TENANT_ID      — Tenant ID where the Search service lives
                            (the guest/resource tenant, NOT your corp tenant)
  FOUNDRYIQ_AUTH_MODE     — "browser" (default) or "cli"
  FOUNDRYIQ_API_VERSION   — defaults to "2025-11-01-preview"

pip dependencies:
  azure-identity[persistent-cache]   requests   python-dotenv
"""

import logging
import os
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from hub_cowork.tools._tool_result import ok, no_data, error

logger = logging.getLogger("hub_se_agent")

_SEARCH_SCOPE = "https://search.azure.com/.default"
_cached_credential = None
_cached_token: object | None = None   # azure.core.credentials.AccessToken

# Shared HTTP session with automatic retry on transient failures. Applied
# to every FoundryIQ request, not just the first one. Retry policy:
#   - 3 attempts total
#   - exponential backoff 1s -> 2s -> 4s (cap 10s)
#   - retry on 429 / 502 / 503 / 504 and on connection reset / read errors
#   - retry POST (idempotent for our use: it's a read-only search)
_session_lock = threading.Lock()
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """Return a process-wide requests.Session with retry configured."""
    global _session
    with _session_lock:
        if _session is not None:
            return _session
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=1.0,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        sess = requests.Session()
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _session = sess
        return sess

SCHEMA = {
    "type": "function",
    "name": "search_foundryiq",
    "description": (
        "Search the Contoso Engineering FoundryIQ knowledge base for case study "
        "narratives, client testimonials, execution methodology stories, and safety "
        "management examples from past projects. Use this tool when the RFP response "
        "requires qualitative project stories, verbatim client quotes, or challenge/"
        "solution narratives. Do NOT use for quantified KPIs, financial figures, or "
        "safety statistics — use query_fabric_agent for those."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural language search query describing the content needed. "
                    "Examples: 'automotive manufacturing facility delivery outcomes', "
                    "'client testimonial aerospace clean room project', "
                    "'risk management compressed schedule fast track delivery'."
                ),
            },
            "top": {
                "type": "integer",
                "description": (
                    "Number of results to return. Defaults to 3. "
                    "Use 5 for broader searches spanning multiple case studies."
                ),
            },
        },
        "required": ["query"],
    },
}


def _load_config() -> dict:
    """Load FoundryIQ config from hub_config or environment variables.

    Priority: hub_config (non-empty) > .env / os.environ > hardcoded defaults.
    """
    _KEYS = (
        "FOUNDRYIQ_ENDPOINT", "FOUNDRYIQ_KB_NAME",
        "RESOURCE_TENANT_ID", "FOUNDRYIQ_AUTH_MODE", "FOUNDRYIQ_API_VERSION",
    )
    cfg: dict[str, str] = {}
    try:
        from hub_cowork.core import hub_config
        raw = hub_config.load()
        for key in _KEYS:
            val = raw.get(key, "")
            if val:  # only use non-empty hub_config values
                cfg[key] = val
    except Exception:
        pass
    # Fall back to env vars for anything not set by hub_config
    for key in _KEYS:
        if key not in cfg:
            cfg[key] = os.environ.get(key, "")
    # Hardcoded defaults for optional keys
    cfg.setdefault("FOUNDRYIQ_KB_NAME", "rfp-knowledge-store")
    cfg.setdefault("FOUNDRYIQ_API_VERSION", "2025-11-01-preview")
    cfg.setdefault("FOUNDRYIQ_AUTH_MODE", "browser")
    return cfg


def _get_credential(tenant_id: str, auth_mode: str):
    """
    Return an Azure credential targeting the RESOURCE tenant.

    For an autonomous background agent, 'browser' mode uses
    TokenCachePersistenceOptions so the token is cached to disk
    after the first interactive login — no popup on subsequent calls.
    """
    global _cached_credential
    if _cached_credential is not None:
        return _cached_credential

    # Prefer the shared agent credential ONLY when it targets the same
    # tenant as the FoundryIQ resource. The shared credential is bound to
    # AZURE_TENANT_ID; when RESOURCE_TENANT_ID points elsewhere we must
    # build a separate InteractiveBrowserCredential for that tenant.
    try:
        from hub_cowork.core.agent_core import get_credential as _get_shared_cred
        shared_tenant = os.environ.get("AZURE_TENANT_ID")
        if shared_tenant and tenant_id and shared_tenant.lower() == tenant_id.lower():
            shared = _get_shared_cred()
            if shared is not None:
                logger.info("[FoundryIQ] Reusing shared agent credential (tenant match, silent refresh)")
                _cached_credential = shared
                return shared
    except Exception as ex:
        logger.debug("[FoundryIQ] Could not reuse shared credential: %s", ex)

    if auth_mode == "cli":
        from azure.identity import AzureCliCredential
        logger.info("[FoundryIQ] Using AzureCliCredential for tenant %s", tenant_id)
        cred = AzureCliCredential(tenant_id=tenant_id) if tenant_id else AzureCliCredential()
    else:
        from hub_cowork.core.auth_credential import make_credential, is_broker_available
        logger.info("[FoundryIQ] Using %s credential for tenant %s",
                    "WAM broker" if is_broker_available() else "InteractiveBrowser",
                    tenant_id)
        cred = make_credential(
            tenant_id=tenant_id,
            cache_name="rfp_agent_foundryiq",
            redirect_uri="http://localhost:8400",
        )

    _cached_credential = cred
    return cred


def _get_bearer_token(credential) -> str:
    """Get or refresh the bearer token for Azure Search, using the cache."""
    global _cached_token
    now = time.time()
    if _cached_token is None or _cached_token.expires_on < now + 60:
        _cached_token = credential.get_token(_SEARCH_SCOPE)
    return _cached_token.token


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    """Query the FoundryIQ knowledge base and return a JSON envelope."""
    tool = "search_foundryiq"
    query = arguments["query"]
    top = int(arguments.get("top", 3))

    cfg = _load_config()
    endpoint = cfg["FOUNDRYIQ_ENDPOINT"].rstrip("/")
    kb_name = cfg["FOUNDRYIQ_KB_NAME"]
    api_version = cfg["FOUNDRYIQ_API_VERSION"]
    tenant_id = cfg["RESOURCE_TENANT_ID"]
    auth_mode = cfg["FOUNDRYIQ_AUTH_MODE"]

    if not endpoint:
        return error(
            tool, "config",
            "FOUNDRYIQ_ENDPOINT is not configured. Add it to hub_config.json "
            "or your .env file.",
        )
    if not tenant_id:
        return error(
            tool, "config",
            "RESOURCE_TENANT_ID is not configured. This is the tenant ID "
            "where the Azure AI Search service lives (the guest/resource "
            "tenant, not your corp tenant).",
        )

    logger.info("[FoundryIQ] Searching: %s (top=%d)", query[:150], top)
    if on_progress:
        # Hardcoded headline — full model-generated query stays in the logger.
        on_progress("tool", "Searching FoundryIQ knowledge base")

    # Correct Azure AI Search Knowledge Base retrieval endpoint:
    # POST /knowledgebases('{kb_name}')/retrieve?api-version=...
    url = (
        f"{endpoint}/knowledgebases('{kb_name}')"
        f"/retrieve?api-version={api_version}"
    )

    # Build the message payload (supports multi-turn; single-turn here)
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": query}],
            }
        ],
        "outputMode": "answerSynthesis",
        "maxRuntimeInSeconds": 60,
        "maxOutputSize": 50_000,
    }

    try:
        credential = _get_credential(tenant_id, auth_mode)
        token = _get_bearer_token(credential)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

        response = _get_session().post(
            url, json=payload, headers=headers, timeout=70,
        )
        response.raise_for_status()
        data = response.json()

        # Parse synthesised answer from response messages
        answer_text = ""
        for resp_msg in data.get("response", []):
            for block in resp_msg.get("content", []):
                if block.get("type") == "text":
                    answer_text += block.get("text", "")

        refs = data.get("references", [])

        # Successful HTTP call but the knowledge base had nothing for this
        # query. Empty synthesis text AND zero references is the unambiguous
        # structural signal — no heuristics needed here.
        if not answer_text.strip() and not refs:
            logger.info("[FoundryIQ] Empty answer + 0 refs — no_data")
            if on_progress:
                on_progress("tool", "FoundryIQ returned no matching documents")
            return no_data(
                tool,
                "FoundryIQ knowledge base returned no matching documents.",
                query=query,
            )

        # Append reference count for transparency
        if refs:
            answer_text += f"\n\n[{len(refs)} source document(s) retrieved]"

        if not answer_text:
            # References without synthesised text — odd, but treat as raw payload.
            answer_text = str(data)

        logger.info("[FoundryIQ] Response received (%d chars, %d refs)", len(answer_text), len(refs))
        if on_progress:
            on_progress("tool", f"Found {len(refs)} matching document(s) in FoundryIQ")

        return ok(tool, answer_text, meta={"references": len(refs)})

    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        body = e.response.text[:500] if e.response is not None else ""
        logger.error("[FoundryIQ] HTTP %s: %s", status, body)
        if status == 401:
            return error(
                tool, "auth",
                f"FoundryIQ authentication failed (HTTP 401). Check "
                f"RESOURCE_TENANT_ID ({tenant_id}) and ensure your account "
                f"has been granted access to the Azure AI Search resource "
                f"in that tenant. If using 'cli' mode, run: "
                f"az login --tenant {tenant_id}",
            )
        if status == 403:
            return error(
                tool, "auth",
                "FoundryIQ access denied (HTTP 403). Your account is "
                "authenticated but lacks read access to the knowledge base "
                "or its underlying index.",
            )
        if status == 404:
            return error(
                tool, "config",
                f"FoundryIQ endpoint or knowledge base not found (HTTP 404). "
                f"Check FOUNDRYIQ_ENDPOINT and FOUNDRYIQ_KB_NAME. Body: {body}",
            )
        if 500 <= status < 600:
            return error(
                tool, "remote",
                f"FoundryIQ service error HTTP {status} after retry: {body}",
            )
        return error(tool, "remote", f"FoundryIQ HTTP {status}: {body}")
    except requests.ConnectionError as e:
        logger.error("[FoundryIQ] Connection error: %s", e)
        return error(tool, "network", f"FoundryIQ connection failed: {e}")
    except requests.Timeout:
        return error(
            tool, "timeout",
            "FoundryIQ request timed out after 70 seconds (incl. retries).",
        )
    except Exception as e:
        logger.error("[FoundryIQ] Unexpected error: %s", e, exc_info=True)
        return error(tool, "unexpected", f"FoundryIQ unexpected error: {e}")
