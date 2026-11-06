# Jobs Sprint 0: verification findings (2026-11-06)

No services were running (Docker down). Redis, RabbitMQ and Postgres results therefore come from
library source reading, fakeredis and local scripts. A live run against real services is still
pending and must confirm:

- `XREADGROUP BLOCK` against the client socket timeout (section 1).
- `xinfo_groups` lag on a real Redis 7+.
- RabbitMQ reconnect and redelivery (section 6).
- Postgres advisory-lock DDL with many concurrent starters (section 2).

Versions checked: redis 8.1.0, aio-pika 10.2.2 (aiormq 7.2.5), croniter 6.2.4, asyncpg 0.32.0,
SQLite 3.53.1; Python 3.12.13, 3.13.13, 3.15.0. Scratch scripts lived in the session scratchpad
and are not kept in the repo.

## 1. Redis Streams on redis-py 8.x

**Verdict:** usable, with two clients and explicit timeouts.

**Evidence**
- Defaults `socket_timeout=5`, `socket_connect_timeout=5` (`redis/_defaults.py:7-8`). Retry
  default is 10 attempts on ConnectionError/TimeoutError, applied by `Redis.__init__`
  (`asyncio/client.py:286`).
- No special handling for blocking commands: `xreadgroup` just appends BLOCK
  (`commands/core.py:8050`) and `read_response` uses `socket_timeout`
  (`asyncio/connection.py:1253-1280`). Only pubsub passes `math.inf`. So BLOCK >= socket_timeout
  races the client timeout, and a dropped connection can leave a delivered entry in the PEL
  until XAUTOCLAIM.
- `xautoclaim(justid=True)` drops the next cursor and deleted ids (`_parsers/helpers.py:767-771`).
- One XREADGROUP over several streams returns all non-empty streams in key order and COUNT is
  per stream, so it is not strict priority.
- A `/1` URL selects db 1. `RedisCluster(db=1)` raises "Argument 'db' must be 0 or None in
  cluster mode" (`asyncio/cluster.py:459-461`). Managed services that reject SELECT give
  `ResponseError`.
- Blocker: `agentflow-api/pyproject.toml:97` has `redis>=5.0.7,<7`, which prevents redis 8.
- No removed APIs found in tenxgraph_api's redis use (`core/redis_client.py:48-63`). The API
  suite has not yet been run on redis 8.

**Plan change**
- The worker uses two clients. Blocking-read client: `socket_timeout=None`,
  `socket_connect_timeout=5`, keepalive on, `health_check_interval=30`, its own reconnect and
  backoff loop, BLOCK 2000-5000 ms wrapped in `asyncio.timeout(block + 5 s)`, never BLOCK 0.
  Short-command client (XACK, lease renew, store): `socket_timeout=5`.
- Do not reuse the API's `create_redis` / `redis_options` for blocking reads.
- Use `xautoclaim(justid=False)`.
- Priority: read `high` alone first (COUNT = free slots), then a blocking read over the rest.
- Detect cluster or SELECT rejection and fall back to db 0 with the key prefix.
- Drop `<7` from the API redis extra (user decision: no pin). Sprint 1 change.

## 2. Postgres table setup

**Verdict:** reuse core's pattern; jobs get their own DSN and pool.

**Evidence**
- Driver is asyncpg (core extras `pg_checkpoint` / `pgvector` need `asyncpg>=0.29.0`).
- The server ignores the checkpointer key (tenxgraph_api `loader.py:806`) and no Postgres pool is
  in InjectQ.
- Core has `ddl_session` (`agentflow/tenxgraph/storage/columns.py:914`: advisory lock plus
  `lock_timeout`), and `PgCheckpointer` has `CURRENT_SCHEMA_VERSION` with ordered idempotent
  migrations (`pg_checkpointer.py:95`, `1005-1040`).

**Plan change**
- The jobs store takes its own DSN (`worker.store.url`, `${VAR}` expansion like `cache_store`)
  and its own small pool (min 1, max ~5; connection budget = pods x max).
- Reuse `ddl_session` with its own lock key, `CREATE TABLE IF NOT EXISTS` after a catalog check,
  a `tenxgraph_task_schema_version` table and ordered idempotent migrations.
- Run from store setup only when the `worker` section exists, and from
  `10xgraph tasks migrate [--check]`. Options `worker.store.schema` (default `public`) and
  `auto_migrate` (default true).
- Advisory session locks break behind PgBouncer transaction pooling: migrate needs a direct or
  session-mode DSN.
- SQL rules: claim by id with `UPDATE ... WHERE id=$1 AND status='queued' RETURNING` is safe in
  READ COMMITTED. `FOR UPDATE SKIP LOCKED` only for sweeper and promotion scans. The counter-row
  cap is atomic but leaks when a pod dies, so terminal transitions decrement in the same
  transaction and the sweeper recounts from running runs. Leader lock:
  `INSERT ... ON CONFLICT DO UPDATE ... WHERE expires_at < clock OR holder=me RETURNING`.
- Clock: use `clock_timestamp()`, not `now()` (`now()` is the transaction start time).

## 3. SQLite local backend

**Verdict:** works for `local`, with required pragmas.

**Evidence**
- SQLite 3.53.1 has RETURNING (3.35+).
- Two processes, 500 rows, WAL + `synchronous=NORMAL` + `BEGIN IMMEDIATE` +
  `UPDATE ... WHERE id=(SELECT ... LIMIT 1) RETURNING`: zero double claims. About 23k claims/s
  idle, about 875/s with 2 ms of work.
- `busy_timeout=0` gave "database is locked" errors (no double claims); `busy_timeout=5000`
  gave none.
- aiosqlite is not in the API venv. Core uses it via the `sqlite_checkpoint` extra
  (`sqlite_checkpointer.py:49, 276`; pyproject `aiosqlite>=0.19.0`).

**Plan change:** require WAL, `busy_timeout=5000`, `BEGIN IMMEDIATE`, retry on OperationalError;
check the SQLite version at startup; the `jobs` extra depends on aiosqlite; store clock is epoch
REAL.

## 4. Graph resume after a crash

**Verdict:** works with a fixed identity scheme; nodes are at-least-once.

**Evidence** (SqliteCheckpointer, N1 -> N2 -> N3, `os._exit` in N2)
- A retry with no new input (`{"messages": []}`) resumes at N2 and runs N2, N3 with correct
  context. The in-flight node runs again.
- Resending the user message duplicates it (core `utils.py:377-389`). With an expired marker, a
  new run_id and a resent message, recovery restarts the turn from START (N1 N2 N3) with a
  duplicate (`utils.py:345-358`).
- `is_dead_run` (`agentflow/tenxgraph/core/graph/utils/utils.py:155-168`) treats a state as its
  own when run_id matches the config run_id (line 166), and as alive while the 60 s active-run
  marker lives (`ACTIVE_RUN_TTL_SECONDS`, `storage/checkpointer/base_checkpointer.py:63`).
- An unreleased SqliteCheckpointer hung process exit.

**Plan change (worker rule)**
- Same `thread_id` on every attempt; `user_id` from the run identity.
- Graph `run_id` unique per attempt (`f"{run_id}:{attempt}"`) so core's repair runs.
- Input only on attempt 1; `{"messages": []}` on attempt > 1. No resume flag (`RESUME_KEY` is for
  `interrupt()` only).
- Close the checkpointer on shutdown.
- Open: a crash inside a ToolNode (recovery closes unanswered tool calls with a synthetic error,
  D7). Sprint 1 test.

## 5. Killing one process child

**Verdict:** `ProcessPoolExecutor` cannot do it; use supervised per-slot processes.

**Evidence**
- Killing one pool worker breaks the pool: BrokenProcessPool for the killed task, an unrelated
  in-flight task and every later submit, on 3.12, 3.13 and 3.15. 3.14+ `terminate_workers` /
  `kill_workers` kill all workers. `max_tasks_per_child` is incompatible with fork and silently
  switches to spawn.
- Working primitive: one supervised `multiprocessing.Process` per slot with its own Pipe,
  forkserver context with `set_forkserver_preload([user task modules])` (never fork: the worker
  has a loop and threads).
- `kill()` + `join` takes 1-9 ms; a 0.5 s timeout finished in 0.503 s. After kill, `recv` gives
  EOFError and `send` BrokenPipeError, so use a new pipe per child. Recycle after N tasks by
  sending None then join. Memory cap by polling `/proc/<pid>/statm` every 50 ms (Linux only,
  psutil elsewhere). New child cost: spawn about 23 ms, forkserver 2-6 ms.
- Asyncio side: `loop.add_reader(conn.fileno())` works on Linux; Windows Proactor fallback
  `asyncio.to_thread(conn.poll, t)`.
- Picklability: a lambda or closure fails at send in the parent (3.12/3.13 AttributeError
  "Can't get local object", 3.15 PicklingError) and the slot survives.

**Plan change**
- Reject `executor="process"` at `@task` registration unless the function is module-level and
  importable. Args and results must be picklable.
- `ctx.emit` needs the pipe (emit messages); cancel needs a per-slot `mp.Event`.
- A thread task abandoned on timeout keeps its slot until it returns (it counts toward
  concurrency caps) and its result is discarded.

## 6. RabbitMQ (aio-pika 10.2.2)

**Verdict:** fits; requires Python >= 3.11.

**Evidence**
- Core publisher (`agentflow/tenxgraph/runtime/publisher/rabbitmq_publisher.py:108-115`) uses
  `connect_robust` and `channel()`. Its Message has no `delivery_mode`, so messages are
  NOT_PERSISTENT.
- Confirmed consumer APIs: `channel(publisher_confirms=True)` (the default), `set_qos(prefetch_count)`,
  `declare_queue(durable, arguments x-dead-letter-exchange, x-max-priority)`,
  `queue.consume` / `iterator` with `no_ack=False`, `ack` / `nack(requeue)` / `reject`,
  `IncomingMessage.message_id` / `headers` / `redelivered`.
- Robust queues re-consume after reconnect and re-apply QoS. A message received before a
  reconnect cannot be acked (ChannelInvalidStateError) and is redelivered with
  `redelivered=True`, so dedup must not use delivery tags.

**Plan change:** job and command messages we publish set `DeliveryMode.PERSISTENT` and
`message_id`.

## 7. Cron (croniter 6.2.4)

**Verdict:** croniter. It is now github.com/pallets-eco/croniter (community-maintained),
classifiers to 3.13, depends on python-dateutil.

**Evidence** (America/New_York)
- 02:30 on spring-forward day fires at 03:00 EDT.
- 02:30 on fall-back day fires once.
- 01:30 on fall-back day fires TWICE (01:30 EDT and 01:30 EST); cronsim 2.8 fires once.
- `croniter_range` includes both ends (`exclude_ends=True` drops them). Catch-up via a `get_next`
  loop works with tz-aware datetimes. The seconds field is supported.
- `is_valid` accepts `"0 0 31 2 *"` but `get_next` raises CroniterBadDateError.

**Plan change:** validate at registration by calling `get_next` once. Open decision for the
user: on the fall-back day, should a 01:30 job fire once (tick id from local wall time,
recommended) or twice (croniter default).

## Open items

- DST fall-back decision (once or twice).
- ToolNode crash test in Sprint 1.
- Run the API suite on redis 8 after dropping the `<7` pin.

## Live run (2026-11-06)

Real containers: redis 8.10.2 and 7.4.11, a single-node Redis cluster (8), postgres 17, rabbitmq 4.
Client libs: redis-py 8.1.0, aio-pika 10.2.2, asyncpg 0.32.0. All containers removed afterwards.

**1. XREADGROUP BLOCK vs socket timeout (Redis 7 and 8 identical)**
- (a) `socket_timeout=5`, BLOCK 5000, empty stream, 5 runs: `TimeoutError` every time at 5.00-5.01 s
  (it never returns `[]`). Confirms the race and why BLOCK must be below the socket timeout.
- (b) BLOCK 0: `TimeoutError: Timeout reading from localhost:<port>` at 5.01 s.
- (c) BLOCK 4500 with XADD at 4.9 s: the read returns `[]` at 4.5 s, the entry arrives after, so
  there is no race (20 iterations, 0 lost). The real race is BLOCK == socket timeout: BLOCK 5000,
  XADD jittered 4.97-5.03 s, 20 iterations: 11 `TimeoutError`, 9 returned the entry, and 2 times
  the entry sat in the PEL and the reader never saw it. Confirms the loss.
- (d) `socket_timeout=None`, BLOCK 3000 in `asyncio.timeout(8)`: returns `[]` in 3.02-3.04 s. With
  `docker pause` for 10 s during a read: `asyncio.timeout` fired at 8.01 s; after unpause the
  next read returned `[]` after 6.16 s (same client object, no manual reconnect needed here),
  then 3.01 s reads. Note: a paused container keeps the TCP connection open, so only
  `asyncio.timeout` detects it; keepalive does not help in that case.

**2. xinfo_groups (7.4.11 and 8.10.2 identical).** XADD 5, read 2, ack 1:
`{'name': 'g', 'consumers': 1, 'pending': 1, 'last-delivered-id': ..., 'entries-read': 2, 'lag': 3}`.
`lag` and `entries-read` are present on both. `xautoclaim(justid=False)` after XDEL of a pending
entry returns 3 elements: `['0-0', [], ['<deleted id>']]`. The deleted id is removed from the PEL
by that call, so a later `justid=True` call returned `[]` (the justid=True shape was not tested on
a pending deleted entry).

**3. Multi-stream XREADGROUP** {hi, normal, low}, COUNT 1, 3 entries each: all three returned, in
the order of the keys passed (hi, normal, low; reordering the keys reordered the result).

**4. SELECT.** `from_url(.../1)` write lands in db 1; a db 0 client sees None. `RedisCluster(db=1)`
raises `RedisClusterException: Argument 'db' must be 0 or None in cluster mode`. `RedisCluster`
db=0 works (needs `address_remap` from a host when the node announces 127.0.0.1:7000; a test
artifact of the container port mapping).

**5. Postgres (ddl_session imported from tenxgraph.storage.columns with the API venv).**
50 concurrent starters: 0.24 s, 0 errors, exactly 1 created, 49 skipped, 1 version row, 3 indexes.
In one transaction after a 0.5 s sleep `now()` did not advance and `clock_timestamp()` advanced
0.50 s. Claim by id with 20 claimers: 1 winner. Counter cap `n < 3` with 20: 3 winners. Leader
upsert with 20 candidates: 1 leader; renew by the holder works; after forced expiry 1 new leader.

**6. RabbitMQ.** `connect_robust`, durable queue with `x-dead-letter-exchange` and
`x-max-priority 3`, PERSISTENT message with `message_id`. After `rabbitmqctl close_all_connections`
the robust consumer re-consumed on its own within 0.1 s; the message came back with
`redelivered=True` and the same `message_id`. The old message's `ack()` raised
`aiormq.exceptions.ChannelInvalidStateError`. `reject(requeue=False)` landed in the DLQ
(`x-first-death-reason=rejected`, same message_id). Priority: published low then high, prefetch 1,
high delivered first (for `get` and `consume`; with a backlog [first, low, high] the order was
high, first, low).
