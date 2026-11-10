## 10xGraph

This project is built on 10xGraph (`10xgraph`, `10xgraph-api`). Before you write or change agent,
graph, tool or `10xgraph.json` code, load the `10xgraph` skill ({skill_path}) and read the
reference file it points to. Tell every subagent you start to do the same.

- Start from a prebuilt agent: `ReactAgent` for a tool-calling agent. Hand-write a `StateGraph`
  only when no prebuilt agent fits, and say why.
- Build one agent unless the user asks for more. Do not add routers, handoffs, guard nodes or
  approval nodes the user did not ask for.
- Business data and rules belong in tools and the systems they call (APIs, MCP servers,
  databases), not in graph nodes or state.
- MCP: pass the signed-in user with `pass_user_info_to_mcp=True`. The MCP server reads it from
  `ctx.request_context.meta["user"]`. Never add `user`, `user_id`, `org_id` or tenant
  arguments to MCP tools.
- Do not work around 10xGraph bugs in app code (subclassing ToolNode, overriding private
  methods). Report them instead.
