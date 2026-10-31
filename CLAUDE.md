# 10xgraph-api (API server + CLI) — Engineering Guide

This file documents the **API server and CLI package** only (`10xgraph-api`). For the
core framework see `agentflow/CLAUDE.md`; for the TS client, docs, or playground see their folders;
for the monorepo overview see the workspace-root `CLAUDE.md`.

- Package name (PyPI): `10xgraph-api` (formerly `10xscale-agentflow-cli`, last release 0.6.x)
- Version: `1.0.0rc4` (`pyproject.toml`; 0.7.0 was the first release under the 10xGraph name). `CLI_VERSION`
  and `tenxgraph_api.__version__` are single-sourced from the installed distribution metadata
  (falling back to `pyproject.toml` only for a non-installed source checkout).
- Requires: Python >= 3.12 · Status: `5 - Production/Stable`
- Console entry points: `10xgraph = tenxgraph_api.cli.main:main`, plus the deprecated alias
  `agentflow = tenxgraph_api.cli.main:legacy_main` (prints a notice to stderr; removed in 2.0)
- Depends on the core framework: `10xgraph>=1.0.0rc3,<2.0` (import `tenxgraph`). Must not be
  installed alongside the old `10xscale-agentflow`: both provide an `agentflow` module.

## Rename compatibility (kept until 2.0)

| Old name | New name | How the old one keeps working |
|---|---|---|
| import `agentflow_cli` | `tenxgraph_api` | `agentflow_cli/__init__.py` is a meta-path alias (same module objects, one `DeprecationWarning`) |
| command `agentflow` | `10xgraph` | second console script, `legacy_main` |
| `agentflow.json` | `10xgraph.json` | `cli/core/config.py` (`config_names`, `resolve_default_config`) and `graph_config.default_config_path()` try `10xgraph.json` first in each directory. `10xgraph config` always writes `10xgraph.json`, seeded from `agentflow.json` (`ConfigFileStore(seed_path=...)`) |
| `AGENTFLOW_NO_FULLSCREEN` / `_NO_SPINNER` / `_ASCII` | `TENXGRAPH_*` | `capabilities.cli_env_name()` |
| WS subprotocol `agentflow-bearer` | `10xgraph-bearer` | both accepted in `core/auth/permissions.py` |
| media URL `agentflow://media/` | `graph://media/` | core `tenxgraph.utils.media_scheme` helpers; both are ownership-checked |

Deliberately unchanged: error code `AGENTFLOW_VALIDATION_ERROR`, the `agentflow.cli/v1` JSON
output schema id, `AF-*` CLI error codes.

## What this package is

It turns a 10xGraph `CompiledGraph` into a production FastAPI service, plus a Typer CLI to
scaffold, run, build, test, and evaluate that service. You write a graph, point `10xgraph.json`
at it, and `10xgraph dev` serves it over REST + WebSocket with auth, rate limiting, media
handling, checkpointer/thread management, and a memory store API.

## Package layout

Importable package: `tenxgraph_api/`. Two halves:

| Path | What lives there |
|---|---|
| `tenxgraph_api/cli/` | The Typer CLI. `main.py` (command definitions), `commands/` (one class per command: api, build, eval, init, skills, test, version), `core/` (config, output, validation), `constants.py`, `templates/` (project scaffolds: `dev/` minimal, `prod/` full) |
| `tenxgraph_api/src/app/` | The FastAPI app. `main.py` + `loader.py` (build app from `10xgraph.json`), `routers/` (graph, checkpointer, store, media, ping, ag_ui), `core/auth/`, `core/config/`, `core/middleware/` (rate_limit, security_headers, request_limits), `tasks/`, `utils/`, `worker.py` |

Public exports from the package root (`from tenxgraph_api import ...`): `BaseAuth`,
`SnowFlakeIdGenerator`, `ThreadNameGenerator`.

## CLI commands (verified against `cli/main.py`)

| Command | Purpose | Notable options |
|---|---|---|
| `10xgraph dev` | The only server command: local Uvicorn dev server (reload on) via `commands/api.py::APICommand`, opens the hosted playground when ready. `api` and `play` were removed (use `dev --no-open` / `dev`); production runs Gunicorn in the `10xgraph build` image | `--config/-c` (default `10xgraph.json`), `--host/-H`, `--port/-p` (8000), `--reload/--no-reload`, `--open/--no-open`, `-v/-q` |
| `10xgraph init` | Interactively scaffold a project (guided prompts pick dev vs production, auth, rate limit) | `--path/-p`, `--force/-f`, `--name`, `--template` (quick-start\|production), `--auth` (none\|jwt\|custom), `--rate-limit` (none\|memory\|redis: `memory` = rate limit on, no `redis` key; `redis` = rate limit on plus `"redis": "${REDIS_URL}"` and, with auth, `"token_revocation": true`), `--provider` (google\|openai\|anthropic), `--websocket/--no-websocket` (default off; writes `websocket.enabled`). Prod `pyproject.toml` depends on `10xgraph[<provider>]` and `10xgraph-api[jwt,redis,websocket]` as chosen, `--yes/-y`, `--non-interactive`, `--dry-run`. There is **no `--prod` flag** |
| `10xgraph build` | Generate a `Dockerfile` (and optionally `docker-compose.yml` / `k8s.yaml`). The builder stage strips CLI assets, runs `tenxgraph_api/_slim_image.py` (uninstalls typer/rich/questionary and what only they need, unless another package needs them) and removes pip, so the image carries no CLI code paths | `--output/-o`, `--force/-f`, `--python-version` (3.13), `--port`, `--docker-compose/--no-docker-compose`, `--k8s/--no-k8s`, `--service-name` |
| `10xgraph eval` | Run agent evaluations; discovers `*_eval.py`/`eval_*.py`, runs cases (optionally `--parallel`), writes JSON and HTML reports to `eval_reports/` and opens the HTML report in the browser (only at an interactive terminal: skipped with `--no-open`, `--no-report`, `-q`, json/jsonl, `--non-interactive`, `CI`, non-TTY; `evaluation.open_report` in `10xgraph.json` sets the default). Simulations print per-turn step lines via `ProgressRun.step` | `--output/-o`, `--no-report`, `--threshold/-t`, `--parallel/-p`, `--max-concurrency/-c`, `--open/--no-open` |
| `10xgraph test` | Run project tests via pytest (args after `--` forwarded verbatim) | `--coverage/-C`, `--html`, `-k`, path arg |
| `10xgraph skills` | Install the bundled 10xGraph skill (Agent Skills spec) for Codex/Claude/GitHub, or validate skills | `--agent/-a`, `--path/-p`, `--force/-f`, `--all`, `--list/-l`, `--validate PATH` |
| `10xgraph version` | Show CLI + core framework version | both resolve from installed distribution metadata |
| `10xgraph audit` | Read-only audit of the interpreter, installed CLI/core packages, evaluation-API compatibility, `10xgraph.json`, and the default port. Exits `1` on failure, `0` on warnings only, so it works as a CI gate | `-v/--verbose`, `-q/--quiet` |
| `10xgraph demo` | Preview the animation/timeline/progress themes with no side effects (`Diagnostics` help panel) | `--style` (all\|typing\|network\|init\|build\|eval) |
| `10xgraph config` | Browser editor for `10xgraph.json` (`Manage` panel). Loopback-only stdlib HTTP server in `cli/config_editor/` (`schema.py` lists every key, `validation.py` reuses the `graph_config` parsers, `store.py` does conflict-checked atomic writes with a `.bak`). The page is a Preact + Tailwind app whose source lives in `config-editor-ui/`; `npm run build` there writes the committed `static/app.js` and `static/app.css`, so rebuild after editing `config-editor-ui/src` | `--config/-c`, `--port/-p` (0 = any free port), `--open/--no-open` |

Defaults (from `cli/constants.py`): `DEFAULT_HOST="127.0.0.1"`, `DEFAULT_PORT=8000`,
`DEFAULT_CONFIG_FILE="10xgraph.json"`.

Root options apply to every command and are resolved in `main.root`:
`--format` (human\|plain\|json\|jsonl), `--json`, `--color` (auto\|always\|never),
`--no-color`, `--progress` (auto\|tty\|plain\|json\|quiet),
`--animation/--no-animation`, `--fullscreen/--no-fullscreen`, `--cwd`, `-v/--verbose`
(counted), `-q/--quiet`, `--debug`, `-y/--yes`, `--non-interactive`, `-V/--version`.
The intro (`cli/core/animations.py`) plays by default on a temporary screen, then prints
the header into scrollback; any key skips it (`_skip_on_keypress`, POSIX cbreak) and
Ctrl+C quits. The pinned full-screen frame (`cli/core/screen.py`) is opt-in:
`--fullscreen` or `TENXGRAPH_FULLSCREEN=1` (`TENXGRAPH_NO_FULLSCREEN=1` forces it off).
Ctrl+C releases the frame without the "Press Enter" pause. Colors live in
`cli/core/theme.py`, taken from the docs tokens and the logo (ink, amber `AMBER`,
accent `ACCENT`).

## `10xgraph.json` (the config contract)

Parsed by `tenxgraph_api/src/app/core/config/graph_config.py`. Supported keys:

| Key | Meaning |
|---|---|
| `agent` (required) | `"module:attribute"` resolving to a `CompiledGraph`. The loader accepts a `CompiledGraph` object, a sync/async factory returning one, or a callable. |
| `env` | Path to a `.env` file, loaded at config-load time |
| `thread_name_generator` | `"module:attr"` -> a `ThreadNameGenerator` |
| `auth` | `null`, the string `"jwt"`, or `{"method": "custom", "path": "module:attr"}` |
| `authorization` | `"module:attr"` -> an `AuthorizationBackend` (RBAC / per-tool access) |
| `checkpointer` | `"module:attr"` -> a `BaseCheckpointer` |
| `injectq` | `"module:attr"` -> an InjectQ container |
| `store` | `"module:attr"` -> a `BaseStore` |
| `redis` | Redis URL (`$VAR` / `${VAR}` expanded; unset variable is a config error) of the API's cache store, a `RedisCacheStore` (also Valkey, Dragonfly, KeyDB). Unset falls back to `REDIS_URL`, then `InMemoryCacheStore` (per process) |
| `cache_store` | `"module:attr"` -> a `BaseCacheStore` instance or zero-arg factory (`core/cache_store/`); wins over `redis` / `REDIS_URL`. Selected and bound at boot by `core/cache_store/loader.py::load_and_bind_cache_store` (one INFO line `Cache store: <class> (<source>, <endpoint>)`), closed once in the lifespan. Holds the run locks and caps, idempotency, ownership cache L2, token revocation and rate-limit counters |
| `rate_limit` | Object (see below) |
| `ag_ui` | `{"enabled": bool}`, default off. Mounts `POST /v1/ag-ui` (AG-UI protocol, for CopilotKit and other AG-UI clients). Needs the `ag-ui` extra |
| `websocket` | `enabled` (`false` leaves out `/v1/graph/ws` and `/v1/graph/live`; `true` without `websockets`/`wsproto` refuses to boot; unset keeps them and warns if no library; `routers/setup_router.py::_graph_router`), `max_connections` (1000), `max_connections_per_user` (10), `realtime_models` (models a `/v1/graph/live` client may pick), `realtime_client_overrides` (init options a client may set; default `voice`, `modalities`, `vad`; add `system_prompt` only for trusted callers) |
| `runs` | `RunsConfig`: `thread_ids` (`server` default = only existing ids may be named on REST invoke/stream, `/v1/graph/ws` and checkpointer writes, a new thread is started by omitting the id; enforced by the ownership/rbac backends via `OwnershipAuthorizationBackend(server_thread_ids=...)`. AG-UI and `/v1/graph/live` are exempt: they pass `client_thread_ids=True` to `authorize` because their frontends make their own ids, and the caller owns a new one. `client` = any request may name a new thread, explicit opt-in), `config_keys` / `state_keys` (allow-lists for request config and `initial_state`/PUT state; 422 on other keys; `thread_id`, `run_id` always allowed), `max_concurrent_per_user` (HTTP invoke/stream/AG-UI runs in flight, default 10, 429 over it; `routers/graph/run_limits.py`), `timeout` (seconds; invoke 504, streams end with an error chunk), `max_parallel_tool_calls` (positive int, default unset = core's default 10 or env `TENXGRAPH_MAX_PARALLEL_TOOL_CALLS`; set into every run's config in `_prepare_input`, reserved from clients) |
| `token_revocation` | `true`, or `{"enabled", "token_lifetime", "user_ttl"}` (`graph_config.TokenRevocationConfig`); the old strings (`"redis"`, `"memory"`, `module:attr`) are config errors naming `cache_store`. Revocations live in the cache store (`core/auth/revocation.py`: `revoke_token`, `revoke_user`, `is_revoked`), checked by `RequirePermission` (401), before every `/v1/graph/ws` run, and before and every 30 s during a `/v1/graph/live` session (`REALTIME_RECHECK_INTERVAL`, close 1008); cache store errors fail closed (503), and an unreachable cache store refuses the boot |
| `media` | `delete_with_thread` (default true): delete an uploaded file when the last message or thread that references it is deleted. Links are recorded in the file's `threads` metadata (`MediaService.link_threads` / `unlink_threads`); files without a `threads` key (uploaded before link tracking) and stores without `update_metadata` (core older than the release that added it) are never auto-deleted. `false` still records links. Parsed by `graph_config.MediaConfig` |
| `graph_introspection` | `"open"` or `"scoped"` (default scoped in production): `GET /v1/graph`, `/v1/graph/tools`, `/v1/graph:StateSchema` need an explicit `graph:read` scope (`RequirePermission(..., introspection=True)`) |

`rate_limit` object: `enabled`, `requests` (default 100), `window` secs (60), `by` (`ip` |
`user` | `global`), `trusted_proxy_headers` (honour
`X-Forwarded-For` only when true), `trusted_proxies` (networks the header is honoured from;
empty means loopback and private networks only, `DEFAULT_TRUSTED_PROXIES`), `trusted_proxy_hops`,
`exclude_paths`, `fail_open` (on a cache store error: allow vs deny with 503
`RATE_LIMIT_UNAVAILABLE`; default true). Counters live in the cache store
(`core/middleware/rate_limit/limiter.py::check`). `backend` and `redis` were removed in rc4;
either key is a config error naming `cache_store`.

## Auth

- `"auth": "jwt"` requires `JWT_SECRET_KEY` and `JWT_ALGORITHM` in the environment (raises at
  load if missing). JWT logic lives in `core/auth/jwt_auth.py`.
- `"auth": {"method": "custom", "path": "module:attr"}` loads your `BaseAuth` subclass
  (`from tenxgraph_api import BaseAuth`).
- Authorization (RBAC / object-level) is separate: `core/auth/authorization.py`
  (`AuthorizationBackend` / `DefaultAuthorizationBackend` / `OwnershipAuthorizationBackend`),
  wired via the `authorization` key. That key accepts `"module:attr"` (custom), a built-in
  name (`"ownership"` = owner-only thread access; `"allow_all"`/`"default"`/`"none"`), or
  `null`. When unset it is `ownership` whenever auth is configured or `MODE=production`, and
  `allow_all` only with auth off in development (no identity, so nothing to authorize).
  Selection lives in `loader._resolve_authorization_backend`; `RequirePermission` passes
  `resource_id` (thread_id from path or body) to `authorize`.
- **Isolation defaults to owner.** `AuthorizationBackend.isolation_scope()` returns `"owner"`, so a
  custom backend with only `authorize()` still scopes threads and memories per user; return
  `"none"` on purpose to share (`DefaultAuthorizationBackend` / `allow_all` does).
- **Token claims are validated.** `JwtAuth` requires `user_id` to be a non-empty string or an int
  (stored as a string), else 401. `token_revoked` checks `jti` even with an odd `user_id`.
  Scopes come from `authorization.scopes_from_claims`: the `scope` claim (space-separated) and
  `scopes` (list or space-separated string) merge into exact names, checked with `in` (no
  substrings); a wrong-typed claim is a 401 (`ScopeClaimError`), no claim means unrestricted.
- **Ownership fails closed.** No checkpointer, or one whose `aget_thread_owner` raises
  `NotImplementedError`, denies thread requests; with auth on the loader refuses to boot in that
  setup (`authorization.assert_ownership_supported`). A thread with no owner allows only
  `CREATE_ACTIONS` (`graph:invoke`, `graph:stream`, `checkpointer:write`, `checkpointer:import`); checkpointer state and
  message writes claim the thread first (`CheckpointerService._claim_thread`). Every saved
  thread carries its owner via `request_config.owned_thread_info` (the in-memory and SQLite
  checkpointers read the owner from `ThreadInfo.user_id`).
- **Ownership is object-level and enforced on every thread-touching step** (invoke/stream/
  stop/fix + all checkpointer read/write/delete): a thread is accessible only to its owner;
  a foreign `invoke`/`stream` is rejected up front (403), never reaching the graph.
- **Scalable, not a DB call per request.** Ownership is immutable, so it is cached by
  `core/auth/ownership_resolver.py::ThreadOwnershipResolver`: in-process LRU (L1) + the
  cache store as L2 (skipped when it is in-memory). The backing lookup is
  `BaseCheckpointer.aget_thread_owner` (implemented in pg/in-memory/sqlite; base raises
  `NotImplementedError`). Cache is evicted on thread delete (`CheckpointerService.delete_thread`),
  which also leaves a tombstone for one L1 TTL (L1 + cache store) so the deleted id cannot be
  recreated by someone else while another worker still caches the old owner
  (`ThreadOwnershipResolver.recently_deleted`); the resolver never closes the cache store.
- **Identity reuse:** with `rate_limit.by == "user"` the limiter's verified identity is reused by
  `RequirePermission` for backends with `BaseAuth.reusable_within_request = True` (`JwtAuth`),
  see `core/auth/identity_cache.py`. Look up the app container inside a request with
  `core/utils/container.py::current_container()`, not `InjectQ.get_instance()`.
- **Secure by construction:** `core/auth/route_guard.py::assert_all_routes_protected` runs at
  boot (`main.py` after `init_routes`) and refuses to start if any non-public route lacks a
  `RequirePermission` guard (`/ping` and `/health/ready` are the only public paths).

## HTTP + WebSocket surface (all under `/v1` except ping)

- **Graph** (`tags=["Graph"]`): `POST /v1/graph/invoke`, `POST /v1/graph/stream`,
  `POST /v1/graph/stop`, `POST /v1/graph/fix`, `GET /v1/graph`,
  `WS /v1/graph/ws`.
- **Checkpointer / threads**: `GET/POST /v1/threads`, `GET/DELETE /v1/threads/{thread_id}`,
  `GET/PUT/DELETE /v1/threads/{thread_id}/state`, `GET /v1/threads/{thread_id}/messages`,
  `GET/DELETE ... /messages/{message_id}`. `POST /v1/threads/{thread_id}/messages` (import
  history, any role) is admin-only: `RequirePermission("checkpointer", "import",
  explicit_scope=True)`, so the caller must hold `checkpointer:import` explicitly (no scopes
  means 403; `"*"` roles get it, `authorization.CHECKPOINTER_ADMIN_SCOPES`). Tool calls, media
  ownership and size limits still apply. `PUT ... /messages/{message_id}` sets
  `metadata["feedback"]` only (body `{"feedback": {...}}`, 8 KB, depth 10, `checkpointer:write`;
  calls `aupdate_message_metadata`, 501 when the checkpointer lacks it, 404 for a missing message).
- **Client input rules** (`core/auth/request_config.py`): `context_summary` and `execution_meta`
  are never client-writable (`reject_builtin_state_keys`, 422); client messages are user-only
  (`client_message_error`: no `assistant`/`system`; a `tool` message only as tool results, which
  must answer a pending remote call, `GraphService._check_tool_results`, also for PUT state
  `context`); the server replaces client `message_id`s (`with_fresh_message_ids`, in the schema
  validators; AG-UI validates with `context={KEEP_MESSAGE_IDS: True}` to keep its ids);
  `current_node` must be a real node and state values must match the state model's types (422).
  Every external media URL in client input (`media.kind="url"`, not `graph://media/` or
  `data:`; `gs://` included) is run through the graph's `media_policy.check_url` before the run
  (`multimodal_preprocessor.check_external_media_urls`, via `GraphService.check_media_urls`,
  also called by `CheckpointerService` for `POST messages` and `PUT state`): 400 with the policy
  message, `MediaURLBlockedError`. `resolve_media_policy` uses `compiled_graph.media_policy`,
  else a default `MediaFetchPolicy`, else skips (core without it). The policy lives in core;
  the API has no config for it.
- **Per-thread run lock** (`routers/graph/thread_lock.py`): invoke, stream, each WS run and
  AG-UI take a one-slot lease `runs:thread:<thread id>` from `core/leases.py::LeaseManager` over the cache store (shared
  with a Redis cache store, else per process; 60 s lease renewed while alive) before the graph starts, and
  release it in `finally` (error, cancel, disconnect via `release_when_done`). Routers lock when
  the request names a thread (streaming routes before the response headers, so the 409 is
  real); `GraphService` locks a thread it creates (`_lock_new_thread`). Busy: wait up to
  `THREAD_BUSY_WAIT_SECONDS` (30), then 409 "thread is busy" in the standard error envelope (WS: error chunk
  `data.reason = "thread is busy"`, socket stays open). Request option `on_busy`
  (`"wait"` | `"cancel"`, top level next to `recursion_limit`; not on AG-UI): `cancel` calls
  `GraphService.stop_graph` while waiting. `/v1/graph/live` takes the lock for the session (and a run slot) too, refused with a fatal `thread_busy` / `too_many_runs` error event and close 1013. Every entry point takes lock + run slot through `routers/graph/run_guard.py::acquire_run_guard`. A cache store error falls back to a per-process count; a refresh that finds its lease gone sets `Lease.lost` and takes it again.
- **Production error chunks:** `graph_service.sanitize_error_chunk` (called in
  `stream_chunks`, so stream, WS and AG-UI share it) replaces `data.reason` / `data.error` /
  `data.message` and `state.execution_meta.internal_data["error"]` of `ERROR` chunks with the
  generic `_sanitize_error_message` text when `MODE=production`. Core's
  `... internal error (reference <id>).` tool text is kept.
- **Store (memory)**: `POST /v1/store/memories`, `/v1/store/memories/list`,
  `/v1/store/memories/forget`, `/v1/store/memories/{memory_id}`, `POST /v1/store/search`.
- **Media / files** (`tags=["Files"]`): `POST /v1/files/upload` (first bytes must match the
  declared type for formats with a signature, else 415; table in `utils/media/signatures.py`),
  `GET /v1/files/{file_id}`, `/{file_id}/info`, `/{file_id}/url`, `DELETE /v1/files/{file_id}`
  (owner only, 404 otherwise, scope `files:delete`, added in `authorization.FILES_DELETE_SCOPES`
  until core lists it), `GET /v1/config/multimodal`. Deleting a message or thread also releases
  the files it referenced (`CheckpointerService.delete_message` / `delete_thread`).
- **AG-UI** (`tags=["AG-UI"]`, only when `ag_ui.enabled`): `POST /v1/ag-ui`. `routers/ag_ui/`:
  `converter.py` (RunAgentInput -> new user/tool messages only; the checkpoint is the record),
  `event_mapper.py` (StreamChunk -> AG-UI events), `service.py` (runs `GraphService.stream_chunks`).
  `RUN_FINISHED` carries an `outcome` only for `interrupt()` pauses (CopilotKit's pinned
  `@ag-ui/core` 0.0.59 rejects the newer `success`/`cancelled` outcome shapes). Browser tools
  from `RunAgentInput.tools` go to the graph as the server-owned `remote_tools` run-config key
  (via `GraphService.stream_chunks(..., server_config=...)`); `RunAgentInput.resume` becomes the
  graph `resume` input. `GraphInputSchema.resume` also resumes over `/v1/graph/invoke|stream`.
- **Ping**: `GET /ping` (liveness). **Readiness**: `GET /health/ready` (`aping()` on the
  bound checkpointer, store and cache store; keys `checkpointer`, `store`, `cache`; 200 or 503
  with only ok/fail per component; a failed `cache` counts only when `token_revocation` is on
  or `rate_limit.fail_open` is false; `routers/ping/readiness.py`). The startup check
  (`routers/ping/startup_checks.py`, `STARTUP_CHECKS`) applies the same rule at boot: required
  components retry for `STARTUP_CHECK_TIMEOUT_SECONDS` then refuse the boot; an optional
  unreachable cache store is one WARNING and the server starts.

Routers are wired in `routers/setup_router.py` (`init_routes`). The `a2a.py` / `a2ui.py` stubs
were removed in the 0.5.0 readiness pass; they were fully commented out and never mounted.

## Settings / environment

`core/config/settings.py` is a `pydantic-settings` `Settings` (with `extra="allow"`, so unknown
env vars are tolerated). Notable vars: `APP_NAME`, `APP_VERSION`, `MODE` (`production` | `prod` |
`development` | `dev` | `local`, case-insensitive, normalised to `production` or `development`;
anything else refuses to start, unset is `development`), `LOG_LEVEL`, `MAX_REQUEST_SIZE` (10MB default), security headers
(`SECURITY_HEADERS_ENABLED`, `HSTS_*`, `FRAME_OPTIONS`, `CSP_POLICY`, ...), `ORIGINS` (CORS,
default `*` with a wildcard warning), `ALLOWED_HOST`, `ROOT_PATH`/`DOCS_PATH`/`REDOCS_PATH`,
`REDIS_URL`, `SENTRY_DSN`, `SNOWFLAKE_*` (epoch/node/worker/bit layout), `JWT_SECRET_KEY`/
`JWT_ALGORITHM`, `OTEL_ENABLED`/`OTEL_SERVICE_NAME`/`OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_LEVEL`,
`STRICT_PRODUCTION_CHECKS` (production refuses wildcard `ALLOWED_HOST`/`ORIGINS` or enabled
docs). Debug follows `MODE` (on in development, off in production); a leftover `IS_DEBUG` is ignored with a warning. Media: `MEDIA_REQUIRE_OWNER` defaults on in
production. In production: set `MODE=production`, a non-`*` `ORIGINS`, and a strong
`JWT_SECRET_KEY`; see `SECURITY.md` for the full checklist.

Middleware is pure ASGI. Order, outermost first: CORS, request id, security headers, trusted
host, rate limit, request size, gzip (`setup_middleware.py`). Run and WebSocket connection caps
are leases in the cache store (`core/leases.py`): shared with a Redis cache store, per process
with the in-memory one.
Production error responses hide only 5xx details; raise `request_config.ClientInputError` for
422 messages that must survive production.

## Optional extras (`pyproject.toml`)

`sentry`, `firebase`, `snowflakekit`, `redis`, `jwt`, `ag-ui` (AG-UI endpoint), `media` (document text extraction via
`textxtract`), `images` (Pillow via `10xgraph[images]`, for image resize / EXIF / format conversion), `websocket` (`websockets`, needed to serve `/v1/graph/ws` and `/v1/graph/live`), `gcloud` (Cloud Logging), `otel` (includes FastAPI instrumentation + OTLP exporter).

## Development workflow

```bash
# from this folder (10xgraph-api/); a .venv is present
.venv/bin/python -m pytest                 # tests in tests/
10xgraph init                             # scaffold (interactive)
10xgraph dev                              # dev server on 127.0.0.1:8000 + hosted playground
10xgraph dev --no-open                    # dev server only
10xgraph build --docker-compose           # Dockerfile + compose
ruff check . && ruff format .
```

- Tests in `tests/`; `pytest` config and ruff/bandit are in `pyproject.toml`. Templates under
  `cli/templates/{dev,prod}` are excluded from lint/type/bandit (they are emitted code, not lib).
- The `prod` template is the reference for a real project: it scaffolds `graph/` (agent, state,
  tools, validators, thread_name_generator), `auth/`, `evals/`, and `tests/`.

## Known doc drift (do not trust without checking)

- **Version is now single-sourced.** `CLI_VERSION` (and `tenxgraph_api.__version__`, which aliases
  it) resolve from installed distribution metadata. `10xgraph version` prints the CLI version and
  the installed core `10xgraph` version; the old `pyproject.toml` path read - which
  printed `unknown` from a wheel - is gone.
- **There is no `agentflow doctor`.** The environment check shipped as `10xgraph audit`; earlier
  README/CHANGELOG copy called it `doctor`. Anything still saying `doctor` is stale.
- There is no `docs/` directory in this package. The README links to the docs site
  (`10xgraph.com/docs/...`) instead; keep it that way.
- **a2a / a2ui routers no longer exist.** Don't document a2a HTTP endpoints as live; restore the
  files from git history if that surface is actually built.
- The local git remote is still `Iamsdt/pyagenity-api.git`; GitHub redirects it to
  `10xGraph/10xgraph-api`, so pushes work. Repoint it when convenient.
- The docs site (`agentflow-docs/`) still uses `10xgraph api` / `10xgraph play` in many pages;
  both commands were removed in favour of `10xgraph dev` (`dev --no-open` for server only).
