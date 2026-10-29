# Dependency Injection

Use this when wiring services into nodes/tools, configuring API dependencies, or removing hidden globals.

## Automatic Injection

10xGraph injects these parameters by name/type conventions when they appear in node or tool signatures:

- `state`: current `AgentState` or subclass.
- `config`: execution config such as `thread_id`, `user_id`, and `run_id`.
- `tool_call_id`: current tool call ID, tools only.

Injected parameters are hidden from the model-facing tool schema.

## Service Injection

Use `injectq` for application services:

```python
from injectq import Inject, InjectQ

container = InjectQ.get_instance()
container.bind_instance(MyService, service)

def tool(query: str, service: MyService = Inject[MyService]) -> str:
    """Search with MyService."""
    return service.search(query)
```

You can bind singleton instances, string keys, or factories. Pass a container to `StateGraph(container=container)` when using a non-default container.

## API Configuration

In `10xgraph.json`, use import paths to wire dependencies into the server runtime:

```json
{
  "agent": "graph.react:app",
  "store": "graph.dependencies:store",
  "injectq": "graph.dependencies:container"
}
```

The API loader imports these and binds them for graph execution. The checkpointer is not set here: pass it to `graph.compile(checkpointer=...)` in your agent module (a `checkpointer` key in `10xgraph.json` is ignored and logs a warning).

## Rules

- Prefer `InjectQ` over module-level globals for databases, stores, callbacks, and custom services.
- Keep user-provided tool arguments model-visible; keep runtime context injected.
- Configure shared services at graph/server boundaries, not deep inside individual tools.

## Source Map

- Docs: https://10xgraph.com/
- API loader: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/loader.py
- Graph config: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/config/graph_config.py
- Tool/node call helpers: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/utils/callable_utils.py
