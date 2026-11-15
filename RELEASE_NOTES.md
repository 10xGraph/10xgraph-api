# 10xgraph-api 1.0.0

The first stable release of `10xgraph-api`, the API server and CLI of 10xGraph. It turns a
10xGraph graph into a production FastAPI service. From 1.0 the HTTP API, the CLI commands and the
`10xgraph.json` keys are stable until 2.0. The deprecated `agentflow` names keep working until
2.0: the `agentflow_cli` import, the `agentflow` command, `agentflow.json`, the `AGENTFLOW_*`
environment variables and the `agentflow-bearer` WebSocket subprotocol.

```bash
pip install -U 10xgraph 10xgraph-api
npm install 10xgraph-client
```

Released together with `10xgraph` 1.0.0 and `10xgraph-client` 1.0.0. Requires `10xgraph` 1.0.
Python 3.12 or newer.

## Versioning from 1.0

- `major.minor` is identical across `10xgraph`, `10xgraph-api` and `10xgraph-client`.
  `10xgraph-api` 1.1.x works with `10xgraph` 1.1.x and `10xgraph-client` 1.1.x.
- Patch numbers move independently. A patch contains only bug or security fixes.
- A new feature means all three packages release the next `X.Y.0` together.
- The deprecated `agentflow` names (`agentflow_cli`, the `agentflow` command, `agentflow.json`,
  `AGENTFLOW_*`, `agentflow-bearer`) keep working until 2.0.

## What 1.0 brings since 0.7

- **Roles with reach.** One `auth` object: `method` (`none`, `jwt` or `custom`), `roles` with
  permissions and a reach of `own`, `tenant` or `all`, `default_role` and `tenant_claims`.
  `GET /v1/me` returns the caller's identity, roles, permissions and reach.
- **Ownership enforced in storage.** A thread, message, memory, file or run outside the caller's
  reach answers 404. Reads widen with reach; runs and writes stay with the owner.
- **One `cache_store`.** A `cache_store` object (`memory`, `redis` or `custom`) holds run locks and
  caps, idempotency, token revocation and rate limiting, so workers share them.
- **Background jobs on TaskIQ.** `from tenxgraph_api.jobs import broker`, the `10xgraph worker` and
  `10xgraph scheduler` commands, and the `/v1/tasks` routes. Jobs run as the caller.
- **Audit log.** A metadata-only log set by the `audit` key, with sinks `log`, `publisher` or your
  own `BaseAuditSink`, plus `LOG_FORMAT=json`. `10xgraph audit` checks the config.
- **Metrics and tracing.** Prometheus metrics for the API and the job worker, by scrape or OTLP
  push, and tracing set by the `observability` block.
- **Deploy files.** `10xgraph build` generates the Dockerfile, `docker-compose.yml` and
  `k8s.yaml`, with `worker` and `scheduler` services when a `worker` key is set.
- **AG-UI.** `POST /v1/ag-ui` serves the graph over the AG-UI protocol, so clients such as
  CopilotKit connect directly.
- **Operations.** `10xgraph config` edits `10xgraph.json` in the browser, `GET /health/ready`
  reports the checkpointer, store and cache store, and `MAX_NESTING_DEPTH` bounds the nesting
  accepted in client input.
- **Safer input and errors.** A NUL character or lone surrogate in client input is refused with
  422. With `MODE=production`, stream, WebSocket and AG-UI errors carry a generic message instead
  of raw exception text.

## Before you upgrade

**From 0.x.** 1.0 has breaking changes. The full list, with the migration for each, is in the
upgrade guide: https://10xgraph.com/docs/project/upgrade-to-1.0

The ones most projects hit:

- `10xgraph api` and `10xgraph play` are removed. Use `10xgraph dev` (`10xgraph dev --no-open`
  for the server only).
- The top-level `authorization`, `redis`, `rate_limit` and `token_revocation` keys refuse to boot.
  Auth lives in the `auth` object; the other three go inside `cache_store`.
- `BaseAuth.authenticate` is async. Change `def authenticate` to `async def authenticate`.
- `runs.state_keys` denies by default. List every state key your clients send, for example
  `"runs": {"state_keys": ["preferences"]}`.
- `thread_name_generator` defaults to none. Set `"random"` to keep the old naming.
- With `cache_store.token_revocation` on, every token needs a `jti` claim.
- `runs.thread_ids` defaults to `"server"`. Omit the thread id on the first request, or set
  `"runs": {"thread_ids": "client"}`.

**From a release candidate.** There was no 1.0.0rc7 of this package, so read the Breaking entries
of the `1.0.0` section in `CHANGELOG.md`, then the rc sections after yours. Since 1.0.0rc6:

- `thread_name_generator` defaults to none and must be `none`, `random`, `first_message` or
  `module:attribute`. `AIThreadNameGenerator` and `DefaultThreadNameGenerator` are replaced by
  `RandomThreadNameGenerator`.
- `runs.state_keys` denies by default; unset, `null` or `[]` allows no key.
- `BaseAuth.authenticate` is async.
- With `cache_store.token_revocation` on, a token without a usable `jti` answers 401
  `INVALID_TOKEN`.
- `MAX_NESTING_DEPTH` is a setting (default 64, range 1 to 900), and the audit log adds `input`
  and `rate_limit` events.
- `meta.is_new_thread` is correct on a continued thread. This needs `10xgraph` 1.0.0.
- `10xgraph init` Quick setup writes a `requirements.txt`, and the generated pyproject pins
  `10xgraph>=1.0,<2.0` and `10xgraph-api>=1.0,<2.0`.
- A NUL character or lone surrogate in client input answers 422 before the run starts.
- A `StorageError` from data the database refuses answers 422 `INVALID_REQUEST_DATA`, not 500.
- Expected 401s log at WARNING, and a token with a missing tenant claim answers 401
  `INVALID_TOKEN`.

See [CHANGELOG.md](CHANGELOG.md) for every change.
