# Jobs and cron (TaskIQ): sprint plan

Status: planned, nothing coded. Each sprint ends with a manual test the user runs before the next one starts.

## Goal

Opt-in background jobs and cron for production, using native TaskIQ. Developers write plain `@broker.task` functions. 10xGraph owns the broker, loads the same graph and InjectQ container in the worker, and adds HTTP and webhook triggers plus build support.

```python
from tenxgraph_api.jobs import broker

@broker.task(http=True, retry_on_error=True, max_retries=5)
async def summarize_interview(interview_id: str): ...

@broker.task(schedule=[{"cron": "0 2 * * *"}])
async def nightly_cleanup(): ...
```

```json
"worker": {
  "jobs": "jobs",
  "broker": "redis",
  "url": null
}
```

There is no `worker` section by default. Without it no routes are added, no worker runs and TaskIQ is not imported. TaskIQ comes only with `pip install "10xgraph-api[worker]"`.

### `worker` section

The `worker` section is separate from `cache_store`. Jobs can use their own Redis or Redis database.

| Key | Meaning |
|---|---|
| `jobs` (required) | Module to import, so its `@broker.task` functions are registered. |
| `broker` | `"redis"` (default) builds a `RedisStreamBroker`. `"module:attr"` points at any TaskIQ broker the user built (RabbitMQ, NATS, Kafka and so on); 10xGraph uses it as is. |
| `url` | Redis URL for the built-in broker, with `$VAR` and `${VAR}` expanded. It is ignored when `broker` is `"module:attr"`. |

The URL is resolved in this order: `worker.url`, then the `WORKER_REDIS_URL` env var, then `REDIS_URL`. A user with one Redis sets only `REDIS_URL`, and it is used everywhere. A user who wants jobs on a separate Redis or database sets `WORKER_REDIS_URL`.

With a custom broker, tasks are written against the user's own broker object, not `tenxgraph_api.jobs.broker`. Everything else (loader, HTTP labels, build) works the same.

## Known blocker

`taskiq-redis` 1.2.4 requires `redis>=8.0.0,<9`, but our `redis` extra pins `redis>=5.0.7,<7`. Sprint 0 must settle this, either by raising our pin (which must be checked against core's Redis usage) or by pinning an older taskiq-redis.

## Sprint 0: spike (no product code)

In a scratch venv with `taskiq` 0.13.0 and `taskiq-redis` 1.2.4, check the following:

- [ ] How to list a broker's registered tasks and find one by name. Build detection and the HTTP route both depend on this.
- [ ] Custom labels such as `http=True` reach the task's labels without errors.
- [ ] `SmartRetryMiddleware` works with `RedisStreamBroker` (retry, delay, max retries).
- [ ] When the worker is killed mid-task, the task is redelivered (stream acks).
- [ ] Graceful shutdown: SIGTERM finishes running tasks within `--shutdown-timeout`.
- [ ] `TaskiqScheduler` with `LabelScheduleSource` fires a cron task once.
- [ ] The redis version conflict above is resolved.

**Exit:** a short findings note. The plan is updated where reality differs.

## Sprint 1: worker core

- [ ] Add the `[worker]` extra: `taskiq`, `taskiq-redis`, with versions from sprint 0.
- [ ] Add the `worker` config section (`jobs`, `broker`, `url`), parsed in `graph_config.py`. Unknown keys are config errors, the same as other sections.
- [ ] `tenxgraph_api.jobs.broker`: a lazily configured `RedisStreamBroker` with retry middleware and a result backend (with expiry). Its URL is `worker.url`, then `WORKER_REDIS_URL`, then `REDIS_URL`. It is independent of `cache_store`.
- [ ] Support `broker: "module:attr"` by loading the user's TaskIQ broker and checking that it is a TaskIQ broker.
- [ ] Replace the legacy, fully commented-out TaskIQ stubs in `src/app/worker.py` and `src/app/tasks/`. They come from an old template (Tortoise, `fastapi_injector`) and nothing imports them.
- [ ] `10xgraph worker` command. It loads env, then `load_container`, then `load_graph`, then the `jobs` module, then runs the TaskIQ worker.
- [ ] InjectQ resolution inside tasks, so `Inject[...]` works the same as in graph code.
- [ ] Startup error when the `worker` section is set but the extra is missing.
- [ ] Tests: unit tests plus one integration test against real Redis.

**User test:**
1. Run `10xgraph worker` on the example project.
2. Call `.kiq()` from a script.
3. Confirm the task gets an injected dependency and can invoke the graph.
4. Confirm a failing task retries.
5. Kill the worker mid-task and confirm the task reruns.

## Sprint 2: HTTP trigger

- [ ] Add `POST /v1/jobs/{name}`, only for tasks labeled `http=True`.
  - Uses the existing auth plus an `AuthorizationBackend` check on resource `jobs`, action `trigger`.
  - Validates the body against the task signature.
  - Calls `.kiq()` and returns `202` with the `job_id`.
- [ ] Add `GET /v1/jobs/runs/{job_id}` for status and result, read from the result backend.
- [ ] Add `GET /v1/jobs` to list the tasks exposed over HTTP.
- [ ] Unknown tasks and unlabeled tasks return 404.

**User test:**
1. Call the endpoint with curl, with and without a token.
2. Send a bad body and confirm it is rejected.
3. Poll the status endpoint until the result arrives.

## Sprint 3: webhook trigger

- [ ] Add a `webhook=Verifier` label, which uses signature checks instead of user auth.
- [ ] Ship a built-in HMAC verifier with the secret read from env.
- [ ] Add a base class for custom verifiers, such as LiveKit's.
- [ ] Return 401 on a bad signature. The endpoint enqueues only and never runs the job inline.

**User test:**
1. Send a signed request and an unsigned one.
2. Run the LiveKit-style example: webhook, then summarize job.

## Sprint 4: cron and build

- [ ] Add the `10xgraph scheduler` command (loader plus `TaskiqScheduler` with `LabelScheduleSource`).
- [ ] `10xgraph build` imports the jobs module and reads the broker's tasks:
  - No `worker` section: `api` only.
  - Tasks: `api` and `worker`.
  - Any task with a `schedule` label: also `scheduler`, with replicas pinned to 1.
- [ ] Document the one-scheduler rule.

**User test:**
1. Build three sample projects (no jobs, jobs only, cron) and check the compose files.
2. Run the cron project and confirm the job fires once per tick.

## Later (not scheduled)

- Admin-only schedule edit (`ListRedisScheduleSource` plus `PATCH /v1/jobs/{name}/schedule`).
- Redis event triggers.
- Docs page and a client SDK method for triggering a job and reading its status.

## Rules carried through every sprint

- Jobs are registered only in code. HTTP can trigger a job but never register one.
- Delivery is at least once, so idempotency is the developer's job (key the work by, for example, `interview_id`). This goes in the docs from sprint 1.
- Cron jobs run as `system_user_id`. HTTP jobs run as the caller.
- Chat streaming and WebSocket code stay untouched.
