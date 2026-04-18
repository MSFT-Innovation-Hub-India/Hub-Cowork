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
import time

import requests

logger = logging.getLogger("hub_se_agent")

_SEARCH_SCOPE = "https://search.azure.com/.default"
_cached_credential = None
_cached_token: object | None = None   # azure.core.credentials.AccessToken

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

    if auth_mode == "cli":
        from azure.identity import AzureCliCredential
        logger.info("[FoundryIQ] Using AzureCliCredential for tenant %s", tenant_id)
        cred = AzureCliCredential(tenant_id=tenant_id) if tenant_id else AzureCliCredential()
    else:
        # browser mode with persistent token cache — silent after first login
        from azure.identity import InteractiveBrowserCredential
        try:
            from azure.identity import TokenCachePersistenceOptions
            cache_opts = TokenCachePersistenceOptions(name="rfp_agent_foundryiq")
            logger.info("[FoundryIQ] Using InteractiveBrowserCredential (cached) for tenant %s", tenant_id)
            cred = InteractiveBrowserCredential(
                tenant_id=tenant_id,
                redirect_uri="http://localhost:8400",
                cache_persistence_options=cache_opts,
            )
        except ImportError:
            # azure-identity without persistent-cache extra — no caching
            logger.warning("[FoundryIQ] TokenCachePersistenceOptions not available; token will not be cached")
            cred = InteractiveBrowserCredential(
                tenant_id=tenant_id,
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
    """Query the FoundryIQ knowledge base and return synthesised answer."""
    query = arguments["query"]
    top = int(arguments.get("top", 3))

    cfg = _load_config()
    endpoint = cfg["FOUNDRYIQ_ENDPOINT"].rstrip("/")
    kb_name = cfg["FOUNDRYIQ_KB_NAME"]
    api_version = cfg["FOUNDRYIQ_API_VERSION"]
    tenant_id = cfg["RESOURCE_TENANT_ID"]
    auth_mode = cfg["FOUNDRYIQ_AUTH_MODE"]

    if not endpoint:
        return (
            "Error: FOUNDRYIQ_ENDPOINT is not configured. "
            "Add it to hub_config.json or your .env file."
        )
    if not tenant_id:
        return (
            "Error: RESOURCE_TENANT_ID is not configured. "
            "This is the tenant ID where the Azure AI Search service lives "
            "(the guest/resource tenant, not your corp tenant)."
        )

    logger.info("[FoundryIQ] Searching: %s (top=%d)", query[:150], top)
    if on_progress:
        preview = query[:100] + "..." if len(query) > 100 else query
        on_progress("tool", f"Searching FoundryIQ: {preview}")

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

        response = requests.post(url, json=payload, headers=headers, timeout=70)
        response.raise_for_status()
        data = response.json()

        # Parse synthesised answer from response messages
        answer_text = ""
        for resp_msg in data.get("response", []):
            for block in resp_msg.get("content", []):
                if block.get("type") == "text":
                    answer_text += block.get("text", "")

        # Append reference count for transparency
        refs = data.get("references", [])
        if refs:
            answer_text += f"\n\n[{len(refs)} source document(s) retrieved]"

        if not answer_text:
            answer_text = str(data)  # raw fallback for debugging

        logger.info("[FoundryIQ] Response received (%d chars, %d refs)", len(answer_text), len(refs))
        if on_progress:
            on_progress("tool", f"FoundryIQ responded ({len(answer_text)} chars, {len(refs)} sources)")

        return answer_text

    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        body = e.response.text[:500] if e.response is not None else ""
        logger.error("[FoundryIQ] HTTP %s: %s", status, body)
        if status == 401:
            return (
                f"FoundryIQ authentication failed (HTTP 401). "
                f"Check RESOURCE_TENANT_ID ({tenant_id}) and ensure your account "
                f"has been granted access to the Azure AI Search resource in that tenant. "
                f"If using 'cli' mode, run: az login --tenant {tenant_id}"
            )
        return f"FoundryIQ HTTP error {status}: {body}"
    except requests.Timeout:
        return "FoundryIQ request timed out after 70 seconds."
    except Exception as e:
        logger.error("[FoundryIQ] Unexpected error: %s", e, exc_info=True)
        return f"FoundryIQ error: {e}"
