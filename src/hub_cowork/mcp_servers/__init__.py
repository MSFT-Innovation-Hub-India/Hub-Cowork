"""hub_cowork MCP servers (stdio).

Each subpackage is a self-contained MCP server. They are launched as
subprocesses by `core.mcp_client_pool.MCPClientPool` and wrap the
existing tool implementations under `hub_cowork.tools` and
`hub_cowork.skills.<name>.tools`.

Per SKILLS_DESIGN_PRINCIPLES.md §11: a tool is an MCP tool. The
in-process loader in `agent_core` is being phased out in favour of
pool-driven dispatch.
"""
