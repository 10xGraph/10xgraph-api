# Changelog

All notable changes to `10xgraph-api` (formerly `10xscale-agentflow-cli`) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Compatibility policy

- **Nothing public is removed without a deprecation cycle.** A public name (anything
  exported from `tenxgraph_api`, a CLI command or flag, an HTTP route, or a
  `10xgraph.json` key) is first marked deprecated in a release, kept working for at
  least one subsequent minor release, and only then removed in a major release.
- **Moved modules keep a back-compat shim** re-exporting from the new location for at
  least one minor release. Import paths do not break silently.
- **Breaking changes are documented under a `### Breaking` heading** in the release's
  section, with the migration step spelled out.
- **From 1.0, `major.minor` is identical across `10xgraph`, `10xgraph-api` and
  `10xgraph-client`.** Patch numbers move independently. A patch release is only a bug or
  security fix; any new feature means all three packages release the next `X.Y.0` together, and
  a package with no changes ships it as "version alignment, no changes".
- **Breaking changes need a major release (2.0)**, after a deprecation that lasted at least one
  minor release. The deprecated `agentflow` command, `agentflow_cli` import, `agentflow.json`
  fallback and `AGENTFLOW_*` environment variable fallbacks are removed in 2.0.
- Anything under `tenxgraph_api.cli.templates/` is emitted scaffolding, not API; template
  content can change in any release.

---

## [Unreleased]

### Removed

- Removed the `/v1/evals/runs` endpoints; eval reports are JSON files opened in the playground.
  `10xgraph eval` now writes only the JSON report and no longer has `--open`.

### Changed

- **`10xgraph init --template production` scaffolds the recommended production setup.** Auth
  defaults to JWT (with ownership authorization) and the rate limiter to Redis, in both the
  prompts and `--non-interactive` (the rate-limit question is now asked whatever the auth
  answer). `graph/agent.py` builds a `PgCheckpointer` from `DATABASE_URL` and `REDIS_URL`, with
  the in-memory checkpointer left as commented-out code for local testing. `.env.example` always
  has the Postgres and Redis blocks, and `pyproject.toml` depends on `10xgraph[<provider>,pg_checkpoint]`
  and `10xgraph-api[...redis...]`. Quick-start is unchanged.
- **Production-readiness warnings.** `10xgraph config` (the editor and the terminal on start-up)
  and `10xgraph build` now warn, from the JSON alone, when `auth` is off, when `authorization`
  is `allow_all`, `default` or `none` with auth on, and when `rate_limit` is missing or
  disabled. They are warnings only: auth off stays allowed and `build` still exits 0. The
  checks live in `production_readiness_warnings` (`cli/config_editor/validation.py`). The
  `10xgraph init` next steps now point to `10xgraph config` (both templates) and
  `10xgraph build` (production); the unused `InitCommand._print_next_steps` was removed.
- **`10xgraph config` editor redesign.** A sidebar layout with a "Get running" page first, a
  collapsible sidebar (Ctrl/Cmd+B), search across every setting, defaults shown as values with a
  per-field reset, and a read-only reference of the related `.env` variables per section plus a
  server environment page. Schema fixes: `graph_introspection`, `websocket.enabled` and
  `observability.logfire.console` no longer show a misleading default and offer an explicit
  "server default" choice; the websocket section no longer seeds `enabled: true`;
  `runs.timeout` must be greater than 0 and can be set to no limit (`null`); the limit fields
  offer "Unlimited"; `rate_limit.exclude_paths` is described as extra paths (`/ping` and
  `/health/ready` are always skipped) and is no longer seeded; `observability.logfire.console: true`
  is now an error, because Logfire accepts only `false` or a `ConsoleOptions` object.

## [1.0.0rc1] - 2026-10-28

This is the 1.0 release candidate. It requires `10xgraph` 1.0 (`10xgraph>=1.0.0rc1`), and its
`major.minor` is aligned with core `10xgraph` and `10xgraph-client`. Install it with
`pip install -U --pre "10xgraph-api==1.0.0rc1"`.

### Breaking

- **`runs.thread_ids` defaults to `"server"`** (was `"client"`). REST and WebSocket clients that
  invent a thread id for a new thread now get 403 on `/v1/graph/invoke`, `/v1/graph/stream`,
  `/v1/graph/ws` and the checkpointer write routes (`PUT /v1/threads/{id}/state`, message
  writes). Migrate by omitting the id on the first request and using the `meta.thread_id` the
  server returns from then on (`"new"` over `/v1/graph/ws`), or set
  `"runs": {"thread_ids": "client"}` in `10xgraph.json`. AG-UI and `/v1/graph/live` need no change. Only the built-in ownership and
  rbac backends refuse; `allow_all` and custom backends do not unless they read
  `server_thread_ids` themselves.
- **Production deployments with the default or wildcard `ALLOWED_HOST` or `ORIGINS` refuse to
  start.** Set explicit values, or `STRICT_PRODUCTION_CHECKS=false`. A public, token-less API
  that runs in production with `ORIGINS=*` and `CORS_ALLOW_CREDENTIALS=false` also needs the
  opt-out.
- **Production runs longer than 300 s are cancelled** (invoke 504, stream and AG-UI error). Raise
  `runs.timeout`, or set it to `null`.
- **An unknown `MODE` now refuses to start.** Accepted values (case-insensitive):
  `production`, `prod`, `development`, `dev`, `local`. Anything else, such as `staging` or
  `test`, fails at startup (and in `10xgraph dev`) with an error listing them. Set `MODE` to
  one of these values.
- **Requests over the size limits get 422** (500 messages, 100 blocks per message, 1,000,000
  characters of text; on AG-UI, for the trailing turn, with a 10,000-message cap on history).
- **`POST /v1/threads/{thread_id}/messages` is now admin-only.** It needs the
  `checkpointer:import` scope held explicitly: an identity with no scopes gets 403, and a `"*"`
  role has it. It still takes any role, since it imports history, but tool calls, media
  ownership and size limits are still enforced. Normal clients should send messages through
  invoke or stream, and use the new `PUT .../messages/{message_id}` for feedback.
- Assistant and system messages, and `context_summary` / `execution_meta`, are rejected with 422
  on the client routes listed under Security.
- **Custom authorization backends are isolated by default.** A backend that implements only
  `authorize()` now gets per-owner storage isolation (`isolation_scope()` returns `"owner"`),
  where it used to share all threads and memories. If you relied on sharing, add
  `def isolation_scope(self) -> str: return "none"`.
- **JWTs with an invalid `user_id` or scope claim are refused with 401.** `user_id` must be a
  non-empty string or an integer; null, empty, boolean, float, list and dict are rejected, and an
  integer becomes a string. A `scope` / `scopes` claim that is not a string or a list of strings
  is rejected. Fix the issuer. A string `scopes` is now split on spaces instead of matched by
  substring, so tokens that depended on that stop matching.

### Security

- **Handled client errors no longer log at ERROR, and Sentry no longer sends request bodies.**
  The error handlers now log at the level of the status they return: a 4xx (401, 403, 404, 413,
  422, 429 and so on) is one WARNING line with method, path, status and error code, no
  traceback and no body; a 5xx stays ERROR with the traceback. Before, every 404 or bad token
  wrote an ERROR with a full traceback, and Sentry's logging integration turned each into an
  event despite the 5xx-only setting. Sentry now also starts with `max_request_body_size="never"`,
  `include_local_variables=False` and `send_default_pii=False`, and only ERROR logs become
  events (WARNING and INFO stay breadcrumbs). Responses are unchanged. Alerts that matched ERROR
  for 4xx must match WARNING instead.
- **AG-UI data is server config, and `ag_ui` is a reserved key.** The AG-UI endpoint now puts
  `context`, `tools` and `forwarded_props` into the server-owned run config, which is merged after
  the client checks. A `runs.config_keys` allow-list no longer rejects AG-UI runs, and nodes
  still read `config["ag_ui"]` as before. Invoke, stream and `/v1/graph/ws` drop a client-sent
  `config.ag_ui`, so a plain graph request cannot pose as AG-UI context or tools.
- **The memory rate limiter evicts in constant time at its key cap.** Keys are kept in
  least-recently-used order, so a new key at the 50,000-key cap drops the oldest entry from the
  front instead of sweeping and scanning every key (about 2.5 ms of blocked event loop per new
  key before). Idle-key cleanup runs in the background only, pops from the front and stops at
  the first fresh key. The "bucket cap reached" warning is logged at most once a minute.
- **REST state responses hide error text in production.** `GET /v1/threads/{id}/state`
  returned `execution_meta.internal_data.error` with the raw exception text. With
  `MODE=production` it is now the same generic message (keeping the error code) that stream,
  WebSocket and AG-UI error chunks already send. Development is unchanged.
- **Ownership cache keys can no longer collide with delete markers.** Redis owner entries
  now live under `<prefix>:id:<thread_id>` and "just deleted" markers under
  `<prefix>:tomb:<thread_id>`. Before, a thread named `deleted:X` shared a key with the marker
  for `X`, so it could block other users from creating `X` for up to an hour. Old keys are no
  longer read; the cache refills from the database.
- **Client thread ids are escaped in request logs, and not logged in production.** The graph
  observability, stop, fix and WebSocket run lines, and the AG-UI run line, now log the id with
  `log_safe` and are only emitted outside `MODE=production`. The WebSocket authorization-failure
  warnings and the thread-naming warning stay in production but escape the id, so a thread id
  with a newline can no longer forge a log line.
- **Store routes cap free text and forward only known options.** `query` is limited to 8,000
  characters and `content` (store and update, string form) to 100,000; longer input returns
  `422`. The client `options` field now passes only `infer` and `replace_metadata` to the store
  (everything else, `memory_id` included, is dropped) instead of arbitrary keyword arguments.
  With core `aforget_memory` now applying `memory_type`, `category` and `filters`, a narrow
  forget no longer deletes all of the caller's memories; `Mem0Store` rejects narrowing.
- **OTEL no longer prints spans to stdout in production.** With `OTEL_ENABLED=true` and no
  `OTEL_EXPORTER_OTLP_ENDPOINT`, `MODE=production` now sets up no exporter and logs a warning;
  the console exporter is development-only.
- **Generated deployment files are locked down by default.** `10xgraph build` images run as a
  fixed uid 10001 (`USER 10001:10001`). `--k8s` sets a restricted `securityContext` (non-root,
  read-only root filesystem, no privilege escalation, all capabilities dropped,
  `RuntimeDefault` seccomp), turns off the service account token, reads secrets from a
  `<service>-secrets` Secret via `envFrom`, and uses a pinned placeholder image instead of
  `:latest`. `--docker-compose` publishes the port on `127.0.0.1` only, reads `.env` through
  `env_file`, and runs read-only with `cap_drop: ALL` and `no-new-privileges`. `/tmp` and
  `/app/uploads` are the only writable paths in both. `.dockerignore` also excludes private
  keys, certificates and cloud credential files, and an existing `.dockerignore` gets the
  missing patterns appended instead of being left as is. Regenerate with `--force` to pick
  this up; the compose file now expects `ORIGINS` and `ALLOWED_HOST` in `.env`.

- **`10xgraph init` writes a `.gitignore`** that keeps `.env`, keys and credential files out of
  git (`.env.example` stays tracked), plus `uploads/` and `eval_reports/`. An existing
  `.gitignore` is never replaced; only the missing secret patterns are appended.

- **A client can no longer pick the vector store collection on a graph run.** `collection` is now
  a server-owned config key: invoke, stream, `/v1/graph/ws` and AG-UI drop it from client config,
  as the store routes already did, so the memory tools cannot be pointed at another
  application's collection or made to create new ones. The value is ignored, not refused (the
  drop is logged at debug), like every other server-owned key. The developer picks collections
  in code: the store default, `UserMemoryConfig(collection=...)` /
  `AgentMemoryConfig(collection=...)`, or `RAGAgent` store config.

- **Nobody can authenticate as a reserved owner of shared data.** Core keeps shared, ownerless
  data (agent/app memory, RAG knowledge bases) under `system_user_id(name)` (`"__system__:<name>"`,
  one owner per agent). An identity with such a `user_id` would read and write that data through
  the store routes, so the API now refuses any `user_id` starting with `__system__`
  with 401 `This user id is reserved` (WebSocket close 1008) and a warning in the log, for the
  built-in JWT backend and any custom `BaseAuth`. The check sits in `RequirePermission` and
  `verify_current_user`, so it covers HTTP, `/v1/graph/ws`, `/v1/graph/live` (handshake and the
  30 s re-check) and AG-UI. A reserved identity is never stored in or served from the per-request
  identity cache, and the rate limiter does not key on it (it falls back to the address). Auth-off
  development still uses `"anonymous"`. Whitespace around the name is ignored; ints are never equal
  to it. If your identity provider issues this id, rename that user.
- **Thread ids are server-made by default (`runs.thread_ids: "server"`).** The default was
  `"client"`, where the first user to name an id owned it (squatting `"default"` locked out its
  intended owner) and a run on an unknown id answered 200 while a taken one answered 403 (an
  existence oracle). Now REST `invoke`/`stream`, `/v1/graph/ws` and the checkpointer write routes
  accept only ids of threads that exist, with the same 403 for an unknown and a foreign id; a new
  thread is started by omitting the id (or `"new"` over WebSocket) and the server picks the id.
  AG-UI (`POST /v1/ag-ui`) and `/v1/graph/live` still accept an id the frontend made, because
  CopilotKit and the realtime client create their own; the caller owns a new one, and an existing
  thread is owner-checked as before. They pass `client_thread_ids=True` to
  `AuthorizationBackend.authorize` (a custom backend sees it in `**context`). The boot refusal for
  `ag_ui` with `thread_ids: "server"` is gone. `"client"` stays available as an explicit opt-in.
- **`/v1/graph/live` now re-checks the token during the session.** It was verified only at the
  handshake, so a token that expired or was revoked (`token_revocation`) kept streaming audio to
  the provider. The credential is now re-checked before the session starts and every 30 seconds
  after, with the same expiry and revocation checks `/v1/graph/ws` runs before each run
  (a revocation backend outage counts as revoked). On failure the server sends a fatal
  `session_expired` event and closes with 1008. Live sessions already count against
  `websocket.max_connections_per_user`. Buffered input is also capped at 16 MiB per session
  (frames over it are dropped and logged), where before only the frame count was bounded
  (1000 frames of up to 1 MiB each). Clients must reconnect with a fresh token when a session
  ends this way.
- **`MODE` is now validated.** It is trimmed and lowercased, and `prod` means `production`;
  `dev` and `local` mean `development`. Before, any value other than exactly `production`
  (`prod`, a typo) silently ran with the development posture: docs on, evals router mounted,
  weak JWT secret only warned, CORS wildcard accepted. Unset still means `development`.
- **HTTP routes now take the bearer token only from the `Authorization` header.** The
  `Sec-WebSocket-Protocol: 10xgraph-bearer, <token>` credential (and the legacy
  `agentflow-bearer` form) was also accepted on plain HTTP requests; it is now honoured on
  WebSocket connections only, like `?token=`. Browser WebSocket clients are unaffected.
- **Clients can no longer set `file_tool_root`, `workspace_root`, `tool_timeout` or
  `node_timeout` in request `config`.** Core reads these keys from the run config, so a client
  could point the prebuilt file tools at any directory on the server, or turn off the tool and
  node time limits with `0` or `null`. They are now dropped like `authz` and `user_id`. Set
  them in your graph instead (for the file tools, use `create_file_tools(root=...)` from
  `10xgraph`).
- **New `runs.max_parallel_tool_calls` in 10xgraph.json** (positive integer, editable in
  `10xgraph config`). The server sets it as the run's `max_parallel_tool_calls` on every run
  path, capping how many tool calls one model turn runs at once. Clients cannot set it. Unset,
  core's default (10, or `TENXGRAPH_MAX_PARALLEL_TOOL_CALLS`) applies. Needs `10xgraph` 1.0
  or later.
- **Built-in state fields are never client-writable.** `context_summary` and `execution_meta`
  in `initial_state` (invoke, stream, WebSocket) or in `PUT /v1/threads/{id}/state` now get 422.
  Before, `PUT state` wrote `context_summary`, which the model reads as its own earlier words.
  Custom state fields and `runs.state_keys` work as before. AG-UI already dropped them.
- **Client messages are user messages only.** On invoke, stream, WebSocket and the `context` of
  `PUT state`, a message with role `assistant` or `system` gets 422, and so does a `tool`
  message that holds anything but tool results. This closes forged assistant turns that
  persisted in the thread.
- **Forged tool results are refused on thread routes too.** `PUT state` applies the check
  invoke already had: a tool result is accepted only as the answer to a remote tool call that is
  waiting for one.
- **The server generates every message id.** A `message_id` sent on invoke, stream, WebSocket
  or in `PUT state` context is ignored, so a client cannot pre-claim ids. AG-UI keeps client ids
  (its converter relies on them).
- **External media URLs are checked before the run.** Every media block with a non-internal URL
  (`http(s)://`, `gs://` and so on) in client input is run through the graph's media fetch
  policy (`compiled_graph.media_policy`, or the default `MediaFetchPolicy`) on invoke, stream,
  WebSocket, AG-UI, `POST /v1/threads/{id}/messages` and `PUT /v1/threads/{id}/state`. A blocked
  URL gets 400 with the policy's message (on the WebSocket, an error chunk). No network is
  touched. The policy lives in core and is set in code; the API has no setting for it. Needs a
  core release that ships `MediaFetchPolicy`; on older cores the check is skipped.
- **Uploads must look like their declared type.** `POST /v1/files/upload` compares the first
  bytes with the declared type for PNG, JPEG, GIF, WebP, BMP, TIFF, ICO, PDF, Office (ZIP and
  OLE), MP3, WAV, OGG, FLAC, MP4 and WebM, and answers 415 `file content does not match <type>`
  on a mismatch. Types with no known signature (plain text, JSON, CSV) are accepted as before.
  EXIF is kept.
- **Log lines escape control characters.** `log_safe` now also escapes ESC and the other C0
  control characters (not just CR and LF), and the upload route logs the client filename
  through it.
- **Document text extraction is bounded.** HTML, XML, CSV, Markdown, JSON and plain-text
  uploads over 5 MiB are no longer parsed (a 12 MB HTML file used about 1.2 GB of RAM); they
  are stored without extracted text and a warning is logged. At most 2 extractions run at once,
  and a slot stays taken until the worker thread actually finishes, so the 30 s timeout can no
  longer let threads pile up.
- **`10xgraph skills` no longer follows symlinks in the target project.** It refuses to install
  when any existing component of the destination (`.claude`, `.agents`, `.github` and the
  paths under them) is a symlink, or when the resolved destination is outside `--path`, so a
  hostile repo cannot make it delete or write files elsewhere.

- **One run at a time per thread.** Invoke, stream, `WS /v1/graph/ws` (each run on the socket)
  and AG-UI take a per-thread lock before the graph starts, so a double submit or two tabs can
  no longer run tools twice or interleave the history. The lock is a one-slot lease keyed by
  thread id (`runs:thread:<id>`): shared through Redis when it is configured, otherwise per
  process. It expires by itself (60 s, renewed while the run is alive), so a crashed worker
  cannot hold a thread, and it is released when the run ends, fails, is cancelled or the client
  disconnects. A second run on a busy thread waits up to 30 s, then gets **409** with the
  message "thread is busy" in the standard error envelope (streaming routes answer before the
  response starts; on the WebSocket it is an error chunk `{"event": "error", "data": {"reason": "thread is busy"}}` and
  the socket stays open). A new thread is locked as soon as its id exists. `/v1/graph/live` is
  not covered. Apps that sent parallel runs on one thread will now see them queue or get 409.
  A Redis error lets the run proceed (logged), like the per-user run cap.
- **Production hides raw error text on stream, WebSocket and AG-UI.** A failing node or tool put
  its exception text (for example a `redis://:SECRET@host` connection string) in the `ERROR`
  chunk's `data.reason`, `data.error` or `data.message`, and in `state.execution_meta` at full
  granularity, and the stream forwarded it, while invoke already answered generically. With
  `MODE=production` the three paths now share one step (`sanitize_error_chunk`, applied in
  `GraphService.stream_chunks`) that swaps that text for the same generic message invoke uses
  (`An error occurred in a graph node.`, chosen by the `[NODE_001]` style code when present). The
  core's own `Tool '<name>' failed with an internal error (reference <id>).` text is kept: it
  holds no detail. Development is unchanged.

- **A resume can name the interrupt it answers.** `POST /v1/graph/invoke`, `POST /v1/graph/stream`
  and the `/v1/graph/ws` frame accept an optional `interrupt_id`. If the thread is not paused, or
  is paused at a different interrupt, the request gets 409 before anything runs (an error chunk on
  stream and WebSocket). A retried or stale resume can no longer answer the next prompt unseen.
  The id is also passed to core as `resume_interrupt_id`.
- **Resume answers are checked against the interrupt's `response_schema`.** A failing answer is
  422 on invoke and an error chunk on stream, WebSocket and AG-UI; a core interrupt mismatch is
  409. Needs a core release with `ResumeValidationError` and `InterruptMismatchError`.
- **Request size limits.** At most 500 messages per request, 100 content blocks per message and
  1,000,000 characters of text in total across text blocks; over a limit is 422 naming the limit.
  Applies to invoke, stream, WebSocket (`messages` and `tool_result`), `PUT
  /v1/threads/{id}/state` `context` and `POST /v1/threads/{id}/messages`. AG-UI clients resend
  the whole conversation, so on AG-UI the limits apply to the trailing turn (what reaches the
  graph) and the full history only gets a 10,000-message cap plus the per-message block cap. The
  limits are fixed, not settings.
- **A custom `AuthorizationBackend` now isolates users by default.** `isolation_scope()` used to
  default to `"none"`, so a backend that implemented only `authorize()` silently turned off
  per-user storage isolation: every user saw every thread and memory. It now defaults to
  `"owner"`. Return `"none"` from `isolation_scope()` to share data on purpose. The built-in
  backends behave as before (`allow_all` / `default` / `none` share, `ownership` and RBAC isolate;
  `DefaultAuthorizationBackend` now says `"none"` explicitly).
- **JWT `user_id` and scope claims are validated.** `user_id` must be a non-empty string or a
  whole number (not a boolean), which is stored as a string (`7` becomes `"7"`); null, empty or
  whitespace-only, boolean, float, list and dict get 401. Revocation is checked on `jti` even
  when `user_id` is missing or odd (it was skipped for a falsy `user_id`); per-user revocation
  applies to a valid `user_id`. The OAuth `scope` claim (space-separated) was ignored, leaving
  the token unrestricted, and a string `scopes` was matched by substring (`xgraph:invoke_admin`
  granted `graph:invoke`). Both `scope` and `scopes` (a list, or a space-separated string) are now
  read and merged into a set of exact scope names. A scope claim of the wrong type (number,
  dict, list holding non-strings) gets 401. A token with no scope claim stays unrestricted.

- **`runs.timeout` defaults to 300 seconds in production** (no limit in development). Set a
  number in `10xgraph.json` to change it, or `null` to remove the limit. The timeout error tells
  the client to raise `runs.timeout` in `10xgraph.json`. Realtime sessions stay unlimited.
- **`STRICT_PRODUCTION_CHECKS` is on by default in production.** A wildcard `ALLOWED_HOST` or
  `ORIGINS`, or enabled `DOCS_PATH`/`REDOCS_PATH`, stops the server from starting. Set
  `STRICT_PRODUCTION_CHECKS=false` to opt out.
- **The wildcard check in `ORIGINS` and `ALLOWED_HOST` strips entries**, so `" *"` and
  `"api.example.com, *"` are no longer missed.
- **Generated production files use placeholders instead of `*`.** The production `.env.example`
  and the generated docker-compose and Kubernetes files set placeholder origins and hosts.

### Added

- **`observability.user_id_mode`** in `10xgraph.json` (`"raw"` default, `"hash"`, `"mask"`):
  how the user id appears in traces. It is passed to the `OtelPublisher` the server binds, for
  both the `observability` block and `OTEL_ENABLED`, and shown in the `10xgraph config`
  editor's Observability section. `hash` is keyed when `TENXGRAPH_TRACE_USER_ID_KEY` is set.
  Message and tool content stay in traces at the `full` level. A core without the option
  keeps the raw id and logs a warning.
- **`on_busy` request option** (`"wait"` default, or `"cancel"`), a top-level field next to
  `recursion_limit` on `POST /v1/graph/invoke`, `POST /v1/graph/stream` and `WS /v1/graph/ws`.
  With `"cancel"`, a run that finds its thread busy asks the running run to stop (the same
  mechanism as `POST /v1/graph/stop`, repeated while waiting), then waits for the lock and runs;
  an idle thread just runs. The 30 s wait limit and 409 still apply. AG-UI has no such field and
  always waits.
- **Uploaded files live and die with the messages that use them.** When an accepted message
  carries `graph://media/<key>` (or the legacy `agentflow://media/`), the thread id is added to
  the file's `threads` metadata (owned files only; invoke, stream, WebSocket, AG-UI,
  `POST /v1/threads/{id}/messages`, `PUT .../state`). Deleting a message (only when no other
  message in the thread still uses the file) or a thread drops the link, and a file with no
  link left is deleted. A file shared by two threads stays until both are gone. Files uploaded
  before this release have no `threads` key and are never deleted automatically. Cleanup
  failures are logged and never fail the delete. Needs a core release with
  `BaseMediaStore.update_metadata`; on older cores nothing is tracked or auto-deleted.
- `media.delete_with_thread` in 10xgraph.json (default `true`, editable in `10xgraph config`).
  `false` keeps recording links but never deletes a file automatically.
- `DELETE /v1/files/{file_id}` deletes a file and its metadata. Owner only: 404 (not 403) for a
  missing file or one that belongs to someone else. Returns `{"data": {"file_id": "...",
  "deleted": true}, "metadata": {...}}`. Needs the new `files:delete` scope, which `"*"` roles
  have.

- `PUT /v1/threads/{thread_id}/messages/{message_id}` sets feedback on a message. Body:
  `{"feedback": {...}}` (any JSON object, at most 8 KB serialized and 10 levels deep, else 422).
  It is stored in the message metadata under `feedback` and nothing else on the message can
  change. Needs `checkpointer:write` and thread ownership; 404 when the thread has no such
  message; 501 until `10xgraph` 1.0 (`aupdate_message_metadata`) is installed.
- `RequirePermission(..., explicit_scope=True)`: the scope must be granted explicitly, even for
  an identity that declares no scopes.
- Scope `checkpointer:import`, granted to `"*"` roles.

- `interrupt_id` on graph invoke, stream and WebSocket input. WebSocket input now also accepts
  `resume`, which was dropped before, so a `fresh` frame can resume an `interrupt()` without
  messages.

### Changed

- An unknown `current_node` in `initial_state` returns 422 (was a 500 from core). Wrong-typed
  state values (a string for an `int` field) return 422 naming the field, on run input and on
  `PUT state` (was stored, then crashed a later run).
- Uploaded documents over the extraction size limit no longer cause a slow or memory-heavy
  parse; the upload succeeds with `extracted_text` null.

### Fixed

- **Client mistakes on thread routes are a 404 or 422, not a 500, and state and message
  responses no longer carry provider or framework internals.** `GET` and `DELETE` of a message
  the thread does not have return 404 on every checkpointer (in-memory and SQLite raised
  `IndexError`, Postgres deleted silently). `PUT /v1/threads/{thread_id}/state` with a `context`
  that is not a list of messages returns 422 naming the field. Message `raw` is no longer
  returned by the thread state and message routes, and `execution_meta.internal_data` in state
  responses keeps only `error` (the playground shows it). `usages` and the interrupt fields are
  unchanged.
- **Development error responses for node and graph errors are no longer a 422.** Outside
  production, `GraphError`, `NodeError`, recursion, storage, transient storage, metrics, schema
  version and serialization errors put their `context` dict into `details`, which expects a
  list, so the client got `422 VALIDATION_ERROR` instead of the real status and code. Each
  context key now becomes one `details` entry (`loc` the key, `msg` the value, `type`
  `context`). Production still hides `details`.
- **`POST /v1/graph/fix` no longer deletes healthy tool-call messages.** It treated every
  assistant message with tool calls as broken (provider-shaped tool calls never carry
  `content`), which orphaned the tool results. It now removes only assistant messages with a
  tool call that has no result (matched by call id), then the tool results left without a call.
  The unanswered calls of the latest assistant message are kept while the thread is paused
  (`interrupt()`, HITL `interrupt_before` / `interrupt_after`) or waiting on a client tool.
  A thread with nothing to repair returns `removed_count` 0 and is not written.

## [0.9.0] - 2026-10-22

One server command, a guided `10xgraph init`, leaner production images and Python 3.14 and
3.15 support. This release has a breaking change: read the **Breaking** list before upgrading.

### Breaking

- **`10xgraph api` and `10xgraph play` are removed; `10xgraph dev` is the only server command.**
  All three ran the same local Uvicorn dev server. Migration: `10xgraph api` ->
  `10xgraph dev --no-open`, `10xgraph play` -> `10xgraph dev`. Options are unchanged
  (`--config`, `--host`, `--port`, `--reload/--no-reload`). `10xgraph demo --style` no longer
  accepts the `play` / `api` aliases. Production is unaffected: the image from `10xgraph build`
  runs Gunicorn directly.

### Added

- **Python 3.14 and 3.15 are officially supported.** The test suite runs on 3.12, 3.13, 3.14
  and 3.15 in CI, and the package classifiers list all four.
- **`websocket.enabled` in `10xgraph.json`** turns the WebSocket routes on or off. `false` leaves
  out `/v1/graph/ws` and `/v1/graph/live`, so the server needs no WebSocket library. `true`
  without one refuses to boot with the install hint. Unset keeps them mounted, as before, but
  now logs a warning when no library is installed (previously a non-Google project silently
  answered 404 on both routes, because only `google-genai` brought in `websockets`).
- **`10xgraph-api[websocket]` extra** (`websockets`) for those routes.
- **`10xgraph init` asks whether to enable WebSocket streaming and realtime audio**
  (`--websocket/--no-websocket`, default off). Yes adds the `websocket` extra to a production
  `pyproject.toml` and writes `"websocket": {"enabled": true}`; no writes `false`.
- **`10xgraph init` asks for the AI provider** (Google Gemini, OpenAI or Anthropic Claude;
  `--provider` for non-interactive runs, default `google`). The scaffold's model, provider,
  `.env.example` API key, test conftest and eval simulator model follow the answer.
- **`10xgraph-api[images]` extra.** Pillow is optional in core `10xgraph`; this extra installs it
  for image resize, EXIF orientation and format conversion.

### Changed

- Replaced `asyncio.iscoroutinefunction`, which is removed in Python 3.16, with
  `inspect.iscoroutinefunction`. No behaviour change.
- **Production scaffolds list only the dependencies the answers need.** `pyproject.toml` now
  depends on `10xgraph[<provider>]` and `10xgraph-api[jwt,redis]` as chosen (no extras when
  neither is picked), and drops the hardcoded `google-genai`. The generated Dockerfile installs
  this list, so JWT and Redis work in the image without a separate `requirements.txt`.
- **The generated Dockerfile removes pip and the CLI's own libraries from the image.** typer,
  rich, questionary and what only they pull in (pygments, markdown-it-py, mdurl, prompt-toolkit,
  wcwidth, shellingham) are uninstalled in the builder stage by `tenxgraph_api/_slim_image.py`,
  unless another installed package still needs them. With pip, about 38 MB smaller.
- Dropped unused dev dependencies (`mkdocs-gen-files`, `mkdocstrings`, `mypy-extensions`,
  `lib`, `markdown-it-py`).

### Fixed

- **`10xgraph init` generated a `pyproject.toml` claiming Python 3.10.** The template said
  `requires-python = ">=3.10"`, but `10xgraph` needs 3.12, so installing a generated project
  on 3.10 or 3.11 failed. It now says `>=3.12`.
- **Redis rate limiting from a URL crashed at startup** with `NameError: Redis`: the class was
  imported only for type checking but used at runtime to bind the client.
- **A production scaffold with the Redis rate limiter did not start:** `10xgraph.json` had no
  Redis URL. It now reads `"redis": {"url": "${REDIS_URL}"}` from `.env`.
- **A production scaffold did not start at all:** `10xgraph.json` points at
  `graph.agent:container`, which the template never defined. `graph/agent.py` now exports it.
- **The production scaffold's `tests/test_graph_nodes.py` failed on collection** (it imported
  `graph.nodes` and `graph.utils`, which the template does not create). It now tests the agent
  in `graph/agent.py`.

## [0.8.0] - 2026-10-16

Security and scalability pass over the API, following an external review. Every item below
has tests; see the review notes for the full list. This release has breaking changes: read
the **Breaking** list before upgrading.

### Breaking

- **Ownership checks fail closed.** With auth on, an ownership or `rbac` authorization
  backend now refuses to start on a checkpointer that cannot resolve thread owners
  (`aget_thread_owner` not implemented), and denies thread requests if none is available at
  runtime. Implement `aget_thread_owner`, or set `"authorization": "allow_all"` or a custom
  backend.
- **An unowned thread only allows actions that create it** (`graph:invoke`, `graph:stream`,
  `checkpointer:write`). Reading, deleting, stopping or fixing a thread that does not exist
  (or has no owner) now answers 403 instead of an empty result.
- **Authorization defaults to `ownership` whenever auth is configured**, in every mode. It
  used to be `allow_all` unless `MODE=production`. Set `"authorization": "allow_all"` to keep
  the old development behaviour.
- **Realtime init frames can no longer replace the system prompt by default.** Add
  `"system_prompt"` to `websocket.realtime_client_overrides` to allow it.
- **`trusted_proxy_headers: true` without `trusted_proxies` trusts only loopback and private
  networks**, not every peer. List your proxy networks (or `0.0.0.0/0` and `::/0`).
- **Production defaults:** `MEDIA_REQUIRE_OWNER` is on, `graph_introspection` is `scoped`
  (graph structure, tools and state schema need the `graph:read` scope), and `IS_DEBUG` is
  forced off.
- **`/v1/graph/invoke` no longer returns `meta.thread_name`**; the name is generated in the
  background and appears on the thread (`GET /v1/threads`).
- **Production 4xx responses keep their message**; only 5xx are made generic. A 422 built
  from an unexpected `ValueError` is still made generic.
- **`gunicorn` is no longer a dependency.** The generated Dockerfile installs it, so
  `10xgraph build` images are unaffected. If you run `gunicorn` yourself outside that image,
  install it alongside the API: `pip install gunicorn uvicorn-worker`.
- **Gunicorn's worker class is now `uvicorn_worker.UvicornWorker`** (from the `uvicorn-worker`
  package). `uvicorn.workers.UvicornWorker` is deprecated in uvicorn. Regenerate the
  Dockerfile and compose file with `10xgraph build --force`, or change `-k` in your own
  command.
- **`POST /v1/graph/fix` returns `state` as a JSON object.** It used to be a JSON-encoded
  string. Drop any `JSON.parse` / `json.loads` on that field; the TS client already types it
  as an object.
- **Generated Dockerfiles install only the dependencies you list.** The `pyproject.toml` path
  no longer adds `10xgraph-api` on top of your list. `10xgraph build` warns when the requirements file or `pyproject.toml` does not list
  `10xgraph-api`.

### Added

- `runs` key in `10xgraph.json`: `thread_ids` (`client` | `server`), `config_keys` and
  `state_keys` allow-lists, `max_concurrent_per_user` (default 10) and `timeout`.
- `token_revocation` key (`redis`, `memory` or `module:attr`) and the
  `TokenRevocationBackend` hook: revoked tokens are refused before they expire, on HTTP and
  on every WebSocket run.
- `graph_introspection` key (`open` | `scoped`).
- `websocket.realtime_client_overrides` allow-list.
- `STRICT_PRODUCTION_CHECKS=true`: production refuses to start on wildcard `ALLOWED_HOST` or
  `ORIGINS`, or enabled API docs.
- `GET /health/ready` readiness probe (checkpointer, Redis, store); the k8s manifest uses it.
- Run and WebSocket connection caps are shared across workers through Redis when it is
  configured.

### Changed

- `10xgraph play` and `10xgraph dev` open the hosted playground at its new address,
  `https://play.10xgraph.com`. The previous address, `playground-463bd.web.app`, still
  serves the same app for older releases.
- Thread delete leaves a short tombstone so a deleted id cannot be handed to another user
  while other workers still cache the old owner.
- Every thread the API saves records its owner, which fixes owner lookups on the in-memory
  and SQLite checkpointers.
- WebSocket JSON frames over `MAX_REQUEST_SIZE` are refused (close 1009); `10xgraph api`
  passes the same limit to uvicorn as `ws_max_size`.
- Request ID, security headers and rate limiting are pure ASGI middleware; CORS is now the
  outermost layer, so 429, 413 and Host rejections are readable by browsers.
- Route-set security headers (such as the download sandbox CSP) are no longer overwritten.
- The JWT is verified once per request when rate limiting by user.
- Local-store downloads stream from disk; uploads are read into memory once.
- JSON responses are serialized by pydantic-core (`model_dump_json`) instead of orjson.
  FastAPI deprecated `ORJSONResponse` in 0.131; the FastAPI floor stays at 0.116.
- Dependencies: dropped `orjson` and the unused `platformdirs`; the `uvicorn` floor is now
  0.36 (required by `uvicorn-worker`).
- Generated Dockerfiles build in two stages: dependencies go into a virtualenv in a builder
  stage, and only that virtualenv and your code reach the runtime image. The runtime image
  has no compiler or curl, copies code with `COPY --chown` instead of a second `chown -R`
  layer, and its healthcheck uses the Python standard library.
- A requirements file in a subfolder (for example `requirements/base.txt`) keeps its path in
  the generated Dockerfile.
- Packaging: `license` is an SPDX expression (`MIT`), so builds no longer warn about the
  deprecated table form; the build needs setuptools 77 or later.

### Security

- **Log injection.** Values a client controls (user and thread ids, file ids, rate-limit
  keys, request paths, run config and messages) are written to logs through `log_safe()`,
  which escapes CR and LF, so a request cannot forge extra log lines. This covers the graph,
  checkpointer and media services, the rate-limit middleware, the WebSocket guard and the
  run limits.
- **ReDoS in the log sanitizer.** The JWT detection pattern backtracked polynomially on
  crafted input (about 585 ms for a 16,000-character string). It now matches in linear time.
- **Security examples.** `examples/security/` no longer prints any part of an API key or of
  the JWT secret. The API key example hashes keys with HMAC-SHA-256 and a server-side
  `API_KEY_PEPPER`; hashes made with the previous example's plain SHA-256 must be
  regenerated.

## [0.7.0] - 2026-10-10

**First release as `10xgraph-api`.** 10xScale Agentflow is now 10xGraph. This package
replaces `10xscale-agentflow-cli`; the server, the CLI and the HTTP API are unchanged apart
from the names below. The old package import, command, config file name and env vars keep
working until 2.0.

### Changed

- **PyPI package `10xscale-agentflow-cli` is now `10xgraph-api`.** Uninstall the old package
  and the old core first: `pip uninstall 10xscale-agentflow-cli 10xscale-agentflow`, then
  `pip install 10xgraph-api`.
- **Import package `agentflow_cli` is now `tenxgraph_api`** (`from tenxgraph_api import
  BaseAuth`). `agentflow_cli` stays as a deprecated alias that resolves to the same module
  objects and emits a `DeprecationWarning` once.
- **The command is `10xgraph`.** `agentflow` is kept as a deprecated alias that prints a
  one-line notice to stderr and runs the same CLI.
- **The config file is `10xgraph.json`.** `agentflow.json` is still read when no
  `10xgraph.json` sits in the same directory; when both exist, `10xgraph.json` wins. This
  applies to the CLI (`api`, `play`, `dev`, `audit`, `config`, `eval`, `test`) and to the
  server started without `GRAPH_PATH` (for example from the generated Dockerfile).
  `10xgraph api` notes when it loaded the legacy name. `10xgraph init` writes
  `10xgraph.json`. `10xgraph config` always edits `10xgraph.json`: when only
  `agentflow.json` exists it opens prefilled from that file, and the first save creates
  `10xgraph.json` without touching `agentflow.json`.
- **Core dependency is `10xgraph>=0.10.0,<2.0`** (import `tenxgraph`), replacing
  `10xscale-agentflow`. Do not install both: each ships an `agentflow` module.
- **CLI env vars use the `TENXGRAPH_` prefix:** `TENXGRAPH_NO_FULLSCREEN`,
  `TENXGRAPH_NO_SPINNER`, `TENXGRAPH_ASCII`. The `AGENTFLOW_` names are read when the new
  variable is unset.
- **Uploaded media is referenced as `graph://media/<key>`**, matching the core. Client
  input using `agentflow://media/<key>` still resolves, and both forms go through the same
  ownership check.
- **Default `MEDIA_CLOUD_PREFIX` is `10xgraph-media`.** With the default, the core still
  reads objects stored under `agentflow-media`; an explicitly set prefix is used as is.
- **The WebSocket bearer subprotocol is `10xgraph-bearer`.** `agentflow-bearer`, which
  released clients send, is still accepted and echoed back.
- **The bundled skill installs as `10xgraph`** (`.claude/skills/10xgraph`,
  `.agents/skills/10xgraph`, `.github/skills/10xgraph`) and the Copilot instructions file as
  `10xgraph.instructions.md`. Earlier `agentflow` skill folders are left in place; delete
  them after installing the new one.
- **Logger names moved to `tenxgraph_api.*`** (from `agentflow-cli.*`, `agentflow_api.*` and
  `agentflowcli`). Update any logging config that targets the old names.
- The default Redis rate-limit key prefix is `10xgraph:rate-limit` (was
  `agentflow:rate-limit`). Counters restart once on upgrade; a configured `prefix` is kept.
- The config editor's session header is `X-10xGraph-Token`.
- **`10xgraph build` Dockerfiles ship only what the server runs.** After installing
  dependencies, the image deletes the CLI-only `tenxgraph_api/cli/templates` (project
  scaffolds, bundled skill) and `tenxgraph_api/cli/config_editor` (the local HTML/JS editor).
  The generated `.dockerignore` also leaves out `tests/`, `evals/`, `.claude/`, `.agents/`
  and `.github/`. Regenerate the Dockerfile with `10xgraph build --force` to pick this up.
- Repository, issues and changelog links point at
  [github.com/10xGraph/10xgraph-api](https://github.com/10xGraph/10xgraph-api); docs at
  [10xgraph.com](https://10xgraph.com).

- **Terminal look follows the brand.** Colors come from the logo and docs palette: ink
  text, the amber entry node, the blue accent (replacing the cyan/violet/pink gradient),
  tuned to read on light and dark terminals. The intro is shorter (0.9s), shows the running
  `10xgraph-api`, core and Python versions, draws the command pipeline like the logo's
  graph, and can be skipped with any key.
- **Easier exit.** Ctrl+C now quits immediately everywhere; it no longer stops on
  "Press Enter to close". The pinned full-screen frame is now opt-in (`--fullscreen` or
  `TENXGRAPH_FULLSCREEN=1`); by default the intro hands over to your normal scrollback, so
  nothing waits for a key when a command ends.

### Unchanged on purpose

- Error code `AGENTFLOW_VALIDATION_ERROR` and the `agentflow.cli/v1` schema id in
  `--format json` output, so clients and scripts that match on them keep working.
- CLI error codes (`AF-*`).

---

## [0.6.0] - 2026-10-07

**Final release of `10xscale-agentflow-cli`.** Agentflow is now 10xGraph
([10xgraph.com](https://10xgraph.com), [github.com/10xGraph](https://github.com/10xGraph)).
No further versions will be published under this name; the API server and CLI continue
as `10xgraph-api`. Installed copies keep working, and `10xscale-agentflow-cli==0.6.0`
can be pinned.

### Added

- **AG-UI endpoint (`POST /v1/ag-ui`)** serves the graph over the
  [AG-UI protocol](https://docs.ag-ui.com), so AG-UI clients such as CopilotKit
  (through its generic `HttpAgent`) can use an Agentflow agent. Off by default: set
  `"ag_ui": {"enabled": true}` in `agentflow.json` and install the new `ag-ui` extra
  (`pip install "10xscale-agentflow-cli[ag-ui]"`, `ag-ui-protocol>=1.0,<2`). The server
  refuses to start with a clear install hint when the endpoint is enabled but the package
  is missing. The route uses the `graph:stream` permission and checks `threadId`
  ownership when auth is configured. Streams text and reasoning, tool calls and results,
  node steps, application state (`STATE_SNAPSHOT`), and `interrupt()` pauses (`RUN_FINISHED`
  with an `interrupt` outcome, answered through `RunAgentInput.resume`; `cancelled` resumes
  with `None`). The thread's checkpoint is the record of the conversation, so only new user
  messages and tool results from the client reach the graph. Browser tools the client sends
  (`RunAgentInput.tools`, e.g. CopilotKit `useFrontendTool`) are offered to the model for that
  run with no `agentflow.json` entry. `RunAgentInput.context`, `tools` and `forwardedProps`
  are available to nodes as `config["ag_ui"]`. `agentflow init` writes
  `"ag_ui": {"enabled": false}`, and `agentflow config` shows the new section. Requires
  `10xscale-agentflow>=0.10.0` (`interrupt()` and per-run `remote_tools`).
- **`resume` on `/v1/graph/invoke` and `/v1/graph/stream`.** Resumes a thread paused by
  `interrupt()`; `messages` may be empty when `resume` is sent.
- **`remote_tools` is a server-owned run-config key.** Client config can no longer set it on
  any route; only adapters whose clients bring tools by protocol (AG-UI) fill it.
- `GraphService.stream_chunks()` yields the graph stream as `StreamChunk` objects, with an
  optional `server_config` for server-owned run config; `stream_graph()` (NDJSON) is now a
  thin wrapper over it.
- **`agentflow skills --validate PATH`** checks a skill directory, or a folder of
  skill directories, against the Agent Skills specification (agentskills.io).
  It reports errors and warnings per skill and exits `1` when any skill is
  invalid. The option is repeatable, and it requires `10xscale-agentflow>=0.10.0`
  (`agentflow.core.skills.validate_skill`).
- **The bundled Agentflow skill conforms to the Agent Skills specification.** Its
  `description` was invalid YAML (an unquoted `TRIGGER when:`), so strict parsers,
  including Agentflow's own loader, rejected it. Codex, Claude and GitHub now all
  receive one identical `SKILL.md` that references `references/...` relative to
  the skill directory, instead of three copies hard-wired to one install path
  each.
- **`agentflow config` opens a browser editor for `agentflow.json`.** Optional sections
  (auth, authorization, rate limiting, observability, ...) can be switched on or off and
  filled in, validated without saving, and saved. Saving is blocked while there are errors,
  writes are atomic, key order is preserved, and the previous file is kept as
  `agentflow.json.bak`. Options: `-c/--config`, `--port`, `--no-open`.
- **`remote_tools` in `agentflow.json`.** Trusted schemas for tools executed by clients,
  attached to the graph at startup.
- **`websocket.max_connections_per_user`** caps how many concurrent WebSocket connections one
  verified user may hold.
- **`rate_limit.trusted_proxies`.** When set, `X-Forwarded-For` is honoured only for requests
  whose peer address is in one of the listed IPs or CIDR ranges.

### Changed

- **Requires `10xscale-agentflow>=0.10.0`**, the final release of the core under the old
  name. It is the first core release with `interrupt()`, per-run `remote_tools` and
  `validate_skill`. Do not install `10xgraph` in the same environment: both provide the
  `agentflow` module.
- WebSocket connection limits now default to finite values (`max_connections` 1000,
  `max_connections_per_user` 10) instead of unlimited when the key is missing. Set a key to
  `0` or `null` for unlimited. Graph runs started over `/v1/graph/ws` count against the
  global `rate_limit` bucket.
- Project URLs point at [10xgraph.com](https://10xgraph.com) and the 10xGraph organisation.

### Security

- **Server-owned config keys are stripped from client requests.** `authz`, `user`,
  `user_id`, `remote_tools` and `_`-prefixed internal keys in a request's `config` are
  dropped on every route that forwards it to the graph, checkpointer or store. The thread a
  request runs on is the one the permission check approved, normalised on both sides.
- **Client messages can no longer carry tool calls.** Only the model may request a tool; a
  client may send a tool result only as the answer to a remote tool call still waiting for
  one.
- **Document extraction is bounded.** ZIP-based documents are inspected before parsing
  (entry count, total uncompressed size, compression ratio), extraction has a timeout, and
  extracted text is capped.

### Breaking

- **`POST /v1/graph/setup` is removed.** Declare client-executed tools under `remote_tools`
  in `agentflow.json` instead; they are attached at startup.
- **`agentflow config list|get|set|unset|path|validate` are removed**, and stored
  `output.*` preferences are no longer read at startup. Pass `--format`, `--color` and
  `--progress` on the command line instead. `agentflow config` is now the
  `agentflow.json` editor above.

---

## [0.5.1] - 2026-09-24

### Added

- **Persistent full-screen application surface.** On an interactive terminal a
  command now runs on its own screen with branded chrome pinned in place: a
  header (gradient rule, identity, version, subtitle) at the top, a footer
  status bar at the bottom, and the command's output scrolling between them.
  Pinning uses a DEC scrolling region, so it survives output from child
  processes — pytest, Uvicorn, Questionary prompts — without routing any of it
  through a renderer. The screen is held until you press Enter, and released
  through a `finally` guard so a crash can never leave your shell on an
  alternate buffer or inside a scrolling region. Opt out with `--no-fullscreen`
  or `AGENTFLOW_NO_FULLSCREEN=1`.
- Full-canvas animated command intro: an eased block-letter `AGENTFLOW` reveal
  with a moving light front, a flowing gradient field, a typed tagline, and a
  per-command pipeline that fills in as the intro plays. Inside the full-screen
  surface it collapses into the pinned header; with `--no-fullscreen` it plays
  on a temporary screen and leaves a durable header in your scrollback.
- Live step timelines (`OutputFormatter.timeline`): a command declares its stages
  up front, so pending work is visible from the first frame while the running
  stage animates with a spinner, elapsed timer, and a live detail line. Wired
  into `play`/`dev`/`api`, `init`, `build`, `test`, and `audit`.
- Determinate progress with a running pass/fail tally
  (`OutputFormatter.progress_run`), used by `agentflow eval` so per-case results
  scroll above a bar that reports completion, counts, and elapsed time.
- Shared brand palette and glyph sets (`cli/core/theme.py`) with continuous
  gradient sampling and a complete ASCII fallback set.
- Command-specific intro signatures and taglines for `play`, `dev`, `api`,
  `init`, `build`, `test`, `eval`, `audit`, and `skills`, previewable with the
  side-effect-free `agentflow demo` (`--style all|typing|network|init|build|eval`).
- Row-by-row reveal for completion panels.
- **Shared guided-prompt layer** (`cli/core/prompts.py`). Every interactive
  question now runs through one themed service, so prompts share a palette, a
  cancellation contract (Ctrl+C returns to a clean exit rather than a
  traceback), and one non-interactive policy.
- `agentflow skills` picks agents with an arrow-key list where **space toggles**
  and enter confirms, instead of typing a menu number. Each row shows its
  install path, already-installed agents are labelled and pre-checked, and
  choosing one that exists offers to overwrite rather than failing.
- `agentflow skills` reports installs through a timeline and a completion
  screen, matching every other command.
- `agentflow init` prompts now explain each option inline — what Quick Start
  versus Production scaffolds, what each auth mode requires, what each rate
  limit backend costs.
- Staged startup feedback, a pre-flight port check, and connected-playground
  completion output for `agentflow play` and `agentflow dev`.
- Adaptive `--animation` / `--no-animation` controls with CI, pipe, JSON, and
  accessibility-safe fallbacks. Every animated surface has a plain
  line-per-transition renderer and a versioned JSON/JSONL event renderer.
- Adaptive Rich terminal rendering with TTY/CI detection, plain/JSONL modes,
  `NO_COLOR` support, ASCII fallback, quiet mode, and shared status rendering.
- Root `--format`, `--json`, `--color`, `--no-color`, `--progress`,
  `--animation/--no-animation`, `--fullscreen/--no-fullscreen`, `--cwd`,
  `--yes`, `--non-interactive`, `--debug`, and `-V/--version` options.
- `agentflow dev` as the goal-oriented local development command (config, host,
  port, `--reload/--no-reload`, `--open/--no-open`); `api` and `play` remain
  available for compatibility.
- `agentflow audit`: six read-only checks — Python interpreter, installed
  `10xscale-agentflow-cli`, installed `10xscale-agentflow`, whether the installed
  core still exposes the evaluation API `agentflow eval` imports, whether
  `agentflow.json` exists and declares a valid `agent` key, and whether the
  default port is free. Reported live through the timeline and again as a summary
  table. Exits `1` on any failure and `0` otherwise (warnings do not fail the
  run), so it works as a CI gate; nothing is written or changed.
- `agentflow demo`: preview the animation, timeline, and progress states without
  touching project state.
- Cross-platform `agentflow config list|get|set|unset|path|validate` user
  preferences, stored as JSON in the per-user config directory. `output.format`,
  `output.color`, and `output.progress` are read at startup as defaults; explicit
  flags still win.
- Reproducible `agentflow init --non-interactive` recipes and `--dry-run` previews.
- Stable CLI error codes and dependency recovery suggestions.
- `py.typed` marker, so type information now reaches consumers (PEP 561).
- `--integration` pytest flag gating tests marked `integration` that require real
  Redis/Postgres, so a default `pytest` run needs no external services.
- mypy configuration (`[tool.mypy]`) and a mypy step in CI.
- CodeQL static analysis workflow.
- Dependabot configuration for pip and GitHub Actions updates.
- Community health files: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`,
  `RELEASE_NOTES.md`, issue forms, and a pull request template.
- `Changelog` entry in project URLs.

### Changed

- Command implementations are loaded lazily, so a broken optional feature no longer
  prevents root help, version, completion, or unrelated commands from starting.
- CLI logging now uses one invocation-wide handler, so quiet and verbose levels apply
  consistently without duplicate records.
- Project configuration discovery now walks parent directories from the current
  working directory.
- Init, build, and eval status output now flows through the shared renderer instead of
  writing raw ANSI control sequences.
- **The full-screen session no longer erases the command's output.** It used to
  hold the alternate screen through a Rich live display, which re-homes the
  cursor before every write — so each line overwrote the last — and then released
  the screen on exit, which a terminal handles by discarding everything drawn on
  it. Short commands showed a flash of nothing. The frame now switches the buffer
  directly and pauses on a closing hint before letting go.
- The screen is claimed lazily, by the first command that renders a header.
  Script-shaped invocations (`--version`, `config get`, `--format json`) never
  take over the terminal or pause on exit.
- A console that reports itself as a terminal but refuses the alternate buffer
  (legacy Windows console) now aborts the frame before anything is written,
  rather than leaving a scrolling region on the user's real scrollback.
- **A terminal that cannot host a prompt no longer crashes the command.**
  `stdin.isatty()` is true under MSYS/Cygwin shells on Windows, but
  prompt-toolkit cannot attach to a console there and raises — which surfaced as
  `AF-INTERNAL-001: Unexpected error: Found xterm-256color, while expecting a
  Windows console`. Prompt availability is now probed, so such a terminal counts
  as non-interactive and gets the usual "pass --agent or --all" guidance.
- Pinned chrome is repainted after each prompt. Prompt-toolkit erases from the
  cursor to the end of the *screen*, which reaches past the scrolling region and
  took the footer with it.
- One Rich `Console` is now reused per stream. Rebuilding it per call meant a live
  display could not tell that ordinary prints belonged to it, so background output
  collided with spinners and progress bars instead of scrolling above them.
- `agentflow init` no longer prints one line per scaffolded file; files stream through
  the active timeline row instead.

### Fixed

- **Scaffolding templates were missing from the wheel.** The `package-data` globs only
  matched `*.json/*.yaml/*.yml/*.md/*.txt`, silently dropping
  `templates/dev/.env.example`, `templates/prod/.env.example`,
  `templates/prod/.python-version`, and `templates/prod/pyproject.toml`. `agentflow init`
  failed for anyone installing from PyPI. Packaging now ships the package tree wholesale.
- `agentflow version` reported `unknown` for the package version when installed from a
  wheel, because it read `pyproject.toml` from a path that does not exist in an installed
  distribution. It now resolves from installed distribution metadata and additionally
  reports the core `10xscale-agentflow` version.
- The `prod` template shipped `.pre-commot-config.yaml` (typo), so `pre-commit` found no
  config in scaffolded projects. Renamed to `.pre-commit-config.yaml`.
- Branch coverage is now measured by the default `pytest` invocation (`--cov-branch`),
  not only in the CI-specific command.

### Changed

- **Every runtime dependency now has a lower bound**, and pre-1.0 / major-version-risky
  dependencies have an upper cap (`pydantic>=2.13,<3`, `fastapi>=0.116,<1.0`,
  `10xscale-agentflow>=0.9.0,<2.0`, and so on). Previously all runtime dependencies were
  unpinned, so a major release of any of them could break installs without warning.
- Optional extras `snowflakekit`, `redis`, and `jwt` gained bounds.
- CI now runs on pushes to `main` as well as pull requests, and covers both advertised
  Python versions (3.12 and 3.13) rather than 3.13 alone.
- The release workflow now depends on a passing test job; it no longer builds and
  publishes untested code.
- `Documentation` URL points at the published docs site instead of a Read the Docs URL
  that was never provisioned.

### Removed

- `agentflow_cli/src/app/routers/a2a.py` and `a2ui.py`. Both were entirely commented out,
  never mounted by `setup_router.init_routes`, and shipped in the wheel as dead code.
  They can be restored from git history when the A2A surface is actually implemented.
- Design and planning notes from the repository root (`AUTHORIZATION_PLAN.md`,
  `AUTHORIZATION_PLAN_PHASE2.md`, `AGENTFLOW_JSON.md`, `CUSTOM_AUTH.md`).

---

## [0.5.0]

Initial entry in this changelog. Releases before `0.5.0` were not tracked here; see the
GitHub release history for their notes.

[Unreleased]: https://github.com/10xGraph/10xgraph-api/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/10xGraph/10xgraph-api/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/10xGraph/10xgraph-api/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/10xHub/agentflow-cli/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/10xHub/agentflow-cli/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/10xHub/agentflow-cli/releases/tag/v0.5.0
