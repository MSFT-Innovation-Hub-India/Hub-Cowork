"""MCP server: workiq — Microsoft 365 data via the WorkIQ CLI."""
from hub_cowork.mcp_servers._runtime import serve

if __name__ == "__main__":
    serve("workiq", ["hub_cowork.mcp_servers.workiq.tools.query_workiq"])
