# API Configuration

Use this when changing `10xgraph.json`, dependency loading, app startup, or graph import behavior.

## `10xgraph.json`

Minimal:

```json
{
  "agent": "graph.react:app"
}
```

Common full shape:

```json
{
  "agent": "graph.react:app",
  "store": "graph.dependencies:my_store",
  "injectq": "graph.dependencies:container",
  "thread_name_generator": "graph.thread_name_generator:MyNameGenerator",
  "authorization": "graph.auth:my_authorization_backend",
  "env": ".env",
  "auth": "jwt",
  "remote_tools": [],
  "rate_limit": {
    "enabled": true,
    "backend": "memory",
    "requests": 100,
    "window": 60,
    "by": "ip",
    "exclude_paths": ["/ping", "/docs", "/redocs", "/openapi.json"]
  }
}
```

## Fields

- `agent`: required import path to a compiled graph variable, in `module.path:attribute` format.
- `checkpointer`: not applied by the server (a warning is logged when it is set). Pass the checkpointer to `graph.compile(checkpointer=...)` in your agent module.
- `store`: optional import path to a `BaseStore` instance; required for store endpoints.
- `injectq`: optional import path to an `InjectQ` container.
- `thread_name_generator`: optional import path to a thread-name generator class/instance.
- `authorization`: `null` (mode default: `ownership` in production, `allow_all` in dev), a built-in name (`"ownership"` | `"allow_all"`/`"default"`/`"none"`), an RBAC config object (`{"backend": "rbac", "roles": {...}, "default_scopes": [...], "isolation": "owner"}`), or a `module:attr` import path to a custom `AuthorizationBackend`. See `references/auth-and-authorization.md`.
- `env`: optional `.env` path loaded before graph import.
- `auth`: `null`, `"jwt"`, or `{"method": "custom", "path": "module:backend"}`.
- `remote_tools`: validated client-executed tool schemas, attached once at startup.
- `rate_limit`: optional sliding-window rate limiter config object; omit or set to `null` to disable. See `references/rate-limiting.md` for the full field reference.

## Loading Order

1. Read `10xgraph.json`.
2. Load `.env` when configured.
3. Import the compiled graph from `agent`.
4. Attach `remote_tools`, then import and bind `store`, `injectq`, `thread_name_generator`, and `authorization` when configured.
5. Configure auth.
6. Start FastAPI routes and services.

## Rules

- Keep graph modules importable from the project root.
- Keep `agent` pointing to a compiled graph object, not an uncompiled `StateGraph`.
- Keep dependency modules side-effect light.
- Load secrets through `.env` or process environment, not committed config.
- Validate import paths early and surface clear CLI/API errors.

## Source Map

- Graph config: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/config/graph_config.py
- Loader: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/loader.py
- App startup: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/main.py
- Docs: https://10xgraph.com/
- Rate limit config details: `references/rate-limiting.md`
