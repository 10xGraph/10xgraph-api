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
- `REDIS_URL`
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
- Rate limiting: sliding-window limiter controlled by the `rate_limit` block in `10xgraph.json`.
  Uses an in-process `memory` backend by default; use the `redis` backend (requires
  `pip install "10xgraph-api[redis]"`) for multi-worker or multi-instance deployments.
  Excluded paths, identity mode (`ip` or `global`), and `fail_open` behavior are all configurable.
  See `references/rate-limiting.md` for the full option reference.

## Production Warnings

Production mode warns about unsafe defaults such as the ones below, and refuses to start on
wildcard `ORIGINS`/`ALLOWED_HOST` and enabled docs because `STRICT_PRODUCTION_CHECKS` is on by
default in production (`STRICT_PRODUCTION_CHECKS=false` downgrades them to warnings):

- `ORIGINS=*`
- debug enabled
- docs endpoints enabled
- `ALLOWED_HOST=*`

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
