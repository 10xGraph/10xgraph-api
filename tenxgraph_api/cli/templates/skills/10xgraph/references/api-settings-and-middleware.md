# API Settings and Middleware

Use this when changing environment variables, app settings, middleware, CORS, request limits, security headers, docs paths, or Sentry.

## Settings

`Settings` reads environment variables through Pydantic settings.

Important variables:

- `APP_NAME`
- `APP_VERSION`
- `MODE`
- `LOG_LEVEL`
- `MAX_REQUEST_SIZE`
- `ORIGINS`
- `ALLOWED_HOST`
- `ROOT_PATH`
- `DOCS_PATH`
- `REDOCS_PATH`
- `REDIS_SOCKET_TIMEOUT`, `REDIS_CONNECT_TIMEOUT`, `REDIS_MAX_CONNECTIONS`, `REDIS_KEY_PREFIX`
  (the Redis client of a `cache_store` type `redis`; its URL is `cache_store.url`)
- `STARTUP_CHECKS`
- `STARTUP_CHECK_TIMEOUT_SECONDS`
- `SENTRY_DSN`
- `JWT_SECRET_KEY`
- `JWT_ALGORITHM`

Security header variables:

- `SECURITY_HEADERS_ENABLED`
- `HSTS_ENABLED`
- `HSTS_MAX_AGE`
- `HSTS_INCLUDE_SUBDOMAINS`
- `HSTS_PRELOAD`
- `FRAME_OPTIONS`
- `CONTENT_TYPE_OPTIONS`
- `XSS_PROTECTION`
- `REFERRER_POLICY`
- `PERMISSIONS_POLICY`
- `CSP_POLICY`

## Middleware

Active middleware areas:

- CORS and host handling.
- Request size limits.
- Security headers.
- Request ID assignment.
- Selective gzip behavior; streaming endpoints should avoid gzip buffering when configured.
- Worker middleware where used by deployment.
- Rate limiting: sliding-window limiter controlled by `cache_store.rate_limit` in
  `10xgraph.json`. Counters live in the cache store: in-process with `"type": "memory"` (the
  default), shared with `"type": "redis"` (requires `pip install "10xgraph-api[redis]"`) for
  multi-worker or multi-instance deployments. Excluded paths, identity mode (`ip`, `user` or
  `global`), and `fail_open` behavior are all configurable.
  See `references/rate-limiting.md` for the full option reference.

## Production Warnings

Production mode warns about unsafe defaults such as the ones below, and refuses to start on
wildcard `ORIGINS`/`ALLOWED_HOST` and enabled docs because `STRICT_PRODUCTION_CHECKS` is on by
default in production (`STRICT_PRODUCTION_CHECKS=false` downgrades them to warnings):

- `ORIGINS=*`
- debug enabled
- docs endpoints enabled
- `ALLOWED_HOST=*`

## Startup Checks

In every mode, the server refuses to start while a configured backend is unreachable: the
checkpointer and the store (each through its `aping()`), and the cache store from
`cache_store` when a fail-closed feature needs it (`token_revocation` with auth, or
`rate_limit` with `fail_open: false`; otherwise an unreachable cache store is one warning). It
retries for `STARTUP_CHECK_TIMEOUT_SECONDS` (30) first, then exits with each failed component,
its `host:port` and the error. `STARTUP_CHECKS=false` skips the check.

## Sentry

`SENTRY_DSN` enables Sentry setup through the API config module. Keep error reporting optional and safe when unset.

## Rules

- Keep environment variable docs in sync with `Settings`.
- Use `ROOT_PATH` when serving behind a reverse proxy subpath.
- Disable or protect docs paths in production when needed.
- Do not gzip SSE streams.
- Sanitize logs for tokens, secrets, and large payloads.

## Source Map

- Settings: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/config/settings.py
- Middleware setup: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/config/setup_middleware.py
- Request limits: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/middleware/request_limits.py
- Security headers: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/middleware/security_headers.py
- Rate limit middleware: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/core/middleware/rate_limit
- Rate limit base class: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/middleware/rate_limit/base.py
- Sentry: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/config/sentry_config.py
- Log sanitizer: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/utils/log_sanitizer.py
- Docs: https://10xgraph.com/
- Rate limiting docs: https://github.com/10xGraph/10xgraph-api/blob/main/docs/rate-limiting.md
