# Jobs: plan

Status: Sprints 0-1 done, Sprint 2 in progress (2026-11-06). Internals (state machine, delivery
guarantees, stores, cluster, research) are in `JOBS_DESIGN.md`. Auth follows in `AUTH_PLAN.md`.

## Decisions

1. Our own asyncio work manager in `tenxgraph_api.jobs`, part of 10xGraph, not a separate library.
2. Run store is the source of truth; the transport only says "run X is ready".
3. Stores: Postgres, Redis, SQLite (`local`), memory. Transports: Redis Streams, SQLite, memory;
   RabbitMQ and Kafka later; NATS and SQS if needed. Postgres is never a transport.
4. `10xgraph worker` runs worker and scheduler in one process. The API only enqueues.
   `10xgraph dev` does not start a worker.
5. No master: pods coordinate through the store and transport; a leader lock in the run store
   runs cron and the sweeper.
6. At-least-once delivery; one attempt runs at a time (lease and fencing); duplicate enqueues
   and cron ticks are deduplicated. External side effects must be idempotent.
7. Executors: `async`, `thread`, `process`.
8. Priority `high`, `normal`, `low` inside each queue; queues for isolation.
9. Pause per queue and per schedule. Resume = rerun after a crash; graph tasks continue from the
   checkpointer.
10. Every run stores its identity. No default identity on enqueue.
11. Local development without Redis via SQLite; fake clock for cron testing.
12. Five triggers: code, HTTP (user), webhook (server REST call with its own auth), event
    (RabbitMQ or Redis message; Kafka later), cron. Each run records its trigger.
13. Lifecycle hooks, lifecycle events and OTEL reuse the graph's: `TaskLifecycleHook`, the core
    publisher (new `task_execution` event), the same `observability` config in API and worker.

## Developer API

### Define a task

```python
from tenxgraph_api.jobs import task, TaskContext, RetryAfter, Fail

@task(http=True, retries=3, timeout=600)
async def summarize_interview(interview_id: str, ctx: TaskContext) -> dict:
    ctx.emit.steps(["load", "summarize"])
    with ctx.emit.step("load"):
        transcript = await load(interview_id)
    with ctx.emit.step("summarize"):
        result = await ctx.invoke_graph({"messages": [transcript]})
    return {"summary": result["messages"][-1].text()}
```

Every `@task` option:

| Option | Default | Meaning |
|---|---|---|
| `name` | function name | Registry name used to enqueue by name, by HTTP, CLI and the store. Two tasks with the same name refuse to boot; set `name=` on one. |
| `queue` | `"default"` | Which workers run it. |
| `priority` | `"normal"` | `"high"`, `"normal"`, `"low"`. |
| `executor` | `"async"` | `"async"` (`async def`), `"thread"` or `"process"` (plain `def`). |
| `retries` | `0` | Extra attempts after a failure. |
| `backoff` | exponential, 2 s base, 10 min cap | Delay between attempts. |
| `timeout` | none | Seconds per attempt. |
| `http` | `False` | Also callable by API clients: `POST /v1/tasks` with a user token (see below). |
| `schedule` | none | Cron string or list. Needs `identity`. |
| `timezone` | `"UTC"` | For `schedule`. |
| `identity` | none | Name from `system_identities`; cron runs as it. |
| `catchup` | `"latest"` | After downtime: fire the latest missed tick once, or `"none"`. |
| `concurrency` | none | Most running at once, cluster-wide. |
| `concurrency_key` | none | Kwarg name or `lambda kwargs, identity: ...`; makes `concurrency` per key. |
| `max_lost` | `2` | Worker crashes tolerated before the run fails. |
| `retention` | `worker.result_ttl` | How long finished runs are kept. |
| `on_success`, `on_failure` | none | `async def hook(run)` called once after the run ends. |

Rules:
- Arguments and results are JSON-serializable, validated from the type hints at enqueue and
  in the worker. Limits: kwargs 256 KB, result 1 MB.
- `ctx: TaskContext` is optional, injected by type. `Inject[...]` works in `async` and
  `thread` tasks.
- Duplicate task names refuse to boot.

### Enqueue

```python
run = await summarize_interview.enqueue(interview_id="42")      # inside a task: inherits identity

run = await summarize_interview.options(
    identity=user,               # required outside a task
    key="interview:42",          # idempotency
    key_mode="keep",             # or "replace": update kwargs while still queued
    priority="high",
    delay=30,                    # or at=datetime
).enqueue(interview_id="42")
```

`.options()` keeps run options apart from task arguments, so a task may have a parameter called
`key` or `delay`.

By name, without importing the function (webhooks, other modules, scripts):

```python
from tenxgraph_api.jobs import enqueue, get_task

run = await enqueue("summarize_interview", {"interview_id": "42"},
                    identity=system_identity("livekit"), key=event.id)

t = get_task("summarize_interview")          # same object as the decorated function
run = await t.options(identity=user).enqueue(interview_id="42")
```

The registry is filled by importing `worker.modules` at startup, in the API process and in every
worker, so the same names exist everywhere. An unknown name raises `TaskNotFound`; kwargs are
validated the same way as a direct call. Enqueue by name works for every task; `http=True` is
not needed for it.

### `http=True`: callable by API clients

Without it, only your server code (routes, webhooks, other tasks, scripts) can enqueue the task.
With it, a frontend, the playground or another service can start it over HTTP:

```
POST /v1/tasks
Authorization: Bearer <user token>
{"task": "summarize_interview", "kwargs": {"interview_id": "42"}, "key": "interview:42"}

202 {"run_id": "7310...", "state": "queued"}
```

- Auth: the caller needs `tasks:trigger`; the run's identity is the caller.
- Only `http=True` tasks; any other name answers 404, so internal task names do not leak.
- Kwargs validated against the type hints (422 on error). Priority and delay cannot be set.
- `GET /v1/tasks` lists these tasks with a JSON schema of their kwargs, which the playground
  turns into a form.
- The caller then polls `GET /v1/tasks/runs/{run_id}` (owner only).

### Run handle and lookup

```python
run.id
await run.status()               # TaskRun: state, attempt, progress, result, error, timestamps
await run.result(timeout=60)     # waits; raises TaskFailed / TaskCancelled / TimeoutError
await run.cancel()
async for event in run.events(after=0): ...

from tenxgraph_api.jobs import runs
run = await runs.get(run_id)
page = await runs.list(task="summarize_interview", state="running", limit=50)
```

Python access is trusted code and is not owner-scoped; HTTP access is.

States: `queued`, `running`, `retrying`, `cancelling`, `succeeded`, `failed`, `cancelled`.

### Inside a task: `TaskContext`

| Member | Meaning |
|---|---|
| `ctx.run_id`, `ctx.attempt`, `ctx.task`, `ctx.queue` | Run facts. Pass `run_id` to external APIs as an idempotency key. |
| `ctx.identity` | Who enqueued it (a system identity for cron and webhooks). |
| `ctx.cancelled` | `True` once cancel is asked; check it in long sync loops. |
| `ctx.emit` | Progress (below). |
| `ctx.thread_id()` | Same on every attempt, derived from `run_id`. |
| `await ctx.invoke_graph(input, config=None)` | Runs the loaded graph with that thread id and the run's identity. A retried run continues from the checkpoint. |

Control flow:

```python
raise RetryAfter(30)          # provider 429: run again in 30 s, no retry used
raise Fail("bad input")       # fail now, no retries
```

`process` tasks get a smaller context: `run_id`, `attempt`, `cancelled`, `emit`. No `Inject`,
no `invoke_graph`.

### Progress: `ctx.emit`

Same style as core's `StreamEmitter`: plain sync calls, safe from any executor.

```python
ctx.emit.steps(["download", "transcribe", "summarize"])   # planned steps, shown as pending
with ctx.emit.step("transcribe", weight=0.6):              # start, end, duration, outcome
    ...
ctx.emit.progress("chunk 12 of 40", percent=35)
ctx.emit.eta(240)                                          # seconds left
ctx.emit.log("cache miss", level="warning", data={"key": k})
```

Writes are buffered and flushed at most once a second, so calling it in a loop is cheap.
Graph tool progress from `invoke_graph` appears as `graph.*` events.

### Cron

```python
@task(schedule="0 2 * * *", timezone="Asia/Dhaka", identity="nightly")
async def nightly_cleanup(ctx: TaskContext) -> None: ...
```

Uses `croniter`. A schedule is validated at registration by calling `get_next` once (`0 0 31 2 *`
passes `is_valid` but never fires). DST: a time inside the spring-forward gap fires at the end of
the gap; a time repeated on fall-back is an open decision: once (recommended, tick id from local
wall time) or twice (croniter default).

### CPU-heavy work

```python
@task(queue="heavy", executor="process", timeout=1800)
def build_mesh(model_id: str, ctx: TaskContext) -> dict: ...
```

Run its own workers: `10xgraph worker --queue heavy --processes 8`. The function must be
module-level and importable, with picklable args and result.

### Five ways to start a task

| Trigger | Who calls | Auth | Opt-in |
|---|---|---|---|
| Code | Your routes, agent tools, other tasks, scripts | Trusted code | Always |
| HTTP | Frontend, playground (a user) | User token, `tasks:trigger` | `@task(http=True)` |
| Webhook | Another server, REST call | Server credential (HMAC, JWT, token, custom) | Listed in the webhook's `tasks` |
| Event | Another system publishes to RabbitMQ or Redis (Kafka later) | Broker access, optional signature | A subscription |
| Cron | The scheduler | System identity | `@task(schedule=...)` |

Every run records its trigger (`{"type": "webhook", "name": "billing"}`, `{"type": "event",
"source": "orders", "topic": "order.created", "offset": ...}`), shown in the run detail.

### Webhooks: REST calls from other servers

A webhook is a named server-to-server endpoint, `POST /v1/webhooks/{name}`, with its own
credential and a `mode` that says what a call does. Mode `task` now; `graph` (invoke the agent)
and others later, without changing the URL shape.

```json
"webhooks": {
  "billing": {
    "auth": {"type": "hmac", "secrets": ["${BILLING_SECRET}"], "header": "X-Signature",
             "timestamp_header": "X-Timestamp", "tolerance": 300},
    "identity": "billing",
    "mode": "task",
    "tasks": ["process_invoice", "refund_invoice"]
  }
}
```

```
POST /v1/webhooks/billing
X-Timestamp: 1762412345
X-Signature: sha256=...           (HMAC of "{timestamp}.{body}")
{"task": "process_invoice", "kwargs": {"invoice_id": "inv-77"}, "key": "inv-77",
 "callback_url": "https://billing.example.com/done"}

202 {"run_id": "7310...", "state": "queued"}

GET /v1/webhooks/billing/runs/7310...      (same credential: status and result)
```

- Only tasks in `tasks` can be started; others answer 404. Kwargs are validated (422).
- `key` dedups retries from the caller. `callback_url` (optional, host must be in the
  webhook's `callback_hosts`) gets a signed POST when the run ends.
- Auth types, all built in: `hmac` (algorithm, hex or base64, prefix, signed-payload template,
  timestamp tolerance), `jwt` (secret or JWKS, issuer, audience), `token` (shared secret
  header), `custom` (`"path": "mod:Class"`, a `WebhookAuth` subclass with
  `async def verify(request) -> None` raising `WebhookRejected`). `secrets` is a list so a
  secret can be rotated without downtime. No webhook without `auth`: boot refuses.
- Verification runs on the raw body before parsing; body limit 1 MB; bad credential is 401 with
  a generic message; the API's rate limiter applies per webhook.
- Runs as the webhook's system identity. The route guard accepts webhook routes because they
  carry `auth`; in AUTH_PLAN Sprint 5 this is `WebhookAuth`.
- Plain custom FastAPI routes can still be mounted with `"routers": ["mod:router"]` and must
  carry normal auth guards.

### Events: start tasks from RabbitMQ or Redis

System B publishes a message; it never calls our API. Workers consume it and enqueue the task.

Two forms:

**Command topic:** the message names the task, like a webhook body.

```json
"events": {
  "sources": {
    "bus": {"type": "rabbitmq", "url": "${RABBITMQ_URL}", "queue": "10xgraph"}
  },
  "commands": [
    {"source": "bus", "topic": "10xgraph.commands", "identity": "bus",
     "tasks": ["process_invoice", "refund_invoice"]}
  ]
}
```

Message: `{"task": "process_invoice", "kwargs": {"invoice_id": "inv-77"}, "key": "inv-77"}`.

**Event subscription:** a normal domain event is mapped to a task.

```python
from tenxgraph_api.jobs import task, on_event, Event

@task(events=[on_event("bus", "order.created", key="{id}", identity="orders")])
async def process_order(event: Event, ctx: TaskContext) -> None:
    order_id = event.json["id"]
```

`Event`: `source`, `topic`, `id`, `key`, `headers`, `body`, `json`, `published_at`. A task
without an `event` parameter can map fields instead:
`on_event("bus", "order.created", kwargs={"order_id": "{id}"})`.

Rules:
- Source types in v1: `redis` (Streams) and `rabbitmq`. Kafka later, when needed; NATS and SQS
  after that. Connection code is shared with core's publishers (`tenxgraph/runtime/publisher/`) and the job transports.
- Consumers run inside `10xgraph worker`. All pods share one consumer group (Redis) or one
  queue as competing consumers (RabbitMQ), so each message goes to one pod. `--no-events` turns them off on a pod.
- At least once: the message is acked only after the run is written. The run key is the
  subscription's `key` template when set, else the message id (Redis stream entry id, RabbitMQ
  `message_id` property), else a hash of the body, so a redelivered message never creates a
  second run. RabbitMQ delivery tags change on redelivery, so they are never used as the key.
- A message that fails parsing, validation or an unknown task goes to the source's `dead_letter`
  topic when set, and is always recorded in `GET /v1/tasks/events/rejected`.
- Order: runs start in arrival order but may finish out of order; for one-at-a-time per key use
  `concurrency=1, concurrency_key=...`.
- Auth: broker access control (network, ACLs, credentials in `url`), plus optional `verify`
  (same HMAC options as webhooks) on the message. Runs as the configured system identity.

### Lifecycle and observability: same as the graph

Tasks reuse what graphs already have; there is no second observability system.

**Lifecycle hooks**, shaped like core's `GraphLifecycleHook` (`tenxgraph/utils/callbacks.py`):

```python
from tenxgraph_api.jobs import TaskLifecycleHook

class Audit(TaskLifecycleHook):                 # every method is an optional no-op
    async def on_task_queued(self, run): ...
    async def on_task_start(self, run): ...
    async def on_task_progress(self, run, event): ...
    async def on_task_retry(self, run, error): ...
    async def on_task_success(self, run, result): ...
    async def on_task_failure(self, run, error): ...
    async def on_task_cancel(self, run): ...
    async def on_task_lost(self, run): ...      # worker died, run requeued
```

Registered with `"worker": {"hooks": ["audit:Audit"]}`. `on_task_queued` runs where the
enqueue happens (API or worker); the rest run in the worker. A hook error is logged and never
changes the run. Per-task `on_success` / `on_failure` stay as a shortcut.

**Lifecycle events through the core publisher.** Each step above is also published as a core
`EventModel` through the `BasePublisher` bound in the InjectQ container, the same publisher the
graph uses (`OtelPublisher`, `RabbitMQPublisher`, `RedisPublisher`, `CompositePublisher` for
several). Core gets one new `Event` value, `task_execution`; the phase uses the existing
`EventType`:

| Step | `EventType` | `data` |
|---|---|---|
| queued | `update` | `state`, task, queue, priority, trigger |
| start | `start` | attempt, worker_id |
| progress | `progress` | percent, step, message |
| retry | `error` | error, next attempt, `available_at` |
| success | `end` | duration, result reference |
| failure | `error` | error, final |
| cancel, lost | `interrupted` | reason |

`run_id` is the task run id, `user_id` comes from the identity, `metadata` holds the trigger.
A graph run inside a task (`ctx.invoke_graph`) publishes its usual graph events with the task
run id in `metadata`, so a consumer can join them. Turn off with
`"worker": {"publish_events": false}`.

**OTEL from the same config.** The worker runs the same setup as the API
(`_setup_observability` in `core/config/setup_middleware.py`, the `observability` block of
`10xgraph.json`), so traces go to the same Logfire / LangSmith / OTEL exporter with no extra
config. `OtelPublisher` turns `task_execution` start and end into a `task.run` span (task,
run id, attempt, queue, trigger); the graph's spans nest under it. The enqueuer's
`traceparent` is stored on the run, so an HTTP call, webhook or event joins the same trace as
the task and its graph. Metrics (queue depth, oldest-queued age, runs, failures, retries,
lease losses, attempt duration) use the OTEL meter `10xgraph`; `--metrics-port` additionally
serves them in Prometheus format.

**Callbacks to the caller:** `callback_url` on webhook calls, signed with the webhook secret
and retried with backoff.

### Production extras

- Backpressure: `@task(max_queued=N)` or per queue; over it, `enqueue` raises `QueueFull`,
  HTTP and webhooks answer 429, event consumers pause that subscription.
- Bulk: `await enqueue_many("task", [kwargs, ...], identity=...)` writes and publishes in batches.
- Operations: bulk retry or cancel by filter (`POST /v1/tasks/runs/retry`, `/cancel` with a
  filter, `tasks:pause`), purge a queue, list failed runs as the dead-letter view.
- Changed task code: queued runs are checked against the current signature when claimed; a
  mismatch fails with a clear error instead of crashing. `@task(version=2)` is stored on the run.
- Large data: results over the limit go to the media store and the run keeps a reference.
- Sensitive kwargs: `@task(redact=["card_number"])` hides fields in the API, logs and events
  (stored values are still visible to the worker).
- One time limit, `timeout`: the attempt fails with `timeout` (retried if `retries` are left).
  An `async` task is cancelled, a `process` child is killed, a `thread` run is abandoned (its
  result is discarded when the function returns).
- Retries apply to any error; `raise Fail(...)` fails at once with no retry.
- Leaky code: `--max-tasks-per-child N` and `--max-memory-per-child MB` replace a `process`
  child after N runs or above the memory limit.
- Health: `--health-port` serves `/healthz` (loop alive) and `/readyz` (store and transport
  reachable, not draining) for Kubernetes probes.
- Clock: leases and `available_at` use the store's clock (Postgres `clock_timestamp()`, not
  `now()`; Redis `TIME`; SQLite epoch), never the worker's, so clock skew between pods cannot
  steal runs.
- Redis transport: the worker uses two clients. The blocking-read client has
  `socket_timeout=None`, keepalive, `health_check_interval=30` and its own reconnect loop, reads
  with BLOCK 2-5 s (never 0) inside an `asyncio.timeout`; the short-command client (ack, lease,
  store) keeps `socket_timeout=5`. Never reuse the API's Redis client for blocking reads.
  `high` is read alone first, then one blocking read over the rest. `xautoclaim` runs without
  `justid`.
- Process executor: one supervised child process per slot (not `ProcessPoolExecutor`, which
  breaks the whole pool when one child is killed), forkserver with the task modules preloaded.
  `executor="process"` needs a module-level importable function (checked at `@task`) and
  picklable args and results. `ctx.emit` and cancel travel over the slot's pipe and event.
  `--max-memory-per-child` polls `/proc` on Linux (psutil elsewhere). A thread task abandoned
  on timeout holds its slot until it returns.
- Graph tasks: every attempt uses the same `thread_id`; the graph `run_id` is
  `{run_id}:{attempt}`; the input is sent on attempt 1 only, later attempts send
  `{"messages": []}`. The node that was running re-runs (at-least-once per node). The
  checkpointer is closed on shutdown.
- RabbitMQ: messages we publish are `PERSISTENT` and carry a `message_id`.
- Own storage, created only when the `worker` section exists (no `worker` section: no tables,
  no keys, nothing created):
  - Redis: jobs use their own logical database, `worker.redis_db` (default `1`, so not the
    cache store's usual `0`). If the server does not allow it (Redis Cluster and some managed
    services only have database `0`), jobs fall back to database `0` with every key under
    `10xgraph:jobs:` and log that they did.
  - Postgres: own DSN (`worker.store.url`) and a small pool (max ~5 per pod); own tables
    `tenxgraph_task_*` (runs, events, workers, leader, counters, schedules, rejected events),
    created under an advisory lock with a schema version by the store's setup or
    `10xgraph tasks migrate [--check]`. Behind PgBouncer transaction pooling, migrate needs a
    direct or session-mode DSN.
  - SQLite: its own file, `.10xgraph/jobs.db`.
  - Docs and the boot log tell users to give each app and environment its own Redis database
    or server instead of sharing one.
- Upgrades: the store has a schema version and `10xgraph tasks migrate`; every transport message
  carries a format version, so old and new pods can run side by side during a rolling deploy.
- Logs: one structured line per state change with `run_id`, `task`, `attempt`, `trace_id`.

Later (not v1): Kafka, NATS, SQS; workflows (chains, fan-out, fan-in, batches); cluster-wide
rate limits; debounce and throttle; round-robin fairness; schedules edited over the API;
versioned draining; encryption of stored kwargs and results.

### Testing

```python
from tenxgraph_api.jobs.testing import jobs_harness

async def test_summary():
    async with jobs_harness() as jobs:                     # memory store, fake clock, in-process worker
        run = await summarize_interview.options(identity=jobs.user("u1")).enqueue(interview_id="42")
        await jobs.drain()                                 # run everything due
        assert (await run.status()).state == "succeeded"

        jobs.clock.advance(hours=24)                       # cron ticks fire
        await jobs.drain()
```

## Config

```json
"worker": {
  "modules": ["jobs.interviews", "jobs.cleanup"],
  "transport": {"type": "redis", "url": "${REDIS_URL}"},
  "store": {"type": "postgres", "url": "${DATABASE_URL}"},
  "result_ttl": 604800,
  "system_identities": {"nightly": {"user_id": "__system__:nightly"}}
}
```

Optional store keys: `schema` (Postgres, default `public`), `auto_migrate` (default true).
Dependencies: the `jobs` extra needs `aiosqlite` (local backend); the API `redis` extra must
drop its `<7` cap (Sprint 1 change, redis-py 8 is verified).

Local development: `"worker": {"modules": "jobs", "backend": "local"}` (SQLite file
`.10xgraph/jobs.db`, WAL, `busy_timeout` 5000, `BEGIN IMMEDIATE`). `system_identities` moves under `auth` in AUTH_PLAN Sprint 5.

## CLI

```
10xgraph worker [--queue NAME ...] [--concurrency 16] [--threads 4] [--processes 2]
                [--reserve high=2] [--no-scheduler] [--shutdown-timeout 30] [--metrics-port N]
10xgraph worker --clock 2026-11-07T01:59:50 --clock-speed 60      # dev only
10xgraph tasks enqueue NAME --kwargs '{...}'
10xgraph tasks runs [--state running]
10xgraph tasks fire NAME                                           # run a schedule now
10xgraph tasks workers
10xgraph tasks migrate [--check]                                   # create or upgrade store tables
```

## HTTP

| Route | Permission |
|---|---|
| `GET /v1/tasks` (HTTP tasks with kwargs schema) | `tasks:read` |
| `POST /v1/tasks` `{task, kwargs, key?}` returns 202 `{run_id, state}` | `tasks:trigger` |
| `GET /v1/tasks/runs`, `GET /v1/tasks/runs/{id}`, `GET /v1/tasks/runs/{id}/events?after=` | `tasks:read` |
| `POST /v1/tasks/runs/{id}/cancel`, `POST /v1/tasks/runs/{id}/retry` | `tasks:cancel`, `tasks:trigger` |
| `GET /v1/tasks/schedules`, `POST .../schedules/{name}/pause` and `/resume` | `tasks:read`, `tasks:pause` |
| `GET /v1/tasks/queues`, `POST .../queues/{queue}/pause` and `/resume` | `tasks:pause` |
| `GET /v1/tasks/workers` | `tasks:pause` |
| `GET /v1/tasks/events/rejected` (event messages that could not start a run) | `tasks:pause` |
| `POST /v1/webhooks/{name}`, `GET /v1/webhooks/{name}/runs/{id}` | The webhook's own `auth` |

Runs are visible to their owner only; others get 404. HTTP callers cannot set priority.

Client SDK: `tasks.list`, `trigger`, `runs`, `run`, `events`, `cancel`, `retry`, `schedules`,
`queues`, `workers`, pause and resume. Playground: a Tasks view on top of these.

## Sprints

Each sprint ends with its suite green (run sequentially, memory limit), an Opus review of the
concurrency and security code, a short report and a user test. Opus writes the worker, stores,
leases, cancel, shutdown, scheduler, webhook auth and event consumers; Sonnet writes routes,
CLI, tests, docs and the playground.

| # | Scope | Exit | Status |
|---|---|---|---|
| 0 | Verify Redis Streams on redis-py 8.x, Postgres table setup, graph resume, killing a process child, cron library, aio-pika. See `JOBS_SPRINT0.md`. | Findings note; plan updated. | Done (2026-11-06) |
| 1 | Core on memory: `@task`, registry, enqueue (by object and name), `.options()`, run handle, state machine, async executor, retries, `Fail`, one timeout, cancel, leases, fencing, `ctx.emit`, caps, `RetryAfter`, hooks, `jobs_harness`. | State-machine property test; harness example passes. | Done (2026-11-06): 194 jobs tests, full suite green |
| 2 | Stores and transports: Postgres, Redis, SQLite `local`; sweeper; store clock; own Redis database and Postgres tables created only with a `worker` section; schema version and `tasks migrate`; one contract suite for all. | Fault injection on real Redis and Postgres: kill -9, SIGSTOP past the lease, duplicate delivery, store error mid-claim. | In progress |
| 3 | `10xgraph worker`: config and validator, queues, priority, reserved slots, thread and process executors, child recycling, InjectQ, `invoke_graph`, registry, draining, health probes, `tasks` CLI. | User test: two workers, kill one mid-run, the run finishes on the other. | Not started |
| 4 | Scheduler: leader lock in the run store, cron, catchup, pause, sweeper fallback, fake clock, `tasks fire`. | Three schedulers fire each tick once; store outage gives no duplicates. | Not started |
| 5 | HTTP routes and client SDK; webhooks (`/v1/webhooks/{name}`, mode `task`, HMAC / JWT / token / custom, secret rotation, callbacks); `routers` key; route-guard support. | Owner-only 404s, bad signature 401, replayed call deduped, non-listed task 404. | Not started |
| 6 | Events: Redis and RabbitMQ sources, command topics, subscriptions, dead letter, rejected list. Lifecycle hooks and events through the core publisher; core `Event.TASK_EXECUTION` and `OtelPublisher` support; OTEL spans, trace propagation, metrics; backpressure, bulk, operations. | One trace from webhook to task to graph in a real exporter; redelivered message makes no second run. | Not started |
| 7 | Playground Tasks view (tasks, runs, progress, events, schedules, workers, rejected events); `10xgraph build` worker service; docs. | Live check: real Redis, Postgres and RabbitMQ, two worker pods, cron, a CPU task, a webhook and an event. Then CHANGELOG. | Not started |

Rough size: about 5,500 lines of product code and 6,000 of tests (the earlier 3,550 plus
webhooks, events, lifecycle and observability, and the production extras).
