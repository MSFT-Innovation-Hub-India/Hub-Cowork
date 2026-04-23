"""
Tool: query_fabric_agent

Call a published Microsoft Fabric Data Agent DIRECTLY via its OpenAI-compatible
Assistants endpoint. This skips the Foundry hop (which was adding 10-15 min of
latency / timeouts when invoked as a Foundry connected tool) and talks straight
to Fabric.

Endpoint shape (from the data agent's "Publish" pane in Fabric):
  https://api.fabric.microsoft.com/v1/workspaces/<ws>/dataagents/<id>/aiassistant/openai

Auth:
  Bearer token scoped to https://api.fabric.microsoft.com/.default
  Targets the RESOURCE tenant where the Fabric workspace lives (same tenant as
  FoundryIQ search). The credential is shared with search_foundryiq so the user
  only signs in once.

Protocol (OpenAI Assistants, api-version=2024-05-01-preview):
  1. assistants.create(model="not used")     # model field is ignored by Fabric
  2. threads.create()
  3. threads.messages.create(role="user", content=question)
  4. threads.runs.create(assistant_id=...)
  5. poll runs.retrieve until status terminal
  6. threads.messages.list(order="desc")  -> latest assistant message
  7. threads.delete                       # cleanup

Configuration keys (hub_config.json or .env):
  FABRIC_DATA_AGENT_URL  — full published URL ending in /aiassistant/openai
  RESOURCE_TENANT_ID     — tenant ID where the Fabric workspace lives
  FABRIC_AUTH_MODE       — "browser" (default) or "cli"
  FABRIC_API_VERSION     — defaults to "2024-05-01-preview"
  FABRIC_POLL_TIMEOUT    — seconds, default 600
  FABRIC_POLL_INTERVAL   — seconds, default 3

pip dependencies:
  openai>=1.30   azure-identity[persistent-cache]
"""

import logging
import os
import time
import uuid

logger = logging.getLogger("hub_se_agent")

_FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
_TOKEN_REFRESH_BUFFER_SECONDS = 300  # refresh token if it expires within 5 min

_cached_credential = None
_cached_token: object | None = None  # azure.core.credentials.AccessToken

SCHEMA = {
    "type": "function",
    "name": "query_fabric_agent",
    "description": (
        "Query the Contoso Engineering Fabric Data Agent for quantified project "
        "intelligence from OneLake. Use this tool when the RFP response requires "
        "specific numbers: KPI outcomes (OEE, LTIFR, TRIR, defect rates), financial "
        "data (cost variance, gross margin, change orders), risk register entries "
        "with scores and mitigation strategies, milestone schedule data, team member "
        "certifications, or client satisfaction scores. "
        "Do NOT use for narrative case study text or client quotes — use "
        "search_foundryiq for those."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "Natural language question or RFP context prompt. Include: "
                    "industry, project type, estimated value, duration, and any "
                    "thresholds to check (e.g. LTIFR). The agent will return a "
                    "structured Bid Intelligence Brief or answer the specific "
                    "data question asked."
                ),
            }
        },
        "required": ["question"],
    },
}


def _load_config() -> dict:
    """Load Fabric agent config from hub_config or environment variables."""
    _KEYS = (
        "FABRIC_DATA_AGENT_URL", "RESOURCE_TENANT_ID", "FABRIC_AUTH_MODE",
        "FABRIC_API_VERSION", "FABRIC_POLL_TIMEOUT", "FABRIC_POLL_INTERVAL",
    )
    cfg: dict[str, str] = {}
    try:
        from hub_cowork.core import hub_config
        raw = hub_config.load()
        for key in _KEYS:
            val = raw.get(key, "")
            if val:
                cfg[key] = val
    except Exception:
        pass
    for key in _KEYS:
        if key not in cfg:
            cfg[key] = os.environ.get(key, "")
    cfg.setdefault("FABRIC_AUTH_MODE", "browser")
    cfg.setdefault("FABRIC_API_VERSION", "2024-05-01-preview")
    cfg.setdefault("FABRIC_POLL_TIMEOUT", "600")
    cfg.setdefault("FABRIC_POLL_INTERVAL", "3")
    return cfg


def _get_credential(tenant_id: str, auth_mode: str):
    """
    Return a credential targeting the RESOURCE tenant. Shares the persistent
    token cache with search_foundryiq so the user only authenticates once.
    """
    global _cached_credential

    # Reuse credential from search_foundryiq if already initialised
    try:
        from . import search_foundryiq as sq
        if sq._cached_credential is not None:
            logger.info("[FabricAgent] Reusing credential from search_foundryiq")
            return sq._cached_credential
    except Exception:
        pass

    if _cached_credential is not None:
        return _cached_credential

    if auth_mode == "cli":
        from azure.identity import AzureCliCredential
        logger.info("[FabricAgent] Using AzureCliCredential for tenant %s", tenant_id)
        cred = AzureCliCredential(tenant_id=tenant_id) if tenant_id else AzureCliCredential()
    else:
        from azure.identity import InteractiveBrowserCredential
        try:
            from azure.identity import TokenCachePersistenceOptions
            cache_opts = TokenCachePersistenceOptions(name="rfp_agent_foundryiq")
            logger.info("[FabricAgent] Using InteractiveBrowserCredential (cached) for tenant %s", tenant_id)
            cred = InteractiveBrowserCredential(
                tenant_id=tenant_id,
                redirect_uri="http://localhost:8400",
                cache_persistence_options=cache_opts,
            )
        except ImportError:
            logger.warning("[FabricAgent] Persistent cache not available; using non-cached credential")
            cred = InteractiveBrowserCredential(
                tenant_id=tenant_id,
                redirect_uri="http://localhost:8400",
            )

    _cached_credential = cred
    try:
        from . import search_foundryiq as sq
        sq._cached_credential = cred
    except Exception:
        pass
    return cred


def _get_token(credential) -> str:
    """Get a Fabric-scoped access token, caching until near expiry."""
    global _cached_token
    now = int(time.time())
    if _cached_token is not None and getattr(_cached_token, "expires_on", 0) - now > _TOKEN_REFRESH_BUFFER_SECONDS:
        return _cached_token.token  # type: ignore[union-attr]
    logger.info("[FabricAgent] Acquiring Fabric token (scope=%s)", _FABRIC_SCOPE)
    _cached_token = credential.get_token(_FABRIC_SCOPE)
    return _cached_token.token  # type: ignore[union-attr]


def _build_client(base_url: str, api_version: str, credential):
    """OpenAI client subclass that injects a Fabric bearer token per request."""
    from openai import OpenAI
    from openai._models import FinalRequestOptions
    from openai._types import Omit
    from openai._utils import is_given

    cred = credential

    class FabricOpenAI(OpenAI):
        def __init__(self) -> None:
            super().__init__(
                api_key="placeholder",  # required by SDK; real auth is per-request header
                base_url=base_url,
                default_query={"api-version": api_version},
            )

        def _prepare_options(self, options: FinalRequestOptions) -> None:
            headers: dict[str, str | Omit] = (
                {**options.headers} if is_given(options.headers) else {}
            )
            headers["Authorization"] = f"Bearer {_get_token(cred)}"
            headers.setdefault("Accept", "application/json")
            headers.setdefault("ActivityId", str(uuid.uuid4()))
            options.headers = headers
            return super()._prepare_options(options)

    return FabricOpenAI()


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    """Call the Fabric Data Agent directly via its published OpenAI endpoint."""
    question = arguments["question"]
    cfg = _load_config()

    base_url = cfg["FABRIC_DATA_AGENT_URL"].rstrip("/")
    tenant_id = cfg["RESOURCE_TENANT_ID"]
    auth_mode = cfg["FABRIC_AUTH_MODE"]
    api_version = cfg["FABRIC_API_VERSION"]

    try:
        poll_timeout = int(cfg["FABRIC_POLL_TIMEOUT"])
    except (TypeError, ValueError):
        poll_timeout = 600
    try:
        poll_interval = float(cfg["FABRIC_POLL_INTERVAL"])
    except (TypeError, ValueError):
        poll_interval = 3.0

    if not base_url:
        return (
            "Error: FABRIC_DATA_AGENT_URL is not configured. Add the published "
            "URL from the Fabric data agent (ends in /aiassistant/openai) to "
            "hub_config.json or your .env file."
        )
    if not tenant_id:
        return (
            "Error: RESOURCE_TENANT_ID is not configured. This is the tenant ID "
            "where the Fabric workspace lives."
        )

    logger.info("[FabricAgent] Querying Fabric Data Agent directly: %s", question[:150])
    if on_progress:
        on_progress("tool", f"Querying Fabric Data Agent: {question}")

    thread_id: str | None = None
    client = None
    try:
        credential = _get_credential(tenant_id, auth_mode)
        client = _build_client(base_url, api_version, credential)

        # 1. Assistant (model field is ignored by Fabric; required by API shape)
        assistant = client.beta.assistants.create(model="not used")

        # 2. Thread + 3. user message
        thread = client.beta.threads.create()
        thread_id = thread.id
        client.beta.threads.messages.create(
            thread_id=thread_id, role="user", content=question
        )

        # 4. Run
        run = client.beta.threads.runs.create(
            thread_id=thread_id, assistant_id=assistant.id
        )

        # 5. Poll
        terminal = {"completed", "failed", "cancelled", "expired", "requires_action"}
        start = time.time()
        last_status = run.status
        while run.status not in terminal:
            elapsed = time.time() - start
            if elapsed > poll_timeout:
                raise TimeoutError(
                    f"Fabric run polling exceeded {poll_timeout}s "
                    f"(last status={run.status})"
                )
            time.sleep(poll_interval)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run.status != last_status:
                logger.info("[FabricAgent] run status: %s (%.1fs elapsed)", run.status, elapsed)
                if on_progress:
                    on_progress("tool", f"Fabric run: {run.status} ({int(elapsed)}s)")
                last_status = run.status

        if run.status != "completed":
            err = getattr(run, "last_error", None)
            err_msg = getattr(err, "message", str(err)) if err else "no error details"
            return f"Fabric Agent run ended with status='{run.status}': {err_msg}"

        # 6. Read assistant reply (latest assistant message)
        msgs = client.beta.threads.messages.list(thread_id=thread_id, order="desc")
        result_text = ""
        for m in msgs.data:
            if m.role == "assistant":
                parts: list[str] = []
                for block in (m.content or []):
                    text = getattr(block, "text", None)
                    if text is not None:
                        parts.append(getattr(text, "value", "") or "")
                result_text = "\n".join(p for p in parts if p)
                break

        if not result_text:
            result_text = "(Fabric Data Agent returned an empty response.)"

        logger.info("[FabricAgent] Response received (%d chars)", len(result_text))
        if on_progress:
            on_progress("tool", f"Fabric Agent responded ({len(result_text)} chars)")
        return result_text

    except ImportError as e:
        return (
            f"Error: required package not installed ({e}). "
            "Run: pip install openai azure-identity"
        )
    except TimeoutError as e:
        logger.error("[FabricAgent] %s", e)
        return f"Fabric Agent timed out: {e}"
    except Exception as e:
        error_str = str(e)
        logger.error("[FabricAgent] Error: %s", error_str, exc_info=True)
        if "401" in error_str or "unauthorized" in error_str.lower():
            return (
                f"Fabric Agent authentication failed (401). Check RESOURCE_TENANT_ID "
                f"({tenant_id}) and that your account has access to the Fabric "
                f"workspace hosting the data agent. If using 'cli' mode, run: "
                f"az login --tenant {tenant_id}"
            )
        if "403" in error_str or "forbidden" in error_str.lower():
            return (
                "Fabric Agent access denied (403). Ensure your account has at "
                "least Viewer permission on the Fabric workspace and read access "
                "to the data agent's underlying data sources."
            )
        return f"Fabric Agent error: {e}"
    finally:
        # 7. Cleanup
        if client is not None and thread_id is not None:
            try:
                client.beta.threads.delete(thread_id=thread_id)
            except Exception as cleanup_err:
                logger.debug("[FabricAgent] thread cleanup failed: %s", cleanup_err)
