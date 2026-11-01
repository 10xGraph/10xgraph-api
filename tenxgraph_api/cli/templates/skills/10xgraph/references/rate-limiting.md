# Rate Limiting

Use this when adding, configuring, or debugging 10xGraph's built-in sliding-window rate limiter.

## Overview

10xGraph provides a sliding-window rate limiter configured by the `rate_limit` block inside
`cache_store` in `10xgraph.json`. The limiter is off by default; add the block to turn it on.

## Quick Start

```json
{
  "agent": "graph.react:app",
  "cache_store": {
    "rate_limit": {
      "enabled": true,
      "requests": 100,
      "window": 60,
      "by": "ip",
      "exclude_paths": ["/ping", "/docs", "/redocs", "/openapi.json"]
    }
  }
}
```

Each client IP may make `requests` calls every `window` seconds.

## Where the counters live

The counters live in the API's cache store (`BaseCacheStore`), the same one that holds the run
locks and caps, idempotency records, the ownership cache and token revocation. There is no
separate rate-limit backend. `cache_store.type` picks the store once at boot:

1. `"memory"`, or no `type`: `InMemoryCacheStore`, counters per process, fine for one worker.
2. `"redis"` with `"url"` (supports `${ENV_VAR}`): `RedisCacheStore` (also Valkey, Dragonfly,
   KeyDB). Needs `pip install "10xgraph-api[redis]"`.
3. `"custom"` with `"path": "module:attr"`: your own `BaseCacheStore`.

`REDIS_URL` alone selects nothing; write `"url": "${REDIS_URL}"` to use it.

Shared counters across workers, containers or servers:

```json
{
  "agent": "graph.react:app",
  "cache_store": {
    "type": "redis",
    "url": "${REDIS_URL}",
    "rate_limit": {
      "enabled": true,
      "requests": 1000,
      "window": 60,
      "by": "ip",
      "trusted_proxy_headers": true,
      "exclude_paths": ["/ping", "/metrics", "/docs", "/redocs", "/openapi.json"],
      "fail_open": true
    }
  }
}
```

Check and record are one atomic store operation (a Lua script on Redis), so concurrent requests
cannot race past the limit. A key not in the table below is a config error listing the valid
keys, and a top-level `rate_limit` (outside `cache_store`) is a config error too.

## Configuration Reference

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Enables the middleware when the `cache_store.rate_limit` block exists. |
| `requests` | integer | `100` | Maximum requests allowed in each window. |
| `window` | integer | `60` | Window size in seconds. |
| `by` | string | `"ip"` | Limit by client IP (`"ip"`), verified user (`"user"`) or one shared quota (`"global"`). |
| `exclude_paths` | string array | `[]` | Paths that bypass rate limiting entirely. |
| `trusted_proxy_headers` | boolean | `false` | Use `X-Forwarded-For` as the client IP (only behind a trusted proxy). |
| `fail_open` | boolean | `true` | On cache store errors, allow (`true`) or deny (`false`, 503) requests. With `false` the server also refuses to start while the cache store is unreachable. |

## Identity Modes

Per-IP limit (most public APIs):

```json
{ "cache_store": { "rate_limit": { "requests": 100, "window": 60, "by": "ip" } } }
```

Global limit (one shared quota for the whole service):

```json
{ "cache_store": { "rate_limit": { "requests": 5000, "window": 60, "by": "global" } } }
```

Only enable `trusted_proxy_headers` when your app sits behind a trusted proxy that strips
untrusted `X-Forwarded-For` headers from direct clients.

## Response Headers

Every response includes:

| Header | Description |
| --- | --- |
| `X-RateLimit-Limit` | Configured request limit. |
| `X-RateLimit-Remaining` | Requests remaining in the current window. |
| `X-RateLimit-Reset` | Unix timestamp for the window reset estimate. |
| `X-RateLimit-Reset-After` | Seconds until the window reset estimate. |
| `Retry-After` | Present on `429` and `503` responses only. |

When the limit is exceeded, 10xGraph returns `429 Too Many Requests`:

```json
{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Too many requests. Limit: 100 per 60s. Retry after 12s.",
    "limit": 100,
    "window_seconds": 60,
    "retry_after_seconds": 12
  },
  "metadata": {
    "request_id": "request-id",
    "status": "error"
  }
}
```

When the cache store cannot be reached and `fail_open` is `false`, the request is denied with
`503 Service Unavailable` and `Retry-After: 5`, not 429, so clients can tell an outage from a
limit hit:

```json
{
  "error": {
    "code": "RATE_LIMIT_UNAVAILABLE",
    "message": "Rate limiter unavailable: its backend could not be reached.",
    "retry_after_seconds": 5
  },
  "metadata": {
    "request_id": "request-id",
    "status": "error"
  }
}
```

## Choosing a Cache Store

| Scenario | Cache store |
| --- | --- |
| Local development, tests, demos | `"type": "memory"` (or no `type`) |
| Single-process production | `"type": "memory"` |
| Gunicorn/Uvicorn with multiple workers | `"type": "redis"`, `"url": "${REDIS_URL}"` |
| Docker / Kubernetes (multiple replicas) | `"type": "redis"`, `"url": "${REDIS_URL}"` |
| Storage that does not speak Redis | `"type": "custom"`, `"path": "module:attr"` (your own `BaseCacheStore`) |

## Rules

- Do not enable `trusted_proxy_headers` unless a reverse proxy strips untrusted forwarding headers.
- Always add health-check and observability paths to `exclude_paths`.
- Set `fail_open: false` only when hard enforcement is required; otherwise `true` prevents availability issues during cache store outages.
- A Redis cache store requires the `redis` extra: `pip install "10xgraph-api[redis]"`.
- `cache_store.url` supports `${ENV_VAR}` expansion; keep secrets out of committed config.

## Source Map

- Middleware: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/core/middleware/rate_limit
- Limiter on the cache store: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/middleware/rate_limit/limiter.py
- Cache store: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/core/cache_store
- Rate-limit config model: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/config/graph_config.py
- Middleware setup: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/config/setup_middleware.py
- Docs: https://github.com/10xGraph/10xgraph-api/blob/main/docs/rate-limiting.md
- Configuration reference: https://github.com/10xGraph/10xgraph-api/blob/main/docs/configuration.md, "Rate Limiting" section
