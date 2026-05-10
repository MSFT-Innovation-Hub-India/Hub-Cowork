"""Per-skill MCP server for the rfp_evaluation skill."""
from hub_cowork.mcp_servers._runtime import serve

if __name__ == "__main__":
    serve("rfp_evaluation", [
        "hub_cowork.skills.rfp_evaluation.mcp_server.tools.create_calendar_reminder",
        "hub_cowork.skills.rfp_evaluation.mcp_server.tools.create_rfp_brief_doc",
        "hub_cowork.skills.rfp_evaluation.mcp_server.tools.query_fabric_agent",
        "hub_cowork.skills.rfp_evaluation.mcp_server.tools.search_foundryiq",
        "hub_cowork.skills.rfp_evaluation.mcp_server.tools.share_onedrive_document",
    ])
