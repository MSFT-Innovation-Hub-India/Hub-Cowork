"""Per-skill MCP server for the meeting_invites skill."""
from hub_cowork.mcp_servers._runtime import serve

if __name__ == "__main__":
    serve("meeting_invites", [
        "hub_cowork.skills.meeting_invites.mcp_server.tools.create_meeting_invites",
    ])
