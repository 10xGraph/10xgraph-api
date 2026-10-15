# Remote Tools

Use remote tools for browser or client-owned capabilities while the Python graph runs on the API server.

## Registration

Declare the trusted schema in `10xgraph.json`:

```json
{
  "remote_tools": [
    {
      "node": "TOOLS",
      "name": "read_browser_state",
      "description": "Read selected browser state.",
      "parameters": {"type": "object", "properties": {}, "required": []}
    }
  ]
}
```

The API validates and attaches these schemas during startup. `node_name` is accepted as an alias for `node`; other unknown keys fail validation.

Register only the implementation in TypeScript:

```typescript
client.registerToolHandler("read_browser_state", async (args) => {
  return readBrowserState(args);
});
```

No setup endpoint exists. A request cannot add, rename, or replace graph tools.

## Execution Flow

1. Model requests a configured remote tool.
2. `ToolNode` returns a `RemoteToolCallBlock` instead of executing locally.
3. TypeScript `invoke` or `stream` runs the matching handler.
4. Client sends a `ToolResultBlock`; graph execution continues.

## Rules

- Schema name and client handler name must match exactly.
- `node` must identify a `ToolNode` in the graph.
- Remote tool names must be globally unique.
- Parameters must be an object JSON Schema.
- Handler results must be serializable.
- Use local Python or MCP tools for server-owned execution.

## Source Map

- Config model: `tenxgraph_api/src/app/core/config/graph_config.py`
- Startup attachment: `tenxgraph_api/src/app/loader.py`
- Python remote handling: `tenxgraph/core/graph/tool_node/base.py`
- TypeScript executor: https://github.com/10xGraph/10xgraph-client/blob/master/src/tools.ts
