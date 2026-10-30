# Rate Limiting

Use this when adding, configuring, or debugging 10xGraph's built-in sliding-window rate limiter.

## Overview

10xGraph provides a sliding-window rate limiter configured via the `rate_limit` block in
`10xgraph.json`. The limiter is disabled by default — add the block to activate it.

## Quick Start

In-memory backend for local development or single-process services:

```json
{
  "agent": "graph.react:app",
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

Each client IP may make `requests` calls every `window` seconds.

## Redis Backend (Production)

Redis stores counters centrally so the limit is enforced across multiple workers,
containers, or servers. Install the optional extra first:

```bash
pip install "10xgraph-api[redis]"
```

```json
{
  "agent": "graph.react:app",
  "rate_limit": {
    "enabled": true,
    "backend": "redis",
    "requests": 1000,
    "window": 60,
    "by": "ip",
    "trusted_proxy_headers": true,
    "exclude_paths": ["/ping", "/metrics", "/docs", "/redocs", "/openapi.json"],
    "redis": {
      "url": "${RATE_LIMIT_REDIS_URL}",
      "prefix": "10xgraph:rate-limit"
    },
    "fail_open": true
  }
}
```

Set the environment variable:

```bash
RATE_LIMIT_REDIS_URL=redis://localhost:6379/0
```

The Redis backend uses an atomic Lua script with sorted sets — check and record happen as a
single Redis operation, which prevents concurrent requests from racing past the limit.

## Configuration Reference

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Enables the middleware when the `rate_limit` block exists. |
| `backend` | string | `"memory"` | `memory`, `redis`, or `custom`. |
| `requests` | integer | `100` | Maximum requests allowed in each window. |
| `window` | integer | `60` | Window size in seconds. |
| `by` | string | `"ip"` | Limit by client IP (`"ip"`) or one shared quota (`"global"`). |
| `exclude_paths` | string array | `[]` | Paths that bypass rate limiting entirely. |
| `trusted_proxy_headers` | boolean | `false` | Use `X-Forwarded-For` as the client IP (only behind a trusted proxy). |
| `redis.url` | string | `null` | Redis URL; required for the Redis backend. Supports `${ENV_VAR}` expansion. |
| `redis.prefix` | string | `"10xgraph:rate-limit"` | Prefix for all Redis keys. |
| `fail_open` | boolean | `true` | On Redis errors, allow (`true`) or deny (`false`, 503) requests. |

## Identity Modes

Per-IP limit (most public APIs):

```json
{ "rate_limit": { "requests": 100, "window": 60, "by": "ip" } }
```

Global limit (one shared quota for the whole service):

```json
{ "rate_limit": { "requests": 5000, "window": 60, "by": "global" } }
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

When the backend cannot be reached and `fail_open` is `false`, the request is denied with
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

## Custom Backend

Implement `BaseRateLimitBackend` and bind the instance through InjectQ, then set
`"backend": "custom"` in `10xgraph.json`.

```python
from tenxgraph_api.src.app.core.middleware.rate_limit import (
    BaseRateLimitBackend,
    RateLimitDecision,
)


class MyRateLimitBackend(BaseRateLimitBackend):
    async def check(self, key: str, *, limit: int, window: int) -> RateLimitDecision:
        allowed = True
        remaining = limit - 1
        reset_after = window
        return RateLimitDecision(
            allowed=allowed,
            remaining=remaining,
            reset_after=reset_after,
        )

    async def close(self) -> None:
        return None
```

When the backend's store cannot be reached and the request must not pass, return
`RateLimitDecision.backend_unavailable()`; the middleware answers it with the 503 above.

```json
{
  "rate_limit": {
    "enabled": true,
    "backend": "custom",
    "requests": 100,
    "window": 60,
    "by": "ip"
  }
}
```

## Choosing a Backend

| Scenario | Backend |
| --- | --- |
| Local development, tests, demos | `memory` |
| Single-process production | `memory` |
| Gunicorn/Uvicorn with multiple workers | `redis` |
| Docker / Kubernetes (multiple replicas) | `redis` |
| Custom storage or quotas | `custom` |

## Rules

- Do not enable `trusted_proxy_headers` unless a reverse proxy strips untrusted forwarding headers.
- Always add health-check and observability paths to `exclude_paths`.
- In production with Redis set `fail_open: false` only when hard enforcement is required; otherwise `true` prevents availability issues during Redis outages.
- The `redis` backend requires the `redis` extra: `pip install "10xgraph-api[redis]"`.
- The `RATE_LIMIT_REDIS_URL` value supports `${ENV_VAR}` expansion — keep secrets out of committed config.

## Source Map

- Middleware: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/core/middleware/rate_limit
- Base class: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/middleware/rate_limit/base.py
- Rate-limit config model: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/config/graph_config.py
- Middleware setup: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/config/setup_middleware.py
- Docs: https://github.com/10xGraph/10xgraph-api/blob/main/docs/rate-limiting.md
- Configuration reference: https://github.com/10xGraph/10xgraph-api/blob/main/docs/configuration.md — "Rate Limiting" section
