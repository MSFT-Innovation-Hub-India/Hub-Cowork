"""MCP server: utility — hub config, progress logging, task-status query.

These are cross-cutting utility tools every skill may need.
"""
from hub_cowork.mcp_servers._runtime import serve

if __name__ == "__main__":
    serve("utility", [
        "hub_cowork.mcp_servers.utility.tools.log_progress",
        "hub_cowork.mcp_servers.utility.tools.get_hub_config",
        "hub_cowork.mcp_servers.utility.tools.get_task_status",
    ])
