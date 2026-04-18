"""
Tool: query_fabric_agent

Call the Contoso Engineering Foundry Agent (project-analysis-agent) using the
Azure AI Projects SDK. The Foundry agent has the Fabric Data Agent attached as
a connected tool and returns structured project intelligence from OneLake.

CROSS-TENANT AUTH
-----------------
The Foundry project lives in a RESOURCE tenant where the agent user is a guest.
Auth must target that tenant explicitly. Same two modes as search_foundryiq:

  'browser' — InteractiveBrowserCredential with token caching (persistent).
              Silent after first login. Recommended for autonomous agents.
  'cli'     — AzureCliCredential. Requires: az login --tenant <RESOURCE_TENANT_ID>

A single shared credential is used for both FoundryIQ and the Fabric agent
(same resource tenant), so if the user already authenticated for FoundryIQ
the token cache is reused here automatically.

Configuration keys in hub_config.json or .env:
  FOUNDRY_PROJECT_ENDPOINT — e.g. "https://<resource>.services.ai.azure.com/
                              api/projects/<project-name>"
  FOUNDRY_AGENT_NAME       — e.g. "project-analysis-agent"
  RESOURCE_TENANT_ID       — Tenant ID where Foundry + Fabric live
                             (guest/resource tenant, NOT your corp tenant)
  FOUNDRY_AUTH_MODE        — "browser" (default) or "cli"

pip dependencies:
  azure-ai-projects   azure-identity[persistent-cache]   python-dotenv
"""

import logging
import os

logger = logging.getLogger("hub_se_agent")

# Shared credential cache (same resource tenant as FoundryIQ)
_cached_credential = None

SCHEMA = {
    "type": "function",
    "name": "query_fabric_agent",
    "description": (
        "Query the Contoso Engineering Fabric Data Agent via Azure AI Foundry for "
        "quantified project intelligence from OneLake. Use this tool when the RFP "
        "response requires specific numbers: KPI outcomes (OEE, LTIFR, TRIR, defect "
        "rates), financial data (cost variance, gross margin, change orders), risk "
        "register entries with scores and mitigation strategies, milestone schedule "
        "data, team member certifications, or client satisfaction scores. "
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
    """Load Fabric agent config from hub_config or environment variables.

    Priority: hub_config (non-empty) > .env / os.environ > hardcoded defaults.
    """
    _KEYS = (
        "FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_AGENT_NAME",
        "RESOURCE_TENANT_ID", "FOUNDRY_AUTH_MODE",
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
    cfg.setdefault("FOUNDRY_AGENT_NAME", "project-analysis-agent")
    cfg.setdefault("FOUNDRY_AUTH_MODE", "browser")
    return cfg


def _get_credential(tenant_id: str, auth_mode: str):
    """
    Return a credential targeting the RESOURCE tenant.
    Shares the persistent token cache with search_foundryiq so the user
    only needs to authenticate once per session.
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
    # Also store back on search_foundryiq so both share the same instance
    try:
        from . import search_foundryiq as sq
        sq._cached_credential = cred
    except Exception:
        pass

    return cred


def handle(arguments: dict, *, on_progress=None, **kwargs) -> str:
    """Call the Foundry agent using the AIProjectClient Responses API."""
    question = arguments["question"]
    cfg = _load_config()

    endpoint = cfg["FOUNDRY_PROJECT_ENDPOINT"]
    agent_name = cfg["FOUNDRY_AGENT_NAME"]
    tenant_id = cfg["RESOURCE_TENANT_ID"]
    auth_mode = cfg["FOUNDRY_AUTH_MODE"]

    if not endpoint:
        return (
            "Error: FOUNDRY_PROJECT_ENDPOINT is not configured. "
            "Add it to hub_config.json or your .env file."
        )
    if not tenant_id:
        return (
            "Error: RESOURCE_TENANT_ID is not configured. "
            "This is the tenant ID where the Foundry project lives "
            "(the guest/resource tenant, not your corp tenant)."
        )

    logger.info("[FabricAgent] Querying agent '%s': %s", agent_name, question[:150])
    if on_progress:
        # Send the full question — UI tooltip / Progress tab show it in full.
        on_progress("tool", f"Querying Fabric Data Agent: {question}")

    try:
        from azure.ai.projects import AIProjectClient

        credential = _get_credential(tenant_id, auth_mode)

        # Create the Foundry project client targeting the resource tenant
        project = AIProjectClient(
            endpoint=endpoint,
            credential=credential,
        )

        # Get an OpenAI-compatible client from the project (v2 API)
        openai_client = project.get_openai_client()

        # Call the agent using the Responses API with agent_reference
        # This routes the request to the named Foundry agent, which in turn
        # invokes the Fabric Data Agent tool with OBO identity passthrough.
        response = openai_client.responses.create(
            input=question,
            extra_body={
                "agent_reference": {
                    "name": agent_name,
                    "type": "agent_reference",
                }
            },
        )

        result = response.output_text
        logger.info("[FabricAgent] Response received (%d chars)", len(result))
        if on_progress:
            on_progress("tool", f"Fabric Agent responded ({len(result)} chars)")

        return result

    except ImportError:
        return (
            "Error: azure-ai-projects is not installed. "
            "Run: pip install azure-ai-projects"
        )
    except Exception as e:
        error_str = str(e)
        logger.error("[FabricAgent] Error: %s", error_str, exc_info=True)

        # Provide actionable guidance for common errors
        if "401" in error_str or "Unauthorized" in error_str.lower():
            return (
                f"Fabric Agent authentication failed (401 Unauthorized). "
                f"Check RESOURCE_TENANT_ID ({tenant_id}) and ensure your account "
                f"has been granted access to the Foundry project in that tenant. "
                f"If using 'cli' mode, run: az login --tenant {tenant_id} first."
            )
        if "403" in error_str or "Forbidden" in error_str.lower():
            return (
                f"Fabric Agent access denied (403 Forbidden). "
                f"Ensure your guest account has been assigned the 'Azure AI User' "
                f"role on the Foundry project '{endpoint}'."
            )
        return f"Fabric Agent error: {e}"
