# Jobs on TaskIQ: Sprint 0 findings (2026-11-06)

Live checks on Redis 8 (port 16379) with taskiq 0.13.0, taskiq-redis 1.2.4, redis-py 8.1.0,
injectq 0.5.0, plus source reading. Key claims re-checked in the installed source.

## Works as planned

| Check | Result |
|---|---|
| Stream broker + result backend | `RedisStreamBroker(url).with_result_backend(RedisAsyncResultBackend(redis_url, result_ex_time=...))`; worker default ack `when_saved` (`--ack-type`), default `--workers 2` |
| Progress | `ProgressTracker` / `TaskProgress` from `taskiq.depends.progress_tracker`; `await task.get_progress()` works on Redis. TaskIQ never sets progress itself |
| Dynamic schedules | `kicker().with_schedule_id(id).with_labels(...).schedule_by_cron / _by_interval / _by_time(source, ...)` -> `CreatedSchedule` (`schedule_id`, `unschedule()`); all three fire; delete stops it |
| InjectQ | `from injectq.integrations.taskiq import setup_taskiq, InjectTaskiq`; `setup_taskiq(container, broker)` |
| Config kwarg | middleware: `self.broker.find_task(message.task_name).original_func`, `inspect.signature`; `config` passed only when declared; tasks without it run fine; sync tasks too |
| Injected parameter list | `taskiq_dependencies.DependencyGraph(fn)` or scanning `Annotated` metadata for `Dependency`; plain args = signature minus that set |
| Middleware order | `pre_execute` in registration order, before dependency resolution and argument parsing; post hooks in reverse |
| `TaskContext` | works as `Annotated[TaskContext, TaskiqDepends()]` with `__init__(self, context: Annotated[Context, TaskiqDepends()])` |

## Changes the plan needs

1. **Dead-worker redelivery only happens while new messages arrive.** `RedisStreamBroker.listen()`
   runs `XAUTOCLAIM` only after an `xreadgroup` that returned messages (`if not fetched:
   continue`, `taskiq_redis/redis_broker.py`). Live: a killed worker's task stayed pending 50 s on
   an idle queue, then ran the moment an unrelated task arrived. Fix: our `redis_stream` type is a
   small subclass whose `listen()` also runs the autoclaim when nothing was fetched. Also set
   `unacknowledged_lock_timeout` (default None: a worker killed while holding the autoclaim lock
   blocks it forever). Worth an upstream issue.
2. **`idle_timeout` must be longer than the longest task** (milliseconds, default 600000). A
   still-running task older than that is claimed by another worker and runs twice. There is no
   lease renewal. Redelivery restarts the task from the beginning (at least once).
3. **Caller kwargs override injected dependencies.** The receiver does
   `kwargs = await dep_ctx.resolve_kwargs(); kwargs.update(message.kwargs)` (`receiver.py`
   320-322). `kiq(1, ctx="x")` replaced `TaskContext`; `graph=` would replace the graph. The
   reserved-argument check is a security check: reject injected names, `config`, `user`,
   `user_id`, `authz`, unknown names and extra positional args, in the API and `enqueue`, and
   again in the worker middleware.
4. **Labels keep only int, str, float, bool.** A dict becomes its Python repr
   (`taskiq/labels.py::prepare_label`). The config label is `json.dumps(cfg)`, parsed in
   `pre_execute`.
5. **A middleware cannot skip a task.** `pre_execute` is called outside any `try`
   (`receiver.py` 189-195); a raise leaves the message unacked (redelivered forever on a stream)
   and the caller waiting. Cancel design (tested, async and sync): `pre_execute` sets a
   ContextVar for a cancelled id; a guard wrapped around each task function raises
   `TaskCancelled` before the body runs; the caller gets an error result; our status record says
   `cancelled`. Our middleware must never raise.
6. **Only `InjectTaskiq` injects.** A plain InjectQ `Inject[...]` default silently passes the
   marker object.
7. **`TaskContext` is a plain class.** A pydantic model fails: TaskIQ reads `__init__`, and
   pydantic's is `(**data)`.
8. **Progress and cancel need `async def`.** `ctx.progress()` / `ctx.cancelled()` are async.
9. **Schedules:**
   - No update API. Re-adding the same id leaves a duplicate in the type index, and a type change
     leaves the old entry. Change = delete + add (verified clean).
   - `get_schedules()` hides future one-time schedules, so listing uses our own owner index.
   - The scheduler reloads every `--update-interval` (default 60 s): changes take up to that long.
   - Do not use `with_task_id()` on schedules (every firing would reuse the id).
   - The schedule source's default serializer is pickle; use a JSON serializer (check in
     Sprint 1).
10. **Constructor parameter names differ per type**, so the factory keeps a per-type map:
    - `RedisStreamBroker(url, ..., idle_timeout ms, unacknowledged_lock_timeout s)`
    - `ListQueueBroker(url, ...)`
    - `RedisAsyncResultBackend(redis_url, result_ex_time, ...)`
    - `taskiq_aio_pika.AioPikaBroker(url, ...)`
    - `taskiq_nats.PullBasedJetStreamBroker(servers, ...)`; `NatsBroker` (core NATS) does not
      persist; the result backend is `NATSObjectStoreResultBackend(servers, ...)`
    - `taskiq_aio_kafka.AioKafkaBroker(bootstrap_servers, ...)`, no `**kwargs`
    - `InMemoryBroker()`, no URL, stores its own results
11. **Dependencies:** taskiq-redis 1.2.4 needs `redis>=8,<9` and `taskiq>=0.13`. `main` still
    has the `redis` extra capped at `<7` (`pyproject.toml` line 97): raise it in Sprint 1. The
    API venv has injectq 0.4.2; check the `setup_taskiq` signature there too.

Scripts and raw notes: scratchpad `s0-taskiq/redis_checks/` and `s0-taskiq/mw_checks/`.
