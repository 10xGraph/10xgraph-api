# 10xgraph-api 1.0.0rc1

The 1.0 release candidate: secure-by-default threads, production checks and error handling, plus resume, run-busy and file-lifecycle features.

**This is a release candidate.** Pip skips it unless you pass `--pre`. It requires `10xgraph` 1.0, and its version is aligned with core `10xgraph` and `10xgraph-client`. This release has **breaking changes**. Read "Before you upgrade" first.

```bash
pip install -U --pre "10xgraph-api==1.0.0rc1"
```

This pulls `10xgraph` 1.0.0rc1.

## Before you upgrade

- **`runs.thread_ids` defaults to `"server"`** (was `"client"`).
  - REST and WebSocket clients that invent a thread id for a new thread now get 403 on `/v1/graph/invoke`, `/v1/graph/stream`, `/v1/graph/ws` and the checkpointer write routes.
  - Omit the id on the first request and use the `meta.thread_id` the server returns from then on (`"new"` over `/v1/graph/ws`), or set `"runs": {"thread_ids": "client"}` in `10xgraph.json`.
  - AG-UI and `/v1/graph/live` need no change. Only the built-in ownership and rbac backends refuse.
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

## New

- **`observability.user_id_mode`** in `10xgraph.json` (`"raw"` default, `"hash"`, `"mask"`) controls how the user id appears in traces.
- **`on_busy` request option** (`"wait"` default, or `"cancel"`) on invoke, stream and `WS /v1/graph/ws`. With `"cancel"`, a run that finds its thread busy asks the running run to stop, then runs.
- **Uploaded files live and die with the messages that use them.** Deleting the last message or the thread that uses a file deletes the file. `media.delete_with_thread` (default `true`) turns the automatic delete off.
- **`DELETE /v1/files/{file_id}`** deletes a file you own, behind the new `files:delete` scope.
- **`PUT /v1/threads/{thread_id}/messages/{message_id}`** sets feedback on a message.
- **`interrupt_id` on invoke, stream and WebSocket input**, so a resume can name the interrupt it answers. WebSocket input also accepts `resume` again.
- **`RequirePermission(..., explicit_scope=True)`** and the `checkpointer:import` scope.

## Security

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

## Fixed

- Client mistakes on thread routes are a 404 or 422, not a 500, and state and message responses no longer carry provider or framework internals.
- Development error responses for node and graph errors are no longer a 422 `VALIDATION_ERROR`.
- `POST /v1/graph/fix` no longer deletes healthy tool-call messages.

See `CHANGELOG.md` for the full list.
