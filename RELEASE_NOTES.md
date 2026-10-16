# 10xgraph-api 0.8.0

A security and scalability pass over the API, following an external review. Authorization now
fails closed, defaults are stricter, and run and connection caps are shared across workers when
Redis is configured.

This release has **breaking changes**. Read "Before you upgrade" first.

```bash
pip install -U "10xgraph-api==0.8.0"
```

## Before you upgrade

- **Authorization defaults to `ownership` whenever auth is on**, in every mode.
  - Before, it was `allow_all` unless `MODE=production`.
  - To keep the old development behaviour, set `"authorization": "allow_all"`.
- **Ownership checks fail closed.**
  - With auth on, the server refuses to start if your checkpointer cannot resolve thread owners (`aget_thread_owner` not implemented).
  - Implement it, or choose `allow_all` or a custom backend.
- **Unowned threads only allow creating actions.**
  - Reading, deleting, stopping or fixing a thread that doesn't exist now answers 403.
  - Before, these returned an empty result.
- **`gunicorn` is no longer a dependency.**
  - Images built with `10xgraph build` install it themselves.
  - If you run gunicorn yourself, install it alongside the API: `pip install gunicorn uvicorn-worker`.
  - The worker class is now `uvicorn_worker.UvicornWorker`. Regenerate with `10xgraph build --force`, or update `-k`.
- **`POST /v1/graph/fix` returns `state` as a JSON object**, not a JSON-encoded string. Drop any `JSON.parse`/`json.loads` on that field.
- **`/v1/graph/invoke` no longer returns `meta.thread_name`.** The name is generated in the background and appears on the thread (`GET /v1/threads`).
- **Realtime init frames can no longer replace the system prompt** unless you add `"system_prompt"` to `websocket.realtime_client_overrides`.
- **`trusted_proxy_headers: true` now trusts only loopback and private networks.** List your proxies in `trusted_proxies`.
- **Production defaults are stricter:**
  - `MEDIA_REQUIRE_OWNER` is on.
  - `graph_introspection` is `scoped`, so the graph structure, tools and state schema need the `graph:read` scope.
  - `IS_DEBUG` is forced off.
- **Production 4xx responses keep their message**; only 5xx responses are made generic.
- **Generated Dockerfiles install only the dependencies you list.** `10xgraph build` warns if your requirements file or `pyproject.toml` does not list `10xgraph-api`.

## Security

- **Log injection:** values a client controls (ids, paths, rate-limit keys, run config, messages) are logged through `log_safe()`, so a request can't forge log lines.
- **ReDoS fix:** the JWT detection pattern in the log sanitizer now runs in linear time on crafted input.
- **Token revocation:** the new `token_revocation` key refuses revoked tokens before they expire, on HTTP and on every WebSocket run.
- **Size limit for WebSocket frames:** JSON frames over `MAX_REQUEST_SIZE` are refused.
- **Strict production checks:** `STRICT_PRODUCTION_CHECKS=true` refuses to start on wildcard `ALLOWED_HOST`/`ORIGINS` or enabled API docs.
- **Example fixes:** `examples/security/` no longer prints key or secret fragments, and API keys are hashed with HMAC-SHA-256 and `API_KEY_PEPPER`.

## New

- **`runs` key:**
  - `thread_ids` (`client` | `server`).
  - `config_keys` and `state_keys` allow-lists.
  - `max_concurrent_per_user`, default 10.
  - `timeout`.
- **Readiness probe:** `GET /health/ready` checks the checkpointer, Redis and the store. The k8s manifest uses it.
- **Shared caps:** run and WebSocket connection caps are shared across workers through Redis when it is configured.
- **Lighter images:** Dockerfiles are built in two stages, so the runtime image ships only your code and a virtualenv: no compiler, no curl.
- **Dependency changes:**
  - JSON responses are serialized by pydantic-core.
  - `orjson` and `platformdirs` are dropped.
  - The `uvicorn` floor is 0.36.

See `CHANGELOG.md` for the full list.
