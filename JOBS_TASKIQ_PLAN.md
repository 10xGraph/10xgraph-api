# Jobs on TaskIQ: plan

Status: agreed 2026-11-06. Branch `feat/jobs-taskiq` (from `main`; old code stays on `worker`). Sprint 0 done (findings in JOBS_TASKIQ_SPRINT0.md); Sprint 1 done (Redis only; other brokers later); Sprint 2 next. Replaces JOBS_PLAN.md / JOBS_DESIGN.md (own work
manager), which are kept only for history.

## Idea

We do not build a job system. 10xgraph owns one TaskIQ broker, built from `10xgraph.json`. The
developer imports it and writes tasks. We add what TaskIQ lacks: the broker setup (InjectQ,
monitoring, retry, OTEL already on), `worker` and `scheduler` commands, HTTP routes with our auth,
owner-scoped status, the run config in tasks, and cancel by signal.

Target size: about 500 lines of code in `tenxgraph_api/jobs/`, plus tests.

## What the developer writes

### Tasks

```python
# app/report_tasks.py
from typing import Annotated

from injectq.integrations.taskiq import InjectTaskiq
from taskiq import TaskiqDepends
from tenxgraph.core.graph import CompiledGraph
from tenxgraph_api.jobs import broker, TaskContext


@broker.task(http=True)  # http=True: can be started or scheduled through the API; default False
async def save_report(
    report_id: int,                                            # caller sets this
    config: dict,                                              # we fill it; optional
    graph: Annotated[CompiledGraph, InjectTaskiq(CompiledGraph)],  # InjectQ fills it
    ctx: Annotated[TaskContext, TaskiqDepends()],              # we fill it; optional
) -> dict:
    await ctx.progress({"step": "loading"})
    if await ctx.cancelled():
        return {"cancelled": True}
    await graph.ainvoke({"messages": [...]}, config=config)   # runs as the caller
    return {"ok": True}


@broker.task(schedule=[{"cron": "0 2 * * *"}])  # static cron, plain TaskIQ
async def nightly_cleanup() -> None: ...
```

The developer surface is `broker` and `TaskContext` from us; injection comes from InjectQ itself.
No broker classes, no middlewares, no URLs in code.

`TaskContext` (optional):

- `task_id`, `task_name`
- `config`: the run config (same value a `config` parameter gets; always has `task_id`)
- `progress(meta)`: save progress, shown by `GET /v1/tasks/{id}`
- `cancelled()`: true after `POST /v1/tasks/{id}/cancel`

### The run config (same rule as core nodes and tools)

Core reads the function signature and passes `state` / `config` only when the function declares
them (`invoke_node_handler._prepare_input_data` for nodes, `KwargsResolverMixin._prepare_kwargs`
with `INJECTABLE_PARAMS` for tools). A function that does not declare them is called without them.
Tasks follow the same rule:

- A task that declares `config` gets the run config; one that does not is called without it.
  `ctx.config` is the same value for tasks that take `TaskContext`.
- The config is built on the server at call time, like a graph run: `user_id`, `user` (what
  `BaseAuth` returned), `authz` (the authorization result), plus `task_id`. It travels in a
  message label (`tenxgraph_config`, a JSON string: labels keep only int/str/float/bool) that only
  the server writes.
- Schedules store the config of the user who created them, so each scheduled run acts as them.
- Python callers pass it with `await jobs.enqueue(task, *args, config=config, **kwargs)`.
  Plain `.kiq()` works too; then `config` is `{}`.

### Reserved arguments

A caller (HTTP body or `jobs.enqueue`) can set only the task's plain arguments. These are refused
with 422:

- `config`, and the keys `user`, `user_id`, `authz` anywhere at the top level of `kwargs`
- any parameter that is injected (`InjectTaskiq(...)`, `TaskiqDepends()`, `TaskContext`)
- names the task does not have, and extra positional args

This is a security check, not tidiness: TaskIQ applies caller kwargs over injected values
(`kwargs.update(message.kwargs)` in its receiver), so `graph=` from a caller would replace the
injected graph. Checked in the API and `enqueue`, and again in the worker middleware.

Only `InjectTaskiq` injects; a plain `Inject[...]` default passes the marker object silently.
`TaskContext` is a plain class (TaskIQ reads `__init__`); progress and cancel need `async def`.

### Config

```json
"worker": {
  "broker": {"type": "redis_stream", "url": "${REDIS_URL}", "options": {}},
  "result_backend": {"type": "redis", "url": "${REDIS_URL}", "options": {}},
  "tasks": ["app/**/*_tasks.py"],
  "schedules": true,
  "result_ttl": 86400,
  "retries": 3,
  "prometheus": {"host": "0.0.0.0", "port": 9000}
}
```

- `broker.type`: a name for a TaskIQ-supported broker (`redis_stream` default, `redis_list`,
  `rabbitmq`, `nats`, `kafka`, `memory`) or `"module:attr"` for any other TaskIQ broker class.
  `url` is passed to it; `options` are extra constructor arguments (for example `idle_timeout`).
- `result_backend.type`: `redis` (default), `memory`, or `"module:attr"`. Omitted: Redis on the
  broker URL when the broker is Redis, else required.
- Each named type needs its TaskIQ package (`taskiq-redis`, `taskiq-aio-pika`, ...). A missing
  package refuses to boot and prints the pip command.
- `tasks`: module names or file globs; worker, scheduler and API import them so tasks register.
- `schedules`: turns on dynamic schedules (TaskIQ `ListRedisScheduleSource`, needs Redis).
- `result_ttl`: how long status, progress and results are kept.
- `retries`: the cap for TaskIQ's retry middleware. Retries are opt-in per task
  (`@broker.task(retry_on_error=True)`), so tasks that are not safe to repeat never re-run.
- Status records, cancel flags and schedule records live in the API's cache store (`jobs:` keys).
  Schedule listing needs an owner index; the cache store has no list/scan, designed in Sprint 3.
- Known limit: the cancel and argument guard does not reach `--use-process-pool` workers.
- Prometheus is off unless set. OpenTelemetry follows the existing `observability` block.
- `10xgraph init` (Production) and `10xgraph config` ask for broker type and URL, result backend
  type and URL, and the task file pattern.

### Commands

```bash
10xgraph dev          # API; starts and schedules tasks, does not run them
10xgraph worker       # runs tasks
10xgraph scheduler    # sends scheduled tasks; run exactly ONE (TaskIQ rule)
```

### From Python

```python
from tenxgraph_api import jobs

await jobs.enqueue(save_report, 42, config=config)                     # run now
sid = await jobs.schedule(save_report, 42, cron="0 9 * * 1", config=config)
await jobs.reschedule(sid, cron="0 10 * * 1")
await jobs.unschedule(sid)
```

## HTTP (same auth and authorization as the graph routes)

One route family for tasks and schedules.

| Route | Does | Permission |
|---|---|---|
| `POST /v1/tasks/{name}` | body `{args, kwargs}`: run now, returns `{task_id}`. Body `{args, kwargs, schedule}`: schedule it, returns `{schedule_id}`. `schedule` is one of `{"cron": "..."}`, `{"at": "<ISO time>"}`, `{"every": <seconds>}` | `tasks:invoke` |
| `GET /v1/tasks/{id}` | task: `{status, progress, result, error, created_at}`. Schedule: `{name, schedule, args, kwargs, created_at}` | `tasks:read` |
| `GET /v1/tasks?kind=schedule` | the caller's schedules | `tasks:read` |
| `PATCH /v1/tasks/{schedule_id}` | change the time only: body `{schedule}` (delete + add with the same id; TaskIQ has no update) | `tasks:invoke` |
| `POST /v1/tasks/{id}/cancel` | task: set the cancel flag. Schedule: delete it | `tasks:cancel` |

- Only tasks marked `http=True` can be started or scheduled over HTTP; others answer 404.
- Webhooks use `POST /v1/tasks/{name}`, or the developer's own route calling `jobs.enqueue`.
- Status values: `queued`, `running`, `success`, `failure`, `cancelled`. Kept in a small metadata
  record per task (owner, task name, created time, status), written at start and updated by our
  middleware; result and progress come from the TaskIQ result backend. All of it expires after
  `result_ttl`.
- Owner scope: a user sees and changes only their own tasks and schedules; anything else is 404.
- Cancel is a signal: a queued task is not run (our middleware marks it and a guard around the
  task function raises `TaskCancelled` before the body; a middleware cannot skip a task and must
  never raise); a running task stops when it checks `ctx.cancelled()`.
- Schedule listing uses our own owner index (TaskIQ `get_schedules()` hides future one-time
  schedules). The scheduler picks up changes within `--update-interval` (we default it to 10 s).

## How it works inside: one worker file we own

`tenxgraph_api/jobs/worker.py`, the same shape as a hand-written TaskIQ `worker.py`:

1. **Broker, built on import from the `worker` config.**
   - `redis_stream`: taskiq-redis `RedisStreamBroker` + `RedisAsyncResultBackend`
     (`result_ex_time` = `result_ttl`). It acks after the task and `XAUTOCLAIM`s messages idle
     longer than `idle_timeout` (default 10 min), so a dead worker's task runs again elsewhere
     (verified in `taskiq_redis/redis_broker.py`). Ours is a small subclass: upstream runs the
     autoclaim only after a read that returned messages, so on an idle queue a dead worker's task
     waits until new work arrives; our `listen()` also claims when nothing was read. We set
     `unacknowledged_lock_timeout`. It also renews the lease of every message it holds every
     `idle_timeout/3` (Lua: reset idle only while this consumer still owns it), so a long task is
     never run twice while its worker lives. `ListQueueBroker` pops the message, so a task
     dies with its worker; it stays available as `redis_list` but is not the default.
   - `memory`: `InMemoryBroker`, for tests and local dev.
2. **Middlewares, always on:** `SimpleRetryMiddleware` (count from config); our
   `MonitoringMiddleware` (task started / finished / failed events to the core publisher); our
   `TaskMiddleware` (status record, cancel check, `config` from the label into kwargs when
   declared). Optional from config: OpenTelemetry and Prometheus (both ship with TaskIQ).
3. **InjectQ binding:** `setup_taskiq(container, broker)` with the container built from the same
   `10xgraph.json` as the API (`agent`, `checkpointer`, `store`, `injectq`, `cache_store`,
   `id_generator`).
4. **`WORKER_STARTUP`:** load the graph, open checkpointer / store / cache store, ping them.
   **`WORKER_SHUTDOWN`:** close them in reverse order, each in its own `try`.
5. **Task discovery:** import the modules or globs in `worker.tasks`.

Project-specific setup (their DB, their own sweeps) stays plain TaskIQ in their code:
`@broker.on_event(TaskiqEvents.WORKER_STARTUP)`. We add no hook API of our own.

The API process imports the same `broker` and only sends.

## Files

| File | Holds | Size goal |
|---|---|---|
| `jobs/__init__.py` | exports `broker`, `TaskContext`, `enqueue`, `schedule`, `reschedule`, `unschedule` | small |
| `jobs/config.py` | pydantic `WorkerConfig`, broker / result backend factory by type | ~120 |
| `jobs/worker.py` | broker, middlewares, InjectQ, startup / shutdown, discovery | ~120 |
| `jobs/middleware.py` | `TaskMiddleware`, `MonitoringMiddleware` | ~120 |
| `jobs/context.py` | `TaskContext`, `enqueue`, schedule helpers, reserved-argument check | ~100 |
| `src/app/routers/tasks/` | the HTTP routes | ~150 |
| `cli/commands/worker.py` | `worker` and `scheduler` commands | ~60 |

## What we do NOT build (add only when users ask)

Own run store, leases, fencing, sweeper, thread / process executors, run listing for the
playground, SQLite / Postgres backends, killing a running task.

## Sprints

| # | Scope | Exit | Status |
|---|---|---|---|
| 0 | Live checks on test Redis (port 16379, never 6379): `RedisStreamBroker` redelivery after `kill -9` of a worker; progress with taskiq-redis; dynamic schedule add / change time / delete with `ListRedisScheduleSource`; InjectQ `setup_taskiq` + `InjectTaskiq`; adding `config` to kwargs in `pre_execute`; how a middleware skips a cancelled queued task; constructors of each named broker and result-backend type; whether `TaskContext` works as a `TaskiqDepends` class | `JOBS_TASKIQ_SPRINT0.md` with findings; plan fixed where a check failed | Done 2026-11-06: 11 plan changes, all applied below |
| 1 | Old code not on this branch (it is on `worker`). Redis only. `redis_broker.py` (reclaim on idle queue + lease renewal), `config.py`, `worker.py`, `middleware.py`, `context.py`; extras `jobs = ["taskiq>=0.13", "taskiq-redis>=1.2.4"]`; raise the `redis` extra cap (taskiq-redis needs redis-py 8) | unit tests on `memory` broker; mypy and ruff clean | Done 2026-11-06: 1,171 lines in 10 files, 51 jobs tests (Redis incl.), full suite 2907 passed, live kill check passed |
| 2 | `10xgraph worker` and `10xgraph scheduler` commands; `10xgraph init` / `10xgraph config` questions | live run: API process enqueues, worker runs, scheduler fires, worker kill redelivers | Not started |
| 3 | HTTP routes, reserved-argument check, owner scope, permissions, route guard | route tests + live run through the API with JWT (run, schedule, change time, cancel, other user gets 404) | Not started |
| 4 | Docs page and an example project | docs build; example runs | Not started |

Sprints run in order; 1 and the docs draft can overlap. Each sprint: small agents, one review,
fix only high / medium findings, then a live check before it is marked done.

## Housekeeping

- `uv.lock` is stale (regenerate after Sprint 1 changes the extras).
- Core `ddl_session` import is gone with the old code, so the rc5 pin note no longer applies.
