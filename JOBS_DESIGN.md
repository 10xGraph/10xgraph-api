# Jobs: internal design

Reference for implementers (2026-11-06). `JOBS_PLAN.md` is the short plan and owns the
developer API; where the API examples here differ (for example `.options(...).enqueue()`),
`JOBS_PLAN.md` wins.
Order: this plan, then build the worker, then implement `AUTH_PLAN.md` (root). The worker ships
with today's auth and switches to `Identity` in AUTH_PLAN Sprint 5.

## Why our own

TaskIQ has no cancel, no run listing and no scheduler lock (checked in taskiq 0.13.0), and
`taskiq-redis` 1.2.4 needs `redis>=8` while our `redis` extra pins `>=5.0.7,<7`. Status, cancel,
listing, pause, the scheduler and the routes would be our code either way, so a TaskIQ layer
would save only the Redis consumer (about 800 lines) and make its decorator public API we would
later have to break.

## Decisions

| # | Decision |
|---|---|
| J1 | Own work manager on asyncio, in the API package: `tenxgraph_api.jobs`. |
| J2 | The developer API is ours: `@task`, `.enqueue()`, `TaskContext`. Routes, docs and the playground depend only on it. |
| J3 | Two separate parts: the **transport** carries a wake-up message (`run_id`), the **run store** is the single source of truth for each run (state, attempt, lease, result). |
| J4 | First release: Redis Streams transport; Postgres and Redis run stores; a SQLite `local` backend (store and transport in one file) so local development needs no Redis; in-memory for tests and single-process use. Later: RabbitMQ and Kafka (reusing the core publisher connection code), then NATS and SQS only if needed. Postgres is never a transport. |
| J10 | Priority: every run has `high`, `normal` or `low` (default `normal`), set on `@task` and overridable on `.enqueue()`. A worker takes higher priority first, with an anti-starvation rule and optional reserved slots. Queues stay for isolation (separate workers); priority orders work inside a queue. |
| J12 | Progress from inside a task: `ctx.emit`, shaped like core's `StreamEmitter` (sync, thread-safe methods). Percent, steps with durations, messages and custom data go to the run store as a latest snapshot plus a bounded event log. |
| J13 | No master. Every worker is equal; they coordinate only through the shared store and transport: self-registration with heartbeats, consumer groups for work sharing, a leader lock for the few singleton duties. Kubernetes (or Compose) only starts and stops pods. |
| J14 | Taken from other systems for v1 (research 2026-11-06, sources in "Prior art"): cluster-wide concurrency caps per task and per key, `replace` mode for `key=`, `RetryAfter` for provider 429s, a cap on lost leases, `on_success` / `on_failure` hooks, per-task retention, a Prometheus `/metrics` endpoint, and schema columns for batches, parent runs and code version (logic later). |
| J11 | Local testing tools: a fake clock for the worker and `10xgraph tasks fire` to run a schedule now. Both refuse to run under `MODE=production`. |
| J5 | `10xgraph worker` runs the worker and the scheduler in one process (one Docker service, any number of replicas). The API process only enqueues. `10xgraph dev` does not start a worker: developers run `10xgraph worker` in a second terminal. |
| J6 | Delivery is at least once. A run is never executed by two workers at the same time (lease plus fencing), and duplicate deliveries, enqueues and cron ticks do not create extra runs. Side effects outside the store are the task's job to make idempotent (`ctx.run_id` is the key to pass on). |
| J7 | Executors: `async` (default), `thread` (sync code), `process` (CPU-heavy code, its own child process). |
| J8 | Pause is per queue and per schedule, not per run. Resume means a run that was interrupted (crash, lost lease, shutdown) runs again; a task that drives a graph continues from the graph checkpointer, a plain function starts over. |
| J9 | Every run stores the identity that enqueued it. Enqueue needs an explicit identity or one inherited from the current task; there is no silent default user. |

## Developer API

```python
from tenxgraph_api.jobs import task, TaskContext

@task(http=True, retries=3, timeout=600)
async def summarize_interview(interview_id: str, ctx: TaskContext) -> dict:
    ctx.emit.progress("loading transcript", percent=10)
    with ctx.emit.step("transcribe", weight=0.6):          # start, end and duration recorded
        ...
    result = await ctx.invoke_graph({"messages": [...]})   # thread_id stable across attempts
    return {"summary": result["messages"][-1].text()}

@task(schedule="0 2 * * *", timezone="UTC", identity="nightly")
async def nightly_cleanup(ctx: TaskContext) -> None: ...

@task(queue="heavy", executor="process", timeout=1800)
def build_mesh(model_id: str) -> dict: ...                # sync, picklable args and result

run = await summarize_interview.enqueue(interview_id="42", key="interview:42", identity=user)
await run.status()          # RunRecord
await run.wait(timeout=60)  # result or raises
await run.cancel()
```

`@task` options:

| Option | Default | Meaning |
|---|---|---|
| `name` | `module.function` | Registry name, used by HTTP and the store. Duplicate names refuse to boot. |
| `queue` | `"default"` | Routing. A worker consumes the queues it is started with. |
| `priority` | `"normal"` | `high`, `normal` or `low`. Code can override it on `.enqueue(priority=...)`; `POST /v1/tasks` cannot (callers would mark everything high). |
| `executor` | `"async"` | `async`, `thread` or `process`. `async` needs an `async def`; `thread` and `process` need a plain `def`. |
| `retries` | `0` | Extra attempts after a failure. |
| `backoff` | exponential, 2 s base, 10 min cap, jitter | Delay between attempts. |
| `timeout` | none | Seconds per attempt. A timeout cancels the attempt and counts as a failure. |
| `http` | `False` | Exposed through `POST /v1/tasks`. |
| `schedule` | none | Cron string or list of them. Needs `identity`. |
| `timezone` | `"UTC"` | For `schedule`. |
| `identity` | none | A name from `system_identities`, used by cron runs. |
| `catchup` | `"latest"` | After scheduler downtime: `"latest"` fires the most recent missed tick once, `"none"` skips. |
| `concurrency` | none | Most runs of this task running at once across the whole cluster. |
| `concurrency_key` | none | Callable or kwarg name (`"tenant"`, `lambda kw, identity: identity.tenant.get("org_id")`). With `concurrency`, the cap applies per key, so one tenant or user cannot take every slot. |
| `max_lost` | `2` | Lease losses (worker died) before the run fails, so a job that kills its worker cannot loop forever. Counted apart from `retries`. |
| `retention` | `worker.result_ttl` | How long finished runs of this task are kept. |
| `on_success`, `on_failure` | none | Async hooks called once after the terminal write, with the run record. Failures in hooks are logged, never change the run. |

Rules:
- Arguments and results are JSON (pydantic `TypeAdapter` from the signature). Kwargs are
  validated at enqueue and again in the worker. Limits: kwargs 256 KB, result 1 MB (configurable);
  larger data goes to files or the store, and the task returns a reference.
- `ctx: TaskContext` is optional and injected by type. It gives `run_id`, `attempt`,
  `identity`, `emit` (see "Progress"), `cancelled` (for cooperative checks in sync code),
  `thread_id()` (derived from `run_id`, same on every attempt) and `invoke_graph(...)`.
- `Inject[...]` from InjectQ works in `async` and `thread` tasks. `process` tasks get plain
  arguments only (the child does not load the container).
- `.enqueue()` also takes `delay=` / `at=` (delayed start) and `key=` (idempotency, see below).
  `key_mode="keep"` (default) returns the existing run; `key_mode="replace"` swaps the kwargs
  of a run that is still `queued` (the common "re-summarize after an edit" case) and behaves
  like `keep` once it is running or finished.
- `raise RetryAfter(seconds)` inside a task (for a provider 429) puts the run back to
  `queued` with `available_at = now + seconds`, without using an attempt. Capped by
  `timeout` and a per-run limit of 50 deferrals, so it cannot loop forever.
- Concurrency caps are checked in the claim itself. A plain `count(*)` is not safe under
  concurrent claims (two transactions both see room), so each `(task, key)` has a counter row
  taken in the claim transaction with `UPDATE ... SET running = running + 1 WHERE running <
  cap` and given back on every exit from `running` (finish, cancel, retry, lost lease via the
  sweeper). Redis does the same in its claim Lua script; SQLite has one writer at a time. A run over its cap is not claimed: it stays `queued` with
  `available_at` pushed by a short backoff (1 s growing to 30 s), and the message is acked; the
  sweeper republishes it. No slot is held while waiting.
- Identity for `.enqueue()`: the `identity=` argument, else the identity of the task currently
  running (contextvar), else an error. A webhook passes a system identity by name.

## Progress (`ctx.emit`)

Modelled on core's `StreamEmitter` (`tenxgraph/core/state/stream_emitter.py`): plain sync
methods, safe from the event loop and from `thread` tasks (`loop.call_soon_threadsafe`), so the
same code works in every executor.

| Call | Records |
|---|---|
| `ctx.emit.progress(message, percent=None, data=None)` | Latest message and percent (0 to 100, never decreasing within an attempt). |
| `ctx.emit.step(name, weight=None)` (context manager, sync and async) | Step start, end, duration and outcome. With weights, percent is computed from finished steps. |
| `ctx.emit.steps(["download", "transcribe", "summarize"])` | Declares the plan up front, so the UI shows pending steps. |
| `ctx.emit.eta(seconds)` | Estimated time left. |
| `ctx.emit.log(message, level="info", data=None)` | A free event line. |

Storage and rate:
- The run record keeps a `progress` snapshot (message, percent, current step, steps with
  durations, eta). The `tenxgraph_task_events` log (Redis: a capped stream per run; SQLite and
  Postgres: a table) keeps the last 500 events per run, each with a sequence number.
- What becomes an event: every `step` start and end, every `log`, `steps` and `eta` call is
  its own event. `progress` calls are coalesced: one event per flush with the latest message
  and percent, so a tight loop does not fill the 500-event window.
- Percent: an explicit `percent=` wins until the next step ends; at a step end with weights,
  percent becomes the sum of finished step weights (never lower than the last value shown).
- Calls only write to an in-memory buffer. The worker flushes at most once a second per run,
  in the same batched call as the heartbeat, and always at step end and on finish. A loop that
  calls `progress` 10,000 times costs at most one write a second.
- Flushes are fenced like every other worker write (attempt and worker id), so a stale worker
  cannot overwrite the progress of the attempt that replaced it. A new attempt starts a fresh
  snapshot; the event log keeps earlier attempts, tagged with their attempt number.
- `process` tasks: the child sends emitter calls to the parent over its result pipe; the parent
  buffers and flushes them.
- When a task runs a graph (`ctx.invoke_graph`), the graph's `StreamEmitter` tool progress is
  forwarded into the event log as `graph.*` events, so a long agent run shows tool steps too.
- Read side: the run record (`GET /v1/tasks/runs/{run_id}`) and
  `GET /v1/tasks/runs/{run_id}/events?after=<seq>` (poll with a cursor; server-sent events later).

## Stores and transports

Two different jobs. A **run store** is the result backend: state, progress, events, result,
leases, schedules, worker registry. A **transport** carries "run X is ready" to a worker.

| Backend | Run store (result backend) | Transport (consumer) | When |
|---|---|---|---|
| Memory | yes | yes | Tests, single process |
| SQLite `local` | yes | yes (polling the same file) | Local development, one machine |
| Postgres | yes | no (never a transport, J4) | Production store |
| Redis | yes | yes (Streams, consumer groups) | Production transport; store when there is no Postgres |
| RabbitMQ | no | yes | Later |
| Kafka | no | yes | Later |
| NATS, SQS | no | yes | Only if needed |

RabbitMQ and Kafka are only transports: they move messages, they are not a place to query a
run's status. Any transport pairs with any production store (for example Kafka plus Postgres).
Each store and each transport implements one contract and passes one shared test suite.

## Cluster: many workers, no master

There is no control plane. `10xgraph worker` is the same image and command on every pod, and
every pod is equal:

| Need | How it works without a master |
|---|---|
| Joining | A worker starts, writes its own registry entry (`worker_id`, host, pod name, version, queues, slots, started_at), and joins the consumer group of each queue. Nobody assigns it anything. |
| Sharing work | The transport hands each message to one consumer (Redis consumer groups, the claim statement for SQLite and Postgres). A worker with free slots asks for more; a busy one does not. Load spreads by itself. |
| Being alive | The registry entry is renewed with the heartbeat (every `lease_ttl / 3`) and expires after `lease_ttl`. Run leases are the real safety; the registry is for visibility. |
| Leaving | On SIGTERM a worker marks itself `draining`, stops reading, finishes or requeues, deletes its entry and leaves the consumer group (`XGROUP DELCONSUMER` once it has no pending messages). |
| Dying | Its leases expire, the sweeper requeues its runs (or fails them past `max_lost`), and the leader removes its stale registry entry and idle consumer. Dead consumer names otherwise stay in a Redis group forever, so this cleanup is ours. |
| Singleton duties | Scheduler ticks, sweeper and retention run on whichever worker holds the leader lock. If it dies the lock expires and another worker takes it within `lease_ttl`. |
| Registry storage | The `tenxgraph_task_workers` table (Redis: a hash with per-entry expiry) in the run store, not the Redis consumer list, so dashboards and autoscaling queries work with any transport. |
| Scaling | Kubernetes or Compose changes the replica count. Best signal: age of the oldest queued run, then depth. Either KEDA's PostgreSQL scaler with a query on the run store (one number per queue), or its Redis Streams scaler on `lagCount` summed over the three priority streams (`streamLength` does not drop after ack, so it is the wrong signal). |
| Different pools | The same image with different flags: a `light` Deployment (`--queue default`) and a `heavy` one (`--queue heavy --processes 8`). Each pool scales on its own queue. |

API pods and worker pods use the same image and config; the API Deployment runs the server, the
worker Deployment runs `10xgraph worker`. With 100 worker pods, the work per pod is constant:
one blocking read per queue and priority, one batched heartbeat a second for the runs it holds.
The leader's sweeper reads by index (`state, lease_expires_at`), not by scanning.

## Config

```json
"worker": {
  "modules": ["jobs.interviews", "jobs.cleanup"],
  "transport": {"type": "redis", "url": "${REDIS_URL}"},
  "store": {"type": "postgres", "url": "${DATABASE_URL}"},
  "result_ttl": 604800,
  "lease_ttl": 60,
  "max_result_bytes": 1048576
}
```

- No `worker` section: no routes, no worker, nothing imported. The jobs code ships with
  `pip install "10xgraph-api[worker]"`.
- `modules`: dotted module paths imported in both the API and the worker so the registry is the
  same in both. An import error is a startup error; zero tasks logs a warning.
- `transport.type`: `redis`, `local` or `memory`. `store.type`: `postgres`, `redis`, `local`
  or `memory`. `url` supports `$VAR` / `${VAR}`. A missing URL is a startup error, never a
  silent default.
- Shortcut for local development: `"worker": {"modules": "jobs", "backend": "local"}` sets both
  to `local` with the file `.10xgraph/jobs.db`. `local` transport needs `local` store (same file).
- `memory` works only when enqueue and execution share one process (tests, scripts).
  `10xgraph worker` and the API server refuse `memory`, with a message pointing to `local`.
- `system_identities` live under `auth` (AUTH_PLAN). Until AUTH_PLAN lands, a cron task's
  `identity` names an entry in a temporary `worker.system_identities` map
  (`{"nightly": {"user_id": "__system__:nightly"}}`), moved to `auth` in AUTH_PLAN Sprint 5.
- The same validator runs at boot, in `10xgraph config`, `10xgraph audit` and `10xgraph build`
  (AUTH_PLAN "Config rules" pattern).

CLI:

```
10xgraph worker [--queue default --queue heavy] [--concurrency 16] [--threads 4]
                [--processes 2] [--no-scheduler] [--shutdown-timeout 30]
```

Dev-only flags (refused under `MODE=production`): `--clock 2026-11-07T01:59:50` starts the
worker's clock at that time and lets it run on from there, `--clock-speed 60` makes one real
second count as one minute. The clock drives the scheduler, delays, backoff and leases in that
worker only.

```
10xgraph tasks fire nightly_cleanup      # insert the run for "now" as the scheduler would
10xgraph tasks runs [--state running]    # list runs from the configured store
10xgraph tasks enqueue summarize_interview --kwargs '{"interview_id": "42"}'
```

Startup order: env, `load_container`, `load_graph`, `modules`, store `setup()`, transport
connect, then consume. `--no-scheduler` lets a deployment run the scheduler only on some
replicas; with the default every replica takes part and one wins the leader lock.

## Run record and states

Postgres table `tenxgraph_task_runs` (Redis store: one hash per run plus sorted-set indexes,
all transitions in Lua scripts):

| Column | Notes |
|---|---|
| `run_id` | Primary key. Snowflake id, or deterministic for cron (below). |
| `task`, `queue` | |
| `state` | See states below. |
| `attempt`, `max_attempts` | `attempt` is incremented on every claim and is the fencing token. |
| `kwargs` | jsonb |
| `identity` | jsonb snapshot of who enqueued it |
| `owner_user_id`, `tenant` | Copied from the identity, indexed for owner-scoped reads. |
| `idempotency_key` | Unique per `(task, owner_user_id, tenant, idempotency_key)` when set. |
| `schedule_tick` | Unique per `(task, schedule_tick)` for cron runs. |
| `worker_id`, `lease_expires_at` | Set while running. |
| `cancel_requested` | Own column, so a cancel never overwrites a worker's state write. |
| `priority`, `concurrency_key` | Used by the claim order and the concurrency caps. |
| `lost_count`, `defer_count` | Lease losses (`max_lost`) and `RetryAfter` deferrals. |
| `parent_run_id`, `batch_id`, `code_version` | Stored from v1, no logic yet (batches, child runs and versioned draining are later). |
| `available_at` | When the run may be claimed (delay, retry backoff). |
| `progress`, `result`, `error` | jsonb; `error` is sanitized in production (same rule as error chunks). |
| `enqueued_at`, `published_at`, `started_at`, `finished_at`, `updated_at`, `expires_at` | `expires_at` = finish + `result_ttl`. |

Indexes: `(owner_user_id, enqueued_at desc)`, `(state, available_at)`,
`(state, lease_expires_at)`, `(expires_at)`.

States:

| State | Meaning | Next |
|---|---|---|
| `queued` | Waiting (possibly until `available_at`) | `running`, `cancelled` |
| `running` | Claimed, lease held | `succeeded`, `failed`, `retrying`, `cancelling`, `queued` (lost lease or shutdown) |
| `retrying` | Failed, waiting for backoff | `running`, `cancelled` |
| `cancelling` | Cancel asked while running | `cancelled` |
| `succeeded`, `failed`, `cancelled` | Terminal | none |

Every transition is a compare-and-set on `(run_id, state, attempt)`. A write that matches no row
changes nothing and tells the caller it lost.

## Delivery guarantees

The stream is only a wake-up signal. Correctness lives in the run store.

| Risk | Guard |
|---|---|
| Enqueue crashes between store write and publish | The run is written first (`published_at` null), then published. The sweeper republishes `queued` runs not picked up within 5 min. A duplicate message is harmless (next row). |
| Two workers get the same message | Claim = CAS `queued/retrying` to `running`, `attempt + 1`, `worker_id`, `lease_expires_at`. Only one wins; the loser acks and skips. |
| Long task holds a transport message | The worker acks the message right after a successful claim. Long runs depend on the store lease, not on the broker (this also avoids RabbitMQ's consumer timeout and Kafka rebalance limits later). |
| Worker dies between receive and claim | The message is not acked; Redis Streams `XAUTOCLAIM` gives it to another consumer after `lease_ttl`. |
| Worker dies mid-run | It stops renewing the lease (every `lease_ttl / 3`). The sweeper CASes `running` with an expired lease back to `queued` with `lost_count + 1` (retries are not used up; the next claim still bumps `attempt` as the fencing token), or to `failed` once `lost_count` passes `max_lost`, and republishes. |
| A stalled worker wakes up after losing its lease | Every worker write (progress, result, state) matches `attempt` and `worker_id`. A stale write matches nothing; the worker cancels its task and discards the result. |
| The same job is triggered twice | `key=` on enqueue: the insert hits the unique key and returns the existing run. |
| Two schedulers fire one tick | Leader lock (below), plus the deterministic id `cron:<task>:<tick UTC ISO>` inserted only if new. |
| Cancel races with completion | Cancel sets `cancel_requested`; the worker's terminal write is a CAS. Whichever lands first wins and the other sees it. |
| Retry and delayed runs | No broker delay needed: the sweeper republishes runs whose `available_at` has passed. |

The sweeper runs on the scheduler leader every second (batched, `FOR UPDATE SKIP LOCKED` on
Postgres). Running it twice is safe because every step is a CAS.

## Local backend (SQLite)

- One file holds the runs table (same columns as Postgres) and acts as the queue. WAL mode, so
  the API process and one or more worker processes on the same machine share it.
- Claim is a single statement: `UPDATE ... SET state='running', attempt=attempt+1, ... WHERE
  run_id = (SELECT run_id ... WHERE state IN ('queued','retrying') AND available_at <= now AND
  queue IN (...) ORDER BY priority, available_at LIMIT 1) RETURNING *` (SQLite 3.35+; the venv
  has 3.53). Uses `aiosqlite`, already used by core's SQLite checkpointer.
- Workers poll every 200 ms when idle (no broker to push). Fine for development, not for many
  machines: `10xgraph audit` and `build` warn when `local` is used with `MODE=production`.
- It runs the same state machine, leases, sweeper, scheduler and cancel code as Redis and
  Postgres, and is part of the same contract test suite, so behaviour seen locally matches
  production.

## Processes and event loops

| Process | Event loops | Runs inside |
|---|---|---|
| API (`10xgraph dev`, or each Gunicorn worker) | 1 | Request handling; `.enqueue()` writes the run and publishes. Never executes tasks. |
| `10xgraph worker` | 1 (main thread) | Consumer per queue, up to `--concurrency` async task slots, heartbeat, cancel watcher, scheduler (tries for leader), sweeper and retention (leader only). |
| Thread pool of that worker (`--threads`) | 0 | `thread` tasks; results handed back to the loop. |
| Process children (`--processes`) | 0 per child | `process` tasks, one at a time per child; the parent's loop waits on them and can kill them. |

Two workers are two processes with one loop each. They are consumers in the same group (Redis)
or race on the same claim statement (local); either way the run store claim decides who runs a
job. Scaling is more replicas, or more slots per replica; one replica never runs more than one
event loop.

## Priority

- Redis: one stream per queue and priority, `tenxgraph:<queue>:high|normal|low`. A free slot
  first does a non-blocking read on `high`, then `normal`, then `low`; when all are empty it
  blocks on all three at once and takes whatever arrives.
- Local and Postgres claim order: `ORDER BY priority, available_at`.
- Anti-starvation: after 10 claims in a row from a higher level while a lower one has work, the
  next free slot takes one from the lower level (configurable `worker.fairness`, `0` = strict).
- Reserved slots: `--reserve high=2` keeps 2 of the `--concurrency` slots for `high` only, so a
  flood of `normal` work cannot block urgent work.
- Retries keep their priority. The sweeper republishes into the right stream.
- Isolation is still by queue: heavy CPU work goes to `queue="heavy"` served by its own
  workers (`--queue heavy --processes 8`), so it never takes slots from light work.

## Worker

- Consumes its queues with `XREADGROUP` (group `tenxgraph:<queue>:<priority>`, consumer
  `<host>:<pid>:<n>`).
- Concurrency: `--concurrency` async slots; `--threads` for `thread`; `--processes` for
  `process`. A full worker stops reading, so messages wait in the stream for another replica.
- Heartbeat renews leases for all runs it holds, in one batched call.
- Timeout: cancels the attempt like a cancel, then records a failure (retried if attempts are left).
- Cancel: a watcher reads `cancel_requested` for held runs every second.
  - `async`: `task.cancel()`; the run ends `cancelled`.
  - `thread`: a running thread cannot be stopped; `ctx.cancelled` turns true, the run stays
    `cancelling` until the function returns, then `cancelled`, result discarded.
  - `process`: SIGTERM to the child, SIGKILL after 5 s, child replaced.
- SIGTERM: stop reading, wait `--shutdown-timeout` for running attempts, then cancel the rest and
  CAS them back to `queued` without using up a retry (`interrupted` noted in `error`). Ties into
  the graceful-shutdown work (`v1-review/ISSUES.md` Group 1).
- Paused queue: the worker stops reading that queue; queued messages wait.
- Each attempt gets an OTEL span and logs carry `run_id` and `attempt`.

## Scheduler

- Runs in every `10xgraph worker` unless `--no-scheduler`. One replica is leader: a lock in
  the **run store** (Postgres and SQLite: a `tenxgraph_task_leader` row with holder and
  expiry, taken and renewed with one conditional update, as River does with its leader
  table; Redis store: `SET NX PX` plus a Lua renew). Renewed every `lease_ttl / 3`; released
  on clean shutdown so another replica takes over at once.
- Not on `BaseCacheStore`: a deployment with Kafka plus Postgres may have an in-memory cache
  store, which would make every pod a leader. Not `LeaseManager` either: it falls back to an
  in-process slot when the cache store errors (`core/leases.py`). The scheduler fails closed:
  no lock, no ticks. The deterministic cron id is the second guard.
- If no leader has renewed for `3 x lease_ttl` (for example a bug in leadership), every worker
  runs the sweeper itself (safe, every step is a CAS) and logs a warning, so stuck runs are
  still rescued. Cron ticks never use this fallback.
- On each tick the leader inserts the run (`cron:<task>:<tick>`, identity from the task's
  `identity`) and publishes it. It keeps the last fired tick per schedule for `catchup`.
- A paused schedule skips its ticks; resuming does not fire skipped ticks.
- The leader also runs the sweeper and the retention cleanup (delete runs past `expires_at`,
  in batches; Redis uses key TTLs).
- Cron parsing library: chosen in Sprint 0 (check maintenance, license and timezone/DST handling
  before adding it).

## HTTP API

| Route | Permission | Notes |
|---|---|---|
| `GET /v1/tasks` | `tasks:read` | HTTP tasks with a JSON schema of their kwargs (for the playground form). |
| `POST /v1/tasks` | `tasks:trigger` | `{"task", "kwargs", "key"?}`; only `http=True` tasks, others 404. 422 on bad kwargs. 202 with `{run_id, state}`. Runs as the caller. |
| `GET /v1/tasks/runs` | `tasks:read` | Filters `task`, `state`, cursor pagination. Own runs (tenant reach widens it after AUTH_PLAN). |
| `GET /v1/tasks/runs/{run_id}` | `tasks:read` | Record with progress, result, error. Not visible: 404. |
| `POST /v1/tasks/runs/{run_id}/cancel` | `tasks:cancel` | Queued: `cancelled` now. Running: `cancelling`. Terminal: 409. Own runs only. |
| `POST /v1/tasks/runs/{run_id}/retry` | `tasks:trigger` | New run with the same task and kwargs, as the caller. |
| `GET /v1/tasks/runs/{run_id}/events` | `tasks:read` | `?after=<seq>` cursor over the event log. Same visibility as the run. |
| `GET /v1/tasks/schedules` | `tasks:read` | Schedules, next fire time, paused flag. |
| `GET /v1/tasks/queues` | `tasks:pause` | Per queue and priority: depth, oldest waiting age, running count, paused flag. |
| `GET /v1/tasks/workers` | `tasks:pause` | Registry: worker id, pod, version, queues, slots used, last heartbeat, draining. |
| `POST /v1/tasks/schedules/{name}/pause` and `/resume` | `tasks:pause` | |
| `POST /v1/tasks/queues/{queue}/pause` and `/resume` | `tasks:pause` | |

Until AUTH_PLAN lands, routes use today's `RequirePermission("tasks", ...)`; owner checks compare
`owner_user_id` with the caller's `user_id`. Polling is enough for v1; a server-sent events stream
per run is a later item.

## Metrics

`GET /metrics` on the worker (Prometheus text, `--metrics-port`, off by default): queue depth
and oldest-queued age per queue and priority, running runs, claims, successes, failures,
retries, lease losses, `RetryAfter` deferrals, attempt duration histogram, leader status. The
API exposes the same queue gauges read from the run store.

## Postgres at scale

From the research: the limit is table bloat, not `SKIP LOCKED`. Keep claim transactions short
(one statement), keep large `kwargs`, `result` and events out of the hot row (events in their
own table), prune in batches, document autovacuum settings for the runs table, and keep
partitioning by day as an option if a deployment needs it.

## Webhooks and user routes

- New key `"routers": ["webhooks:router"]` mounts the developer's FastAPI routers.
- `assert_all_routes_protected` (`core/auth/route_guard.py`) still applies. A webhook route
  marks itself public with an explicit opt-in that the guard accepts; it verifies the provider
  signature itself and enqueues with a system identity. AUTH_PLAN Sprint 5 replaces the opt-in
  with `WebhookAuth(verify=...)`.
- Docs: verify before enqueueing, enqueue only (never work inline), return 2xx for ignored
  events, use `key=` with the provider's event id because providers retry.

## Playground and client

- Client SDK: `tasks.list()`, `tasks.trigger()`, `tasks.runs()`, `tasks.run(id)`,
  `tasks.cancel(id)`, `tasks.retry(id)`, `tasks.schedules()`, pause and resume.
- Playground "Tasks" view: task list with a form built from the schema, trigger, runs table
  with state, attempt and progress, run detail (kwargs, result, error, timeline), cancel and
  retry buttons, schedules with pause. Admin controls show when `/v1/me` grants `tasks:pause`
  (after AUTH_PLAN).

## Sprints

Each sprint ends with its suite green (run sequentially, memory limit), a short report and a
user test. Code goes through subagents: Opus for the worker, leases, cancel, shutdown, stores
and migrations, and for reviews; Sonnet for routes, CLI, tests, docs and the playground.

### Sprint 0: verify (read-only, no product code)

- Redis Streams commands in redis-py 6.4 (`xreadgroup`, `xautoclaim`, `xack`) and the minimum
  Redis server (`XAUTOCLAIM` is 6.2+); Lua CAS scripts through `register_script`.
- How core and the API create Postgres tables today (PgCheckpointer setup, schema versioning),
  so the run store follows the same pattern.
- How a graph run continues after a crash when invoked again with the same `thread_id`
  (graceful-shutdown crash recovery), to confirm what "resume" gives a graph task.
- Process executor: kill and replace one child without breaking others; spawn start method.
- Cron library choice.
- Exit: findings note; this plan is updated where reality differs.

### Sprint 1: core (in-memory)

- `@task`, registry, signature validation, `TaskContext`, `.enqueue()` and run handle.
- `BaseRunStore` and `BaseTransport` contracts with in-memory versions.
- State machine with CAS, async executor, retries and backoff, timeout, cancel, lease and
  heartbeat, fencing, SIGTERM handling, `ctx.emit` with buffered fenced flushes, concurrency
  caps in the claim, `key_mode`, `RetryAfter`, `max_lost`, hooks, per-task retention.
- Tests: property test of random event sequences against the state machine (no illegal
  transition, no double run, terminal is final).

### Sprint 2: Redis, Postgres and local

- Redis Streams transport (with priority streams), Redis run store (Lua), Postgres run store
  (table, indexes, setup), SQLite `local` store and transport.
- Sweeper (orphaned queued, expired leases, due retries and delayed runs) and retention.
- One contract test suite run against every store and transport.
- Fault injection on real Redis and Postgres: `kill -9` mid-run, `SIGSTOP` past the lease then
  `SIGCONT`, forced duplicate messages, duplicate `key=`, store errors mid-claim.

### Sprint 3: worker command

- `worker` config section and validator, `10xgraph worker`, queues, priority (fairness,
  reserved slots), thread and process executors, InjectQ inside tasks, `ctx.invoke_graph`
  (with graph progress forwarded), worker registry and draining, shutdown,
  `10xgraph tasks enqueue|runs|workers`.
- Replace the commented-out legacy stubs in `src/app/worker.py` and `src/app/tasks/`.
- User test: run the worker on the example project, enqueue from a script, watch retries,
  kill the worker mid-run and see the run continue on another one.

### Sprint 4: scheduler and pause

- Leader lock in the run store (row for Postgres and SQLite, key for Redis), cron ticks with
  deterministic ids, catch-up, pause and resume of schedules and queues, sweeper on the leader,
  `--clock` / `--clock-speed`, `10xgraph tasks fire`.
- Tests: two and three schedulers at once, leader killed mid-tick, Redis outage (no ticks, no
  duplicates after it returns), DST change.

### Sprint 5: HTTP, webhooks, client

- `/v1/tasks` routes, `routers` key, public opt-in, client SDK methods.
- Tests: owner-only reads and cancel (others 404), non-HTTP task 404, kwargs 422, idempotent
  trigger, cancel in every state.

### Sprint 6: playground, build, docs, live check

- Playground Tasks view.
- `10xgraph build` adds a `worker` service when the `worker` section exists.
- Docs: "background tasks" (core `BackgroundTaskManager`, in-process, best effort) versus
  "queued tasks" (this system, durable); idempotency; executors; webhooks.
- Live check with real Redis and Postgres, two workers, a cron task, a CPU task. Then CHANGELOG.

## Size estimate

| Part | Lines |
|---|---|
| `@task`, registry, enqueue, context | ~250 |
| Run store contract, Redis, Postgres, local, memory | ~900 |
| Redis Streams (with priority), local and memory transports | ~550 |
| Priority, fairness, reserved slots, fake clock, `tasks` CLI | ~250 |
| Worker and executors | ~800 |
| Scheduler and sweeper | ~300 |
| CLI and config | ~250 |
| Routes | ~300 |
| **Total** | **~3,550**, plus 3,500 to 4,500 lines of tests |

Later transports: about 300 to 400 lines each.

## Later

- RabbitMQ and Kafka transports, reusing the connection code in
  `agentflow/tenxgraph/runtime/publisher/` (send-only today; the consumer side is new).
- NATS and SQS, only if needed.
- Server-sent events per run, schedule edits over the API.
- Cluster-wide rate limits per task or key (tokens per window), throttle and debounce modes.
- Round-robin fairness across keys (v1 has per-key caps, which already stop one tenant
  taking every slot).
- Batches with completion callbacks and parent/child runs (columns already in v1).
- Versioned deploys: stop claiming runs whose `code_version` is not current, drain old pods.
- Runtime concurrency overrides per queue (next to pause), sharding a hot stream by key.

## Prior art (research 2026-11-06)

Official docs checked: BullMQ (groups, rate limiting, dedup, flows, stalled jobs, metrics),
Sidekiq (weighted queues, dead set), Oban (peers, pruner, queue control), River (leader table,
unique jobs), Hatchet (concurrency keys, slots), Temporal (task queues, heartbeats, worker
versioning), Celery (prefetch, `acks_late`), Graphile Worker (job keys, performance), Faktory
(batches, progress tracking), Inngest (concurrency keys, debounce), Procrastinate (queueing
locks), KEDA (Redis Streams and PostgreSQL scalers). Not one of them uses a permanent master;
all coordinate through the shared store, with a leader for cron and pruning.

## Notes for AUTH_PLAN

- `tasks:pause` acts on the whole deployment (a queue or schedule is shared by all tenants).
  It should need reach `all`, or pause must become per tenant. AUTH_PLAN gives it to
  `org_admin` with reach `tenant`, which does not match.
- Cancel and retry stay own-only, in line with A13.
- `.enqueue()` has no default identity (J9); the worker rebuilds the stored identity and
  recomputes permissions before each attempt (A12). A run whose identity lost `tasks:trigger`
  fails without running.

## Rules carried through every sprint

- Tasks are registered only in code; HTTP can trigger but never register.
- Delivery is at least once; the docs say so from Sprint 1.
- Chat streaming and WebSocket code stay untouched.

## Store and transport interfaces

Sprint 1 code (2026-11-06, fix round applied). The contracts live as docstrings in
`tenxgraph_api/jobs/store/base.py` (`BaseRunStore`) and `tenxgraph_api/jobs/transport/base.py`
(`BaseTransport`); records are pydantic models in `tenxgraph_api/jobs/models.py` (the whole
package uses pydantic, no dataclasses). Memory versions: `store/memory.py`,
`transport/memory.py`. A new backend implements the base class and adds one factory to
`tests/unit_tests/jobs/contract/conftest.py` (`STORE_FACTORIES`, `TRANSPORT_FACTORIES`); the
contract suite, including its `asyncio.gather` race checks, then runs against it. Core logic
(enqueue, worker, sweeper) never changes for a new backend.

`BaseRunStore`: every state change is one atomic call (one SQL statement or transaction, one
Lua script, one `BEGIN IMMEDIATE`).

| Method | What it does |
|---|---|
| `setup()`, `aclose()` | Create tables or scripts (idempotent); close. |
| `now()` | Store clock, epoch seconds. Every lease and `available_at` uses it. |
| `create_run(new, *, key_mode="keep")` | Insert `queued` (`available_at` = `at`, else now + `delay`; `next_publish_at = available_at`); idempotency per `(task, owner_user_id, tenant, key)`; `keep` returns the existing run, `replace` swaps kwargs while `queued`. Returns `created`, `replaced`, `due`. |
| `get_run(id)`, `list_runs(filters, *, limit, cursor)` | Read; list newest first with an opaque cursor. |
| `claim(id, *, worker_id, lease_ttl, concurrency_limit=None)` | CAS `queued/retrying` and due to `running`, `attempt + 1`, lease from the store clock, slot taken in `(task, key)`, `next_publish_at = None`. Over the cap: `CAPPED`, `available_at = next_publish_at` pushed 1 s doubling to 30 s. |
| `claim_next(queues, *, worker_id, lease_ttl, concurrency_limits=None)` | Optional (default raises `NotImplementedError`): pick and claim the best due run (priority, `available_at`, `run_id`) in one call, skipping full caps. For SQLite `local`'s single-statement claim. Memory implements it. |
| `renew_leases(worker_id, held, *, lease_ttl)` | Batched heartbeat and cancel watch: `{run_id: cancel_requested}` for leases still owned; a missing id is lost. |
| `append_events(id, *, attempt, worker_id, events, progress=None)` | Fenced flush: events get seq numbers, log capped at 500, snapshot replaced. |
| `transition(id, *, attempt, worker_id, outcome)` | Fenced end of an attempt: `succeeded`, `failed`, `retry`, `defer` (`RetryAfter`), `cancelled`, `requeue` (shutdown). Releases the slot in the same call. From `cancelling` always `cancelled`. Safe to repeat (CAS). |
| `request_cancel(id)` | `queued/retrying` to `cancelled`; `running` to `cancelling` + `cancel_requested`. |
| `read_events(id, *, after=0, limit=100)` | Event log after a cursor. |
| `find_expired_leases(*, limit)`, `mark_lost(id, *, attempt)` | Sweeper: CAS an expired lease back to `queued` (`lost_count + 1`, `next_publish_at = now`), to `failed` past `max_lost`, to `cancelled` when cancelling; slot released. |
| `due_runs(*, limit=100)` | Claimable runs with `next_publish_at <= now` (index `(state, next_publish_at)`). |
| `mark_published(seen, *, republish_after=300)` | CAS after a publish on `(attempt, next_publish_at)`. `seen` are the `TaskRun` records the caller read before publishing (a claim always raises `attempt`). Only a run still `queued`/`retrying` with the same `attempt` and `next_publish_at` gets `published_at = now`, `next_publish_at = now + republish_after`; a run claimed and set to retry, defer or requeue meanwhile keeps its newer time (without the CAS a 1 s retry waited 5 minutes). Each run waits its own interval, so an unclaimed backlog is never republished all at once. |
| `concurrency_count(task, key=None)`, `recount_concurrency()` | Read a counter; rebuild counters from leased runs. The recount must hold each counter's row lock (Postgres `FOR UPDATE` in one transaction), run inside the claim script (Redis) or be serialised with claims (SQLite, memory), so it never overwrites a concurrent increment. |
| `purge_expired(*, limit)` | Retention: delete finished runs past `expires_at` and their events; frees the key. |

Rules:
- Storable values: every store encodes JSON fields with `tenxgraph_api/jobs/serialization.py`
  and raises `ValueNotStorable(field, reason)` (`jobs/errors.py`) before any write for NaN or
  Infinity, a string holding `\x00` (at any depth, keys too), or a non-JSON value. Postgres
  cannot store the first two; memory, SQLite and Redis refuse them too, so the worker sees the
  same error on every backend and fails the run (`result_not_serializable`) instead of
  retrying a write that can never succeed.
- Cancellation: a cancelled store call either finished its write or wrote nothing. SQLite runs
  `BEGIN IMMEDIATE`, the body and `COMMIT`/`ROLLBACK` as one unit on its own task; a cancelled
  caller waits for its rollback.
- Postgres takes at most one counter row lock per transaction (after the run row) and retries
  a reported deadlock; statements time out after 30 s, pool acquires after 30 s
  (`pool_config`: `command_timeout`, `acquire_timeout`).
- Redis clients built by the jobs code never retry commands (Lua scripts are not idempotent);
  a supplied client that retries gets a WARNING.
- Fencing: `attempt` is the token. Every worker write matches `run_id`, `attempt`,
  `worker_id` and a leased state; a lease expired by the clock but not yet recovered can still
  be renewed.
- Tenant is never NULL: `TaskRun` and `NewRun` normalise None to `""`, so a plain SQL unique
  key on `(task, owner_user_id, tenant, idempotency_key)` works (no `NULLS NOT DISTINCT`).
- Redis stores keep every key under one hash tag, `REDIS_HASH_TAG = "{tenxgraph-jobs}"`
  (`redis_key(*parts)`), always, not only on Cluster: every Lua script touches one slot, at the
  cost of putting all jobs data on one shard by design. Counter keys may be derived inside a
  script from the run's `slot` field, since they share the tag.

`BaseTransport`: `setup()`, `publish(message)`, `publish_many(messages)`,
`consume(queues, *, max_messages, block=0.0)` (higher priority first, never blocks past
`block`; during or after `aclose` it returns `[]`), `ack(deliveries)`, `aclose()` (Redis:
cancels in-flight reads and waits for `consume` before XPENDING / DELCONSUMER, so no delivered
entry is dropped). Messages carry a format `version` (now 1). A message
with an unknown version or an unreadable body is acked, dropped and logged, never left unacked
(that would loop through `XAUTOCLAIM`); the run stays in the store and is published again when
its `next_publish_at` comes. Transports have no delay: delayed, retrying, capped and lost runs
wait in the store and the sweeper publishes them.

Worker behaviour (Sprint 1):
- `aclose(grace)` stops reading; `run()` keeps the heartbeat, cancel watch and flushes until
  every attempt has written its outcome, and stops its ticker last.
- Store and transport errors never end `run()`: a failing delivery stays unacked for
  redelivery, a failing `consume` backs off (0.1 s doubling to 5 s), a failing ack is ignored
  (the claim already decided), a failing final write is retried with backoff while the local
  lease deadline holds (a None after a write that raised is checked by reading the run back).
- Local lease deadline: `lease_ttl - lease_margin` (default `lease_ttl / 5`) after the last
  successful renewal on the worker's monotonic clock, the attempt is stopped and nothing more
  is written for it, so a worker cut off from the store stops before the sweeper can hand the
  run to someone else.
- A cancel, shutdown or lost lease that arrives before the body starts (during
  `on_task_start`) skips the body.
- `on_task_progress` hooks run on one worker task fed by a bounded queue (1000); the tick never
  awaits them, and events are dropped with a warning when the queue is full.
- A task this worker does not know (rolling deploy): the message is acked, the run stays
  `queued` and is published again after 30 s, so a pod that has the task runs it. It is not
  failed; operators cancel such runs if the task is gone for good.
- Shutdown during a timeout requeues (shutdown wins).

Open items: the step-end flush timing (step ends wait for the next flush, at most one a second)
and `RetryAfter` versus `timeout` (a deferral is not capped by the task timeout) are left as
they are for now.

## Executors (Sprint 3)

`tenxgraph_api/jobs/executors/` runs the body of `thread` and `process` tasks; `async` tasks
stay in the worker loop. The worker keeps claims, leases, flushes and transitions. Interface
(`base.py`):

- `Executor`: `capacity`, `free_slots` (abandoned threads count as busy), `await astart()`,
  `await start(ExecutionSpec) -> ExecutionHandle` (waits for a slot; `RuntimeError` after
  close), `await aclose(grace=30.0)`.
- `ExecutionSpec(task, stored_kwargs=run.kwargs, ctx, timeout=None, max_result_bytes)`.
  The executor loads the kwargs (in the child for `process`) and enforces the timeout
  (`timeout` or `task.timeout`).
- `ExecutionHandle`: `await result() -> ExecutionResult` (never raises), `request_cancel()`,
  `kill(reason)`, `done`.
- `ExecutionResult.kind`: `returned` (`value`; `value_is_json` true for `process`: already
  dumped and size-checked, store as is), `raised` (`exception`: `Fail` / `RetryAfter` keep their
  types, other child errors are `ChildTaskError`; `error` has the original type name),
  `invalid` (kwargs or result validation: fail now), `timeout`, `crashed` (`crashed`,
  `memory_limit`, `child_import`, `child_start`: retry policy applies), `stopped` (after
  `kill(reason)`; the worker decides from its own stop reason).

Worker integration: call `start` inside the attempt's asyncio task (contextvars such as the
task context and InjectQ's container context are copied into the thread); check `free_slots`
of the task's executor before claiming; map `_stop(attempt, "cancel")` to `request_cancel()` and
`lost` / `shutdown` / `abort` to `kill(reason)`; keep flushing `attempt.emitter` (thread tasks
write to it directly, process emit calls are replayed into it on the loop); call `aclose` on
shutdown after the worker's own grace.

- `thread`: `ThreadExecutor(threads)`. Timeout and kill set `ctx.cancelled` and resolve at once;
  the thread keeps its slot until it returns and its result is discarded. `Inject[...]`
  defaults are left out of the kwargs schema and resolve in the thread.
- `process`: `ProcessExecutor(processes, preload=[task modules], max_tasks_per_child=None,
  max_memory_per_child=None (MB), start_method=None, cancel_grace=5.0)`. Forkserver with
  `set_forkserver_preload` (spawn where missing, never fork); one child and pipe per slot; the
  child imports the task by `module:qualname`. Cancel sets a lock-free shared flag and kills
  after `cancel_grace`. Timeout, kill and memory over the limit mid-run kill the child; a new
  child starts on the next run. A child that cannot start fails runs with `child_start` and
  backs off 1 s doubling to 60 s. `process` tasks cannot take `Inject[...]` parameters
  (refused at `@task`). The forkserver preload list is process-wide: the first executor that
  starts the forkserver decides it.
