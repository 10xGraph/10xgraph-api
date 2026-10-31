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
  "redis": "${REDIS_URL}",
  "token_revocation": true,
  "remote_tools": [],
  "rate_limit": {
    "enabled": true,
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
- `redis`: Redis URL (supports `${ENV_VAR}`) of the API's cache store, a `RedisCacheStore`. Unset falls back to `REDIS_URL`, then to an in-memory store per process.
- `cache_store`: optional `module:attr` of your own `BaseCacheStore` (an instance or zero-argument factory); wins over `redis`. The cache store holds everything the API shares between workers: run locks and caps, idempotency records, the ownership cache, token revocation and rate-limit counters.
- `token_revocation`: `true`, or `{"enabled": true, "token_lifetime": 3600, "user_ttl": 2592000}`. Revocations live in the cache store; fails closed (503) when it is unreachable. The old string values (`"redis"`, `"memory"`, `module:attr`) are config errors.
- `rate_limit`: optional sliding-window rate limiter config object; omit or set to `null` to disable. See `references/rate-limiting.md` for the full field reference.

## Startup check and readiness

Before serving, the server calls `aping()` on the checkpointer, the store and the cache store
(`STARTUP_CHECKS=false` skips it) and refuses to start while one stays unreachable for
`STARTUP_CHECK_TIMEOUT_SECONDS`. The cache store counts only when a fail-closed feature uses it
(`token_revocation`, or `rate_limit` with `fail_open: false`); otherwise an unreachable one is
one warning and the server starts with per-process locks and caps. `GET /health/ready` reports
`checkpointer`, `store` and `cache` with the same rule.

## Loading Order

1. Read `10xgraph.json`.
2. Load `.env` when configured.
3. Import the compiled graph from `agent`.
4. Attach `remote_tools`, then import and bind `store`, `injectq`, `thread_name_generator`, the cache store, and `authorization` when configured.
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
