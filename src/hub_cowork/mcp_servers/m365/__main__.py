"""MCP server: m365 — Word docs, OneDrive sharing, ACS email, speaker resolution."""
from hub_cowork.mcp_servers._runtime import serve

if __name__ == "__main__":
    serve("m365", [
        "hub_cowork.mcp_servers.m365.tools.create_word_doc",
        "hub_cowork.mcp_servers.m365.tools.resolve_speakers",
        "hub_cowork.mcp_servers.m365.tools.send_email",
    ])
