from hub_cowork.core import agent_core

print("Skills:", sorted(agent_core._skills.keys()))
print()
for name in sorted(agent_core._skills.keys()):
    s = agent_core._skills[name]
    print(f"  {name}: {len(s.tools)} tools  servers={s.mcp_servers}  allowlist={s.tool_allowlist}")
    for t in s.tools:
        n = t["name"]
        print(f"      - {n}  (server={s.server_for_tool(n)})")
print()
print("Total tools registered:", len(agent_core.TOOL_SCHEMAS))
agent_core.mcp_pool.shutdown()
