"""Per-skill MCP server for the shelf_watch skill.

The `_compare`, `_discover`, `_memory`, `_report`, `_session` modules
are private helpers (the legacy underscore convention skipped them in
the in-process loader). Only `shelf_watch_run` is the public tool.
"""
from hub_cowork.mcp_servers._runtime import serve

if __name__ == "__main__":
    serve("shelf_watch", [
        "hub_cowork.skills.shelf_watch.mcp_server.tools.shelf_watch_run",
    ])
