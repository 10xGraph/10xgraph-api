# 10xgraph-api 1.0.0rc3

The third 1.0 release candidate: the `1.0.0rc2` code plus a startup check for every configured backend, a clear 503 when the rate limiter's backend is down, and a CORS fix so the hosted playground can reach a local `10xgraph dev` server.

```bash
pip install -U --pre "10xgraph-api==1.0.0rc3"
```

This pulls `10xgraph` 1.0.0rc3. `10xgraph-client` stays at 1.0.0-rc.2 (no client changes).

## Before you upgrade (changes in rc3)

- **The server refuses to start while a configured backend is unreachable**, in development and production. It checks the checkpointer's Postgres (query and schema version) and Redis, the memory store (`aping()`, now required in `10xgraph` 1.0.0rc3), the API's Redis and the rate limiter's Redis. It retries for `STARTUP_CHECK_TIMEOUT_SECONDS` (default 30), then exits naming each failed backend and its `host:port` (no credentials). Set `STARTUP_CHECKS=false` to skip it.
- **A rate-limiter backend outage answers 503, not 429.** With `fail_open: false` and Redis unreachable, requests get `503 RATE_LIMIT_UNAVAILABLE` with `Retry-After: 5`. A real limit hit is still `429 RATE_LIMIT_EXCEEDED`. Retry on 503 as well as 429.
- **Running `10xgraph dev` on your machine against Docker backends:** publish Postgres, Redis and Qdrant on `127.0.0.1` and use `localhost` in `.env`. The compose service names only resolve inside the Docker network, and the startup check now reports this at boot.

## What changed in rc3

- The hosted playground (`https://play.10xgraph.com`) can reach a local `10xgraph dev` server: CORS answers the browser's private-network preflight for origins in `ORIGINS` (before, it answered 400 and the browser reported a CORS error). Chrome also asks the user to allow access to the local machine; the playground now shows the steps.
- A custom rate-limit backend can signal an outage with `RateLimitDecision.backend_unavailable()`.
- The Redis rate-limit backend logs one traceback per outage instead of one per request.

See [CHANGELOG.md](CHANGELOG.md) for the full list. The rc2 notes follow.

---

## 10xgraph-api 1.0.0rc2

The second 1.0 release candidate: it is the `1.0.0rc1` code plus graceful shutdown, stricter request validation, 409 answers for writes during a run, idempotency keys, JWT key rotation and a redesigned eval and config experience.

**This is a release candidate.** Pip skips it unless you pass `--pre`. It requires `10xgraph` 1.0.0rc2, and its version is aligned with core `10xgraph` and `10xgraph-client` (1.0.0-rc.2). This release has **breaking changes**. Read "Before you upgrade" first.

```bash
pip install -U --pre "10xgraph-api==1.0.0rc2"
```

This pulls `10xgraph` 1.0.0rc2.

## Before you upgrade (changes in rc2)

If you are on `1.0.0rc1`, this section is all that is new. If you are coming from 0.9.x, read it and then "Upgrading from 0.9.x" below.

- **AG-UI and `/v1/graph/live` now follow `runs.thread_ids` like REST.** Under the default `"server"`, a thread id the server never issued gets 403 on `POST /v1/ag-ui`, and an error event (`not_authorized`, close code 1008) on live. A blank or `"new"` id, or none, makes the server create the thread: AG-UI returns it in `RUN_STARTED`, and live sends `{"type": "session_started", "thread_id": "<id>"}` first. Frontends that make their own ids (CopilotKit does by default) must set `"runs": {"thread_ids": "client"}` in `10xgraph.json`.
- **Ids must match `^[A-Za-z0-9._:@-]{1,128}$`** (`thread_id`, `message_id`, memory ids, WebSocket and AG-UI thread ids). Others get 422 `VALIDATION_ERROR`. Snowflake integers and UUIDs fit.
- **Nested `initial_state`, `config` and `resume` deeper than 64 levels get 422**, and client dicts and lists are size-capped (1000 keys per object, 10000 items per list, 200000 values in all).
- **Resume errors answer 409 or 422.** A wrong interrupt id, a resume sent to a thread that is not paused, or a plain message sent to a thread paused at `interrupt()` returns 409 (before the stream starts on `/v1/graph/stream`). A resume value that fails the interrupt's `response_schema` returns 422.
- **Writes during a run return 409.** `PUT`/`DELETE` thread state, `POST` messages, `DELETE` of a message or thread, and `POST /v1/graph/fix` are refused with 409 while a run holds the thread. Retry after the run ends or stop it first.
- **Thread delete can return `success=false`** with `data.cleanup_failed` (`files`, `traces`, `cache`) when a cleanup step failed. The thread rows are deleted; treat it as "retry the cleanup".
- **`JWT_ALGORITHM` must be on an allowlist** (HS/RS/ES/PS 256/384/512 and `EdDSA`), PyJWT must be installed when `"auth": "jwt"`, and `auth` must be exactly `"jwt"`.
- **Other error and format changes:** the `graph:setup` scope is gone; the duplicate `AGENTFLOW_VALIDATION_ERROR` code is no longer sent (use `VALIDATION_ERROR`); a recursion limit answers 422 `GRAPH_RECURSION_LIMIT`, not 500; `POST /v1/graph/stream` is labelled `application/x-ndjson` and sends a blank keep-alive line every 15 s (update proxy rules that matched `text/event-stream`).
- **Uploads:** images over 50,000,000 pixels and PDFs over 10 MB or 500 pages get 413.
- **`10xgraph build`** refuses `--docker-compose` together with `--k8s`; run it once per target.
- **`IS_DEBUG` is removed.** Debug follows `MODE`. The `/v1/evals/runs` endpoints are removed, and `10xgraph eval` writes the JSON report (and now an HTML report, see below) with no `--open` flag.
- **Postgres:** `10xgraph` 1.0 runs schema migration v4 on first use. Stop all old pods, run the migration (one new pod), then start the rest. A rollback needs a database restore. `/health/ready` fails while the schema is older than the code expects.
- **Dependency minimums:** `python-multipart>=0.0.31`, `starlette>=1.3.1`, `PyJWT>=2.15.0` (`jwt` extra), `opentelemetry-api` and `opentelemetry-sdk` `>=1.45.1` (`otel` extra). The core floor is `10xgraph>=1.0.0rc2,<2.0`.
- **Gunicorn and shutdown:** the generated command uses the worker class `tenxgraph_api.src.app.uvicorn_worker.TenxGraphUvicornWorker`. Regenerate Docker and Kubernetes files with `10xgraph build`, or point your own command at it. Keep Gunicorn `--graceful-timeout` above the sum of the three `SHUTDOWN_*_SECONDS` settings (50 s by default) and the pod grace period above that.
- **Alerting:** handled client errors log one WARNING line, not an ERROR with a traceback. Alerts that matched ERROR for 4xx must match WARNING.

## What changed in rc2

**Shutdown and reliability**
- Graceful shutdown: `SHUTDOWN_GRACE_SECONDS` (10), `SHUTDOWN_STOP_SECONDS` (10) and `SHUTDOWN_TIMEOUT_SECONDS` (30). Cut runs are saved INTERRUPTED and the thread resumes on the next request. While closing, runs answer 503 `SERVICE_UNAVAILABLE` with `Retry-After`, `/health/ready` reports `draining`, and an open HTTP stream ends with a chunk that has `data.interrupted: true`.
- HTTP run streams end with `{"event": "updates", "data": {"status": "done"}}`, so a client can tell a finished stream from a cut one.
- `Retry-After` on 429 and 409, typed error envelopes for unhandled errors, 503 for database or Redis connection errors, and 429 `PROVIDER_RATE_LIMITED` for model provider rate limits.
- Redis timeouts and pool settings (`REDIS_SOCKET_TIMEOUT` and others) and `REDIS_KEY_PREFIX` for deployments that share a Redis. Leases and revocation use the Redis clock.
- A stored thread at a missing node answers 409 (`GRAPH_NODE_MISSING_001`); state from a newer `10xgraph` answers 500 (`STATE_FORMAT_001`).
- Fixes: stop and shutdown now reach a REST stream after its first chunk, a disconnected stream frees its thread only after the run is saved, each realtime connection gets its own `LiveAgent`, and resuming a thread paused at `interrupt()` works on Postgres.

**New and opt-in**
- Idempotency keys (`Idempotency-Key` header, or `idempotency_key` in the WebSocket run frame) so a gateway retry does not run tools twice.
- JWT key rotation (`JWT_SECRET_KEYS`) and a maximum token age (`JWT_MAX_TOKEN_AGE_SECONDS`).
- `10xgraph revoke` revokes one token or all of a user's tokens without a restart.
- Media limits (per-user files and storage, image pixels, PDF size and pages, inline media size), `GET /v1/files`, paging fields on lists, a run trace span tree, and `realtime_options` in graph details.
- `10xgraph eval` writes an HTML scorecard next to the JSON report and opens it (use `--no-open` to skip), prints live progress lines, and `--threshold` now exits 1 only when the pass rate is below it.
- `10xgraph init --template production` scaffolds JWT, ownership authorization, Redis rate limiting and a `PgCheckpointer`; `10xgraph build`, `10xgraph config` and `10xgraph audit` warn about missing auth, authorization or rate limiting. The `10xgraph config` editor is redesigned.

**Security**
- Ownership checks on `file_id` media blocks for message and state writes, percent-encoded Redis keys, secrets redacted from logs and validation errors, and uploads authenticated before the body is parsed.

## Upgrading from 0.9.x (changes since rc1 also apply)

Everything below was the `1.0.0rc1` guidance and still applies when you jump from 0.9.x to rc2. Where rc2 changes an rc1 item, the rc2 section above wins.

### Breaking changes from 0.9.x

- **`runs.thread_ids` defaults to `"server"`** (was `"client"`).
  - REST and WebSocket clients that invent a thread id for a new thread now get 403 on `/v1/graph/invoke`, `/v1/graph/stream`, `/v1/graph/ws` and the checkpointer write routes.
  - Omit the id on the first request and use the `meta.thread_id` the server returns from then on (`"new"` over `/v1/graph/ws`), or set `"runs": {"thread_ids": "client"}` in `10xgraph.json`.
  - AG-UI and `/v1/graph/live` need no change in rc1, but rc2 enforces this setting there too. Only the built-in ownership and rbac backends refuse.
- **Production deployments with the default or wildcard `ALLOWED_HOST` or `ORIGINS` refuse to start.**
  - Set explicit values, or `STRICT_PRODUCTION_CHECKS=false`.
  - A public, token-less API that runs in production with `ORIGINS=*` and `CORS_ALLOW_CREDENTIALS=false` also needs the opt-out.
- **Production runs longer than 300 s are cancelled** (invoke 504, stream and AG-UI error). Raise `runs.timeout`, or set it to `null`.
- **An unknown `MODE` now refuses to start.** Use one of `production`, `prod`, `development`, `dev`, `local` (case-insensitive). Values such as `staging` or `test` fail at startup, and in `10xgraph dev`.
- **Requests over the size limits get 422:** 500 messages, 100 blocks per message, 1,000,000 characters of text. On AG-UI the limits apply to the trailing turn, with a 10,000-message cap on history.
- **`POST /v1/threads/{thread_id}/messages` is now admin-only.**
  - It needs the `checkpointer:import` scope held explicitly: an identity with no scopes gets 403, and a `"*"` role has it.
  - Send normal messages through invoke or stream, and use the new `PUT .../messages/{message_id}` for feedback.
- **Assistant and system messages, and `context_summary` / `execution_meta`, are rejected with 422** on the client routes.
- **Custom authorization backends are isolated by default.** A backend that implements only `authorize()` now gets per-owner storage isolation. If you relied on sharing, add `def isolation_scope(self) -> str: return "none"`.
- **JWTs with an invalid `user_id` or scope claim are refused with 401.**
  - `user_id` must be a non-empty string or an integer. Null, empty, boolean, float, list and dict are rejected.
  - A `scope` / `scopes` claim that is not a string or a list of strings is rejected. Fix the issuer.
  - A string `scopes` is now split on spaces instead of matched by substring.

### New

- **`observability.user_id_mode`** in `10xgraph.json` (`"raw"` default, `"hash"`, `"mask"`) controls how the user id appears in traces.
- **`on_busy` request option** (`"wait"` default, or `"cancel"`) on invoke, stream and `WS /v1/graph/ws`. With `"cancel"`, a run that finds its thread busy asks the running run to stop, then runs.
- **Uploaded files live and die with the messages that use them.** Deleting the last message or the thread that uses a file deletes the file. `media.delete_with_thread` (default `true`) turns the automatic delete off.
- **`DELETE /v1/files/{file_id}`** deletes a file you own, behind the new `files:delete` scope.
- **`PUT /v1/threads/{thread_id}/messages/{message_id}`** sets feedback on a message.
- **`interrupt_id` on invoke, stream and WebSocket input**, so a resume can name the interrupt it answers. WebSocket input also accepts `resume` again.
- **`RequirePermission(..., explicit_scope=True)`** and the `checkpointer:import` scope.

### Security

- Handled client errors log one WARNING line instead of an ERROR with a traceback, and Sentry no longer sends request bodies. Alerts that matched ERROR for 4xx must match WARNING instead.
- AG-UI context, tools and forwarded props are server config, and `ag_ui` is a reserved key that other routes drop.
- Built-in state fields (`context_summary`, `execution_meta`) are never client-writable (422; AG-UI drops them), client messages are user messages only, forged tool results are refused, and the server generates message ids on invoke, stream, WebSocket and `PUT state` (AG-UI keeps client ids).
- Thread ids are server-made by default (`runs.thread_ids: "server"`; operators can set `"client"`), so nobody can squat an id or probe whether one exists. AG-UI and `/v1/graph/live` still accept frontend-made ids.
- A client can no longer pick the vector store collection on a graph run: `collection` is a server-owned config key and is ignored if sent.
- Clients can no longer set `file_tool_root`, `workspace_root`, `tool_timeout` or `node_timeout` in request `config`; set them in your graph instead.
- `/v1/graph/live` re-checks the token during the session, and HTTP routes take the bearer token only from the `Authorization` header.
- One run at a time per thread, and external media URLs, upload types and document text extraction are checked or bounded.
- Production hides raw error text on state, stream, WebSocket and AG-UI, and OTEL no longer prints spans to stdout.
- Generated deployment files are locked down by default, and `10xgraph init` writes a `.gitignore` that keeps `.env` and credentials out of git.

### Fixed

- Client mistakes on thread routes are a 404 or 422, not a 500, and state and message responses no longer carry provider or framework internals.
- Development error responses for node and graph errors are no longer a 422 `VALIDATION_ERROR`.
- `POST /v1/graph/fix` no longer deletes healthy tool-call messages.

See `CHANGELOG.md` for the full list.
