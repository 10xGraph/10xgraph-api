"""Postgres run store (asyncpg).

Tables, all ``tenxgraph_task_*`` in one schema (``schema``, default ``public``):

``runs``
    One row per run; every ``TaskRun`` field is a column, plus ``event_seq`` (last event
    number) and ``prio`` (generated from ``priority`` for the claim order).
``events``
    The event log, primary key ``(run_id, seq)``, at most ``EVENT_LOG_CAP`` rows per run.
``counters``
    Concurrency slots in use, one row per ``slot_name(task, key)``.
``leader``, ``workers``, ``schedules``, ``rejected_events``
    Created empty for later sprints (leader lock, worker registry, cron, rejected event
    payloads), so those sprints only add columns through migrations.
``schema_version``
    One row per applied schema version.

Times are ``double precision`` epoch seconds (the models use float epoch seconds), taken from
``clock_timestamp()`` (never ``now()``, which is the transaction start). JSON fields are
``jsonb``, sent and read as text (``$n::text::jsonb`` / ``col::text``) so a caller's pool with
its own jsonb codec cannot change them. ``jsonb`` cannot hold ``\\u0000`` or NaN, so the shared
encoder (``jobs/serialization.py``) refuses both with ``ValueNotStorable`` before any write.

Every method is one statement or one transaction. Fencing: every worker write matches
``run_id``, ``attempt``, ``worker_id`` and a leased state. A capped claim locks the run row,
then takes the counter slot (``running < cap``) in the same transaction; every exit from a
leased state gives it back in the same transaction. Lock order is always run row, then at
most ONE counter row per transaction (``claim_next`` ends its transaction when a counter race
locks a full counter, and picks again in a new one); ``recount_concurrency`` locks counter rows
only, in slot order, then counts. A deadlock (40P01) reported anyway is retried a few times.
Statements time out after ``command_timeout`` (30 s) and a pool connection is awaited at most
``acquire_timeout`` (30 s); both are set through ``pool_config``.

``setup()`` creates what is missing under core's ``ddl_session`` (session advisory lock
``DDL_LOCK_KEY`` plus ``lock_timeout``): a catalog check, ``CREATE TABLE IF NOT EXISTS`` for a
missing table, then the ordered idempotent migrations above the recorded version. A restart at
the current version runs no DDL and takes no lock. ``migrate(dsn, schema, check=...)`` does the
same for ``10xgraph tasks migrate [--check]``. Advisory session locks need a direct or
session-mode connection (not PgBouncer transaction pooling).

``idempotency_key`` is unique per ``(task, coalesce(owner_user_id, ''), tenant, key)``: an owner
of None and of ``""`` share keys (``enqueue`` always sets an owner).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from tenxgraph.storage.columns import ddl_session

from tenxgraph_api.jobs import serialization
from tenxgraph_api.jobs.errors import JobsError
from tenxgraph_api.jobs.models import (
    CAP_WAIT_BASE,
    CAP_WAIT_MAX,
    CLAIMABLE_STATES,
    EVENT_LOG_CAP,
    REPUBLISH_AFTER,
    TERMINAL_STATES,
    AttemptOutcome,
    CancelResult,
    CancelStatus,
    ClaimResult,
    ClaimStatus,
    CreateResult,
    EventIn,
    HeldLease,
    KeyMode,
    LostResult,
    NewRun,
    RunFilter,
    RunPage,
    RunState,
    TaskEvent,
    TaskRun,
)

from .base import BaseRunStore, published_marks, slot_name


logger = logging.getLogger("tenxgraph_api.jobs.store.postgres")

#: Bump with a new entry in ``_MIGRATIONS`` (and the table DDL) when the schema changes.
CURRENT_SCHEMA_VERSION = 1
#: Session advisory lock key for jobs setup DDL ("TXGTASK1"); not shared with the checkpointer.
DDL_LOCK_KEY = 0x5458475441534B31
#: Seconds a setup DDL statement waits for a table lock.
DDL_LOCK_TIMEOUT = 10.0
TABLE_PREFIX = "tenxgraph_task_"
MAX_PAGE = 1000
#: Default seconds one statement may run (asyncpg ``command_timeout``; ``pool_config`` wins).
DEFAULT_COMMAND_TIMEOUT = 30.0
#: Default seconds to wait for a pool connection (``pool_config["acquire_timeout"]`` wins).
DEFAULT_ACQUIRE_TIMEOUT = 30.0
#: Runs ``claim_next`` may pick (and skip) before it reports nothing claimable.
CLAIM_NEXT_PICKS = 10
#: Times a claim is retried after Postgres reports a deadlock (SQLSTATE 40P01).
DEADLOCK_RETRIES = 3
DEADLOCK_BACKOFF = 0.01

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_NOW = "extract(epoch FROM clock_timestamp())::double precision"
_CLAIMABLE = "('queued', 'retrying')"
_LEASED = "('running', 'cancelling')"
_TERMINAL = "('succeeded', 'failed', 'cancelled')"

RUN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "task",
    "queue",
    "state",
    "priority",
    "attempt",
    "max_retries",
    "failure_count",
    "lost_count",
    "max_lost",
    "defer_count",
    "cap_waits",
    "kwargs",
    "identity",
    "owner_user_id",
    "tenant",
    "idempotency_key",
    "concurrency_key",
    "slot",
    "trigger",
    "worker_id",
    "lease_expires_at",
    "cancel_requested",
    "progress",
    "result",
    "error",
    "parent_run_id",
    "batch_id",
    "code_version",
    "retention",
    "available_at",
    "enqueued_at",
    "published_at",
    "next_publish_at",
    "started_at",
    "finished_at",
    "updated_at",
    "expires_at",
)
JSON_COLUMNS = frozenset({"kwargs", "identity", "trigger", "progress", "result", "error"})

# Table DDL at the current schema version. ``{name}`` tokens become qualified table names.
_TABLES: tuple[tuple[str, str], ...] = (
    (
        "schema_version",
        """CREATE TABLE IF NOT EXISTS {schema_version} (
            version integer PRIMARY KEY,
            applied_at double precision NOT NULL
        )""",
    ),
    (
        "runs",
        """CREATE TABLE IF NOT EXISTS {runs} (
            run_id text PRIMARY KEY,
            task text NOT NULL,
            queue text NOT NULL,
            state text NOT NULL DEFAULT 'queued' CHECK (state IN (
                'queued', 'running', 'retrying', 'cancelling',
                'succeeded', 'failed', 'cancelled')),
            priority text NOT NULL DEFAULT 'normal'
                CHECK (priority IN ('high', 'normal', 'low')),
            prio smallint GENERATED ALWAYS AS (
                CASE priority WHEN 'high' THEN 0 WHEN 'low' THEN 2 ELSE 1 END) STORED,
            attempt integer NOT NULL DEFAULT 0,
            max_retries integer NOT NULL DEFAULT 0,
            failure_count integer NOT NULL DEFAULT 0,
            lost_count integer NOT NULL DEFAULT 0,
            max_lost integer NOT NULL DEFAULT 2,
            defer_count integer NOT NULL DEFAULT 0,
            cap_waits integer NOT NULL DEFAULT 0,
            kwargs jsonb NOT NULL,
            identity jsonb NOT NULL,
            owner_user_id text,
            tenant text NOT NULL DEFAULT '',
            idempotency_key text,
            concurrency_key text,
            slot text,
            trigger jsonb NOT NULL,
            worker_id text,
            lease_expires_at double precision,
            cancel_requested boolean NOT NULL DEFAULT false,
            progress jsonb NOT NULL DEFAULT '{}'::jsonb,
            result jsonb,
            error jsonb,
            parent_run_id text,
            batch_id text,
            code_version text,
            retention double precision,
            available_at double precision NOT NULL,
            enqueued_at double precision NOT NULL,
            published_at double precision,
            next_publish_at double precision,
            started_at double precision,
            finished_at double precision,
            updated_at double precision NOT NULL,
            expires_at double precision,
            event_seq bigint NOT NULL DEFAULT 0
        )""",
    ),
    (
        "events",
        """CREATE TABLE IF NOT EXISTS {events} (
            run_id text NOT NULL,
            seq bigint NOT NULL,
            attempt integer NOT NULL,
            type text NOT NULL,
            at double precision NOT NULL,
            data jsonb NOT NULL,
            PRIMARY KEY (run_id, seq)
        )""",
    ),
    (
        "counters",
        """CREATE TABLE IF NOT EXISTS {counters} (
            slot text PRIMARY KEY,
            running integer NOT NULL DEFAULT 0 CHECK (running >= 0)
        )""",
    ),
    (
        "leader",
        """CREATE TABLE IF NOT EXISTS {leader} (
            name text PRIMARY KEY,
            holder text NOT NULL,
            expires_at double precision NOT NULL,
            updated_at double precision NOT NULL
        )""",
    ),
    (
        "workers",
        """CREATE TABLE IF NOT EXISTS {workers} (
            worker_id text PRIMARY KEY,
            state text NOT NULL DEFAULT 'running',
            info jsonb NOT NULL DEFAULT '{}'::jsonb,
            started_at double precision NOT NULL,
            heartbeat_at double precision NOT NULL,
            expires_at double precision NOT NULL
        )""",
    ),
    (
        "schedules",
        """CREATE TABLE IF NOT EXISTS {schedules} (
            name text PRIMARY KEY,
            task text NOT NULL,
            spec jsonb NOT NULL DEFAULT '{}'::jsonb,
            paused boolean NOT NULL DEFAULT false,
            next_at double precision,
            last_tick text,
            updated_at double precision NOT NULL
        )""",
    ),
    (
        "rejected_events",
        """CREATE TABLE IF NOT EXISTS {rejected_events} (
            id text PRIMARY KEY,
            source text NOT NULL,
            reason text NOT NULL,
            payload jsonb,
            received_at double precision NOT NULL
        )""",
    ),
)

# Ordered, idempotent steps per version. Tables are created at the current shape above, so a
# later version adds columns with ``ADD COLUMN IF NOT EXISTS`` and indexes with ``IF NOT
# EXISTS`` here (and in the table DDL).
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "CREATE UNIQUE INDEX IF NOT EXISTS tenxgraph_task_runs_key_uq ON {runs} "
        "(task, (coalesce(owner_user_id, '')), tenant, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS tenxgraph_task_runs_publish_ix ON {runs} "
        "(state, next_publish_at) WHERE next_publish_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS tenxgraph_task_runs_lease_ix ON {runs} "
        "(state, lease_expires_at) WHERE lease_expires_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS tenxgraph_task_runs_expires_ix ON {runs} "
        "(expires_at) WHERE expires_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS tenxgraph_task_runs_claim_ix ON {runs} "
        "(queue, prio, available_at, run_id) WHERE state IN ('queued', 'retrying')",
        "CREATE INDEX IF NOT EXISTS tenxgraph_task_runs_slot_ix ON {runs} "
        "(slot) WHERE slot IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS tenxgraph_task_runs_list_ix ON {runs} "
        "(enqueued_at DESC, run_id DESC)",
        "CREATE INDEX IF NOT EXISTS tenxgraph_task_runs_owner_ix ON {runs} "
        "(owner_user_id, enqueued_at DESC, run_id DESC)",
        "CREATE INDEX IF NOT EXISTS tenxgraph_task_runs_task_ix ON {runs} "
        "(task, enqueued_at DESC, run_id DESC)",
        "CREATE INDEX IF NOT EXISTS tenxgraph_task_workers_expires_ix ON {workers} (expires_at)",
        "CREATE INDEX IF NOT EXISTS tenxgraph_task_rejected_events_received_ix "
        "ON {rejected_events} (received_at)",
    ),
}

_TRANSITION_FIELDS = (
    "state",
    "slot",
    "worker_id",
    "lease_expires_at",
    "result",
    "error",
    "failure_count",
    "defer_count",
    "available_at",
    "next_publish_at",
    "finished_at",
    "expires_at",
    "updated_at",
)
_LOST_FIELDS = (
    "state",
    "slot",
    "worker_id",
    "lease_expires_at",
    "lost_count",
    "error",
    "available_at",
    "next_publish_at",
    "finished_at",
    "expires_at",
    "updated_at",
)
_CANCEL_FIELDS = (
    "state",
    "cancel_requested",
    "next_publish_at",
    "finished_at",
    "expires_at",
    "updated_at",
)
_CAPPED_FIELDS = ("available_at", "cap_waits", "next_publish_at", "updated_at")


class JobsSchemaError(JobsError, RuntimeError):
    """The jobs tables are missing or older than this code and ``auto_migrate`` is off."""


def check_schema_name(schema: str) -> str:
    """A plain identifier (letters, digits, ``_``), else ``ValueError``."""
    if not isinstance(schema, str) or not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid Postgres schema name for the jobs store: {schema!r}")
    return schema


def table_names(schema: str) -> dict[str, str]:
    """Qualified, quoted name of every jobs table in ``schema``."""
    check_schema_name(schema)
    return {name: f'"{schema}"."{TABLE_PREFIX}{name}"' for name, _ in _TABLES}


def _render(sql: str, tables: Mapping[str, str]) -> str:
    for name, qualified in tables.items():
        sql = sql.replace("{" + name + "}", qualified)
    return sql


def _cols(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(
        f"{prefix}{c}::text AS {c}" if c in JSON_COLUMNS else f"{prefix}{c}" for c in RUN_COLUMNS
    )


def _dumps(value: Any, field: str) -> str:
    return serialization.dumps(value, field=field)


def _param(name: str, value: Any) -> Any:
    if name in JSON_COLUMNS:
        return None if value is None and name in ("result", "error") else _dumps(value, name)
    if name == "state":
        return RunState(value).value
    return value


def _run(row: Any) -> TaskRun:
    data = {c: row[c] for c in RUN_COLUMNS}
    for name in JSON_COLUMNS:
        raw = data[name]
        data[name] = None if raw is None else json.loads(raw)
    return TaskRun.model_validate(data)


def _is_deadlock(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "40P01"


async def _now(conn: Any) -> float:
    return float(await conn.fetchval(f"SELECT {_NOW}"))


async def recorded_version(conn: Any, schema: str) -> int:
    """Highest applied jobs schema version in ``schema``; 0 when there is none. No DDL."""
    table = table_names(schema)["schema_version"]
    if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
        return 0
    return int(await conn.fetchval(f"SELECT coalesce(max(version), 0) FROM {table}"))  # noqa: S608


async def ensure_schema(conn: Any, schema: str, *, timeout: float | None = DDL_LOCK_TIMEOUT) -> int:
    """Create missing tables and apply pending migrations; returns the DDL statements run.

    At the current version this reads the catalog only (no lock, no DDL). Otherwise it runs
    under ``ddl_session`` and re-checks, so many processes starting at once run the DDL once.
    """
    if await recorded_version(conn, schema) >= CURRENT_SCHEMA_VERSION:
        return 0
    tables = table_names(schema)
    ran = 0
    async with (
        ddl_session(conn, lock_key=DDL_LOCK_KEY, timeout=timeout, owner="PostgresRunStore"),
        conn.transaction(),
    ):
        has_schema = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = $1)", schema
        )
        if not has_schema:
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            ran += 1
        for name, ddl in _TABLES:
            if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", tables[name]):
                continue
            await conn.execute(_render(ddl, tables))
            ran += 1
        current = await recorded_version(conn, schema)
        for version in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS.get(version, ()):
                await conn.execute(_render(statement, tables))
                ran += 1
            await conn.execute(
                f"INSERT INTO {tables['schema_version']} (version, applied_at) "  # noqa: S608
                f"VALUES ($1, {_NOW}) ON CONFLICT (version) DO NOTHING",
                version,
            )
        if current < CURRENT_SCHEMA_VERSION:
            logger.info(
                "jobs store schema %s: version %d -> %d",
                schema,
                current,
                CURRENT_SCHEMA_VERSION,
            )
    return ran


async def migrate(dsn: str, schema: str = "public", check: bool = False) -> int:
    """Bring the jobs tables in ``schema`` to ``CURRENT_SCHEMA_VERSION``; return the version.

    ``check=True`` runs no DDL and returns the recorded version (0 when nothing exists), so
    ``10xgraph tasks migrate --check`` can compare it with ``CURRENT_SCHEMA_VERSION``. Needs a
    direct or session-mode DSN (advisory session lock).
    """
    import asyncpg  # - optional dependency

    check_schema_name(schema)
    conn = await asyncpg.connect(dsn)
    try:
        if not check:
            await ensure_schema(conn, schema)
        return await recorded_version(conn, schema)
    finally:
        await conn.close()


def _encode_cursor(enqueued_at: float, run_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([enqueued_at, run_id]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        enqueued_at, run_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return float(enqueued_at), str(run_id)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc


# ---------------------------------------------------------------------- state changes (pure)


def _finish(run: TaskRun, state: RunState, now: float) -> None:
    run.state = state
    run.next_publish_at = None
    run.finished_at = now
    run.expires_at = None if run.retention is None else now + run.retention


def _end_lease(run: TaskRun, now: float) -> None:
    run.slot = None
    run.worker_id = None
    run.lease_expires_at = None
    run.updated_at = now


def _apply_outcome(run: TaskRun, outcome: AttemptOutcome, now: float) -> None:
    kind = "cancelled" if run.state == RunState.CANCELLING else outcome.kind
    _end_lease(run, now)
    if kind == "succeeded":
        run.result = outcome.result
        run.error = None
        _finish(run, RunState.SUCCEEDED, now)
    elif kind == "failed":
        run.error = outcome.error
        run.failure_count += 1
        _finish(run, RunState.FAILED, now)
    elif kind == "cancelled":
        run.error = outcome.error if outcome.kind == "cancelled" else None
        _finish(run, RunState.CANCELLED, now)
    elif kind == "retry":
        run.error = outcome.error
        run.failure_count += 1
        run.state = RunState.RETRYING
        run.available_at = now + max(0.0, outcome.delay)
        run.next_publish_at = run.available_at
    elif kind == "defer":
        run.defer_count += 1
        run.state = RunState.QUEUED
        run.available_at = now + max(0.0, outcome.delay)
        run.next_publish_at = run.available_at
    else:  # requeue
        run.error = outcome.error
        run.state = RunState.QUEUED
        run.available_at = now
        run.next_publish_at = now


def _apply_lost(run: TaskRun, now: float) -> None:
    cancelling = run.state == RunState.CANCELLING
    _end_lease(run, now)
    run.lost_count += 1
    if cancelling:
        _finish(run, RunState.CANCELLED, now)
    elif run.lost_count > run.max_lost:
        run.error = {
            "code": "lost",
            "type": "LeaseLost",
            "message": f"worker lost the run {run.lost_count} times (max_lost={run.max_lost})",
        }
        _finish(run, RunState.FAILED, now)
    else:
        run.state = RunState.QUEUED
        run.available_at = now
        run.next_publish_at = now


# ---------------------------------------------------------------------- the store


class PostgresRunStore(BaseRunStore):
    """Run store in Postgres. See the module docstring for the tables and rules.

    Pass ``dsn`` (the store creates and owns a small pool: ``min_size=1``, ``max_size=5``,
    ``command_timeout=30`` s per statement, overridden by ``pool_config``) or an existing
    asyncpg ``pool`` (never closed by the store; its own ``command_timeout`` applies). Every
    call waits at most ``acquire_timeout`` seconds for a pool connection (default 30 s; set
    ``pool_config["acquire_timeout"]``, None waits forever), so a store call never hangs on an
    exhausted pool or a stuck server. ``auto_migrate=False`` makes ``setup()`` refuse to run against tables older than
    this code instead of migrating them.
    """

    def __init__(
        self,
        dsn: str | None = None,
        pool: Any = None,
        schema: str = "public",
        pool_config: Mapping[str, Any] | None = None,
        auto_migrate: bool = True,
    ) -> None:
        if (dsn is None) == (pool is None):
            raise ValueError("PostgresRunStore needs exactly one of dsn or pool")
        self._schema = check_schema_name(schema)
        self._dsn = dsn
        self._pool: Any = pool
        self._owns_pool = False
        config: dict[str, Any] = {
            "min_size": 1,
            "max_size": 5,
            "command_timeout": DEFAULT_COMMAND_TIMEOUT,
            **(pool_config or {}),
        }
        self.acquire_timeout: float | None = config.pop("acquire_timeout", DEFAULT_ACQUIRE_TIMEOUT)
        self._pool_config = config
        self._auto_migrate = auto_migrate
        self._ready = False
        self._closed = False
        self._lock = asyncio.Lock()
        #: DDL statements the last ``setup()`` ran (0 when the schema was current).
        self.ddl_statements = 0
        t = self._t = table_names(schema)
        runs = t["runs"]
        cols = _cols()
        self._sql_get = f"SELECT {cols} FROM {runs} WHERE run_id = $1"  # noqa: S608
        self._sql_get_for_update = self._sql_get + " FOR UPDATE"
        self._sql_insert = (
            f"INSERT INTO {runs} (run_id, task, queue, priority, kwargs, identity, "  # noqa: S608
            "owner_user_id, tenant, idempotency_key, concurrency_key, max_retries, max_lost, "
            "retention, trigger, parent_run_id, code_version, available_at, next_publish_at, "
            "enqueued_at, updated_at) VALUES ($1, $2, $3, $4, $5::text::jsonb, $6::text::jsonb, "
            "$7, $8, $9, $10, $11, $12, $13, $14::text::jsonb, $15, $16, $17, $17, $18, $18) "
            "ON CONFLICT (task, (coalesce(owner_user_id, '')), tenant, idempotency_key) "
            f"WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING {cols}"
        )
        key_match = (
            "task = $1 AND coalesce(owner_user_id, '') = coalesce($2::text, '') "
            "AND tenant = $3 AND idempotency_key = $4"
        )
        self._sql_by_key = f"SELECT {cols} FROM {runs} WHERE {key_match}"  # noqa: S608
        self._sql_replace = (
            f"UPDATE {runs} SET kwargs = $5::text::jsonb, concurrency_key = $6, "  # noqa: S608
            f"updated_at = $7 WHERE {key_match} AND state = 'queued' RETURNING {cols}"
        )
        claim_set = (
            "state = 'running', attempt = attempt + 1, worker_id = $2, "
            "lease_expires_at = {ts} + $3, started_at = {ts}, cancel_requested = false, "
            "error = NULL, progress = '{}'::jsonb, cap_waits = 0, slot = $4, "
            "next_publish_at = NULL, updated_at = {ts}"
        )
        self._sql_claim_now = (
            f"WITH c AS MATERIALIZED (SELECT {_NOW} AS ts) UPDATE {runs} "  # noqa: S608
            f"SET {claim_set.replace('{ts}', 'c.ts')} FROM c WHERE run_id = $1 "
            f"AND state IN {_CLAIMABLE} AND available_at <= c.ts RETURNING {cols}"
        )
        self._sql_claim_at = (
            f"UPDATE {runs} SET {claim_set.replace('{ts}', '$5')} WHERE run_id = $1 "  # noqa: S608
            f"AND state IN {_CLAIMABLE} AND available_at <= $5 RETURNING {cols}"
        )
        self._sql_take_slot = (
            f"INSERT INTO {t['counters']} AS k (slot, running) VALUES ($1, 1) "  # noqa: S608
            "ON CONFLICT (slot) DO UPDATE SET running = k.running + 1 "
            "WHERE k.running < $2 RETURNING k.running"
        )
        self._sql_release_slot = (
            f"UPDATE {t['counters']} SET running = greatest(running - 1, 0) "  # noqa: S608
            "WHERE slot = $1"
        )
        self._sql_pick = (
            f"SELECT r.run_id, r.task, r.concurrency_key FROM {runs} r "  # noqa: S608
            f"WHERE r.queue = ANY($1::text[]) AND r.state IN {_CLAIMABLE} "
            "AND r.available_at <= $2 AND NOT (r.run_id = ANY($5::text[])) "
            "AND NOT EXISTS (SELECT 1 FROM unnest($3::text[], $4::int[]) AS l(task, cap) "
            f"JOIN {t['counters']} k ON k.slot = r.task || chr(31) || "
            "coalesce(r.concurrency_key, '') WHERE l.task = r.task AND k.running >= l.cap) "
            "ORDER BY r.prio, r.available_at, r.run_id LIMIT 1 FOR UPDATE OF r SKIP LOCKED"
        )
        self._sql_renew = (
            f"WITH c AS MATERIALIZED (SELECT {_NOW} AS ts) UPDATE {runs} r "  # noqa: S608
            "SET lease_expires_at = c.ts + $4 "
            "FROM c, unnest($2::text[], $3::int[]) AS h(run_id, attempt) "
            "WHERE r.run_id = h.run_id AND r.attempt = h.attempt AND r.worker_id = $1 "
            f"AND r.state IN {_LEASED} RETURNING r.run_id, r.cancel_requested"
        )
        fenced = f"run_id = $1 AND attempt = $2 AND worker_id = $3 AND state IN {_LEASED}"
        self._sql_get_fenced = f"SELECT {cols} FROM {runs} WHERE {fenced} FOR UPDATE"  # noqa: S608
        self._sql_events_head = (
            f"UPDATE {runs} SET event_seq = event_seq + $4, "  # noqa: S608
            "progress = coalesce($5::text::jsonb, progress), "
            f"updated_at = {_NOW} WHERE {fenced} RETURNING event_seq"
        )
        self._sql_events_insert = (
            f"INSERT INTO {t['events']} (run_id, seq, attempt, type, at, data) "  # noqa: S608
            "SELECT $1, s.seq, $2, s.type, s.at, s.data::jsonb FROM "
            "unnest($3::bigint[], $4::text[], $5::float8[], $6::text[]) AS s(seq, type, at, data)"
        )
        self._sql_events_trim = f"DELETE FROM {t['events']} WHERE run_id = $1 AND seq < $2"  # noqa: S608
        self._sql_events_read = (
            f"SELECT seq, attempt, type, at, data::text AS data FROM {t['events']} "  # noqa: S608
            "WHERE run_id = $1 AND seq > $2 ORDER BY seq LIMIT $3"
        )
        self._sql_get_expired = (
            f"SELECT {cols} FROM {runs} WHERE run_id = $1 AND attempt = $2 "  # noqa: S608
            f"AND state IN {_LEASED} AND lease_expires_at <= $3 FOR UPDATE"
        )
        self._sql_find_expired = (
            f"WITH c AS MATERIALIZED (SELECT {_NOW} AS ts) SELECT r.run_id, r.attempt "  # noqa: S608
            f"FROM {runs} r, c WHERE r.state IN {_LEASED} AND r.lease_expires_at <= c.ts "
            "ORDER BY r.lease_expires_at, r.run_id LIMIT $1 FOR UPDATE OF r SKIP LOCKED"
        )
        self._sql_due = (
            f"WITH c AS MATERIALIZED (SELECT {_NOW} AS ts) SELECT {_cols('r')} "  # noqa: S608
            f"FROM {runs} r, c WHERE r.state IN {_CLAIMABLE} AND r.next_publish_at <= c.ts "
            "ORDER BY r.next_publish_at, r.run_id LIMIT $1"
        )
        self._sql_published = (
            f"WITH c AS MATERIALIZED (SELECT {_NOW} AS ts) UPDATE {runs} r "  # noqa: S608
            "SET published_at = c.ts, next_publish_at = c.ts + $4 "
            "FROM c, unnest($1::text[], $2::int[], $3::float8[]) AS s(run_id, attempt, seen) "
            "WHERE r.run_id = s.run_id AND r.attempt = s.attempt AND r.next_publish_at = s.seen "
            f"AND r.state IN {_CLAIMABLE}"
        )
        self._sql_count = f"SELECT running FROM {t['counters']} WHERE slot = $1"  # noqa: S608
        self._sql_lock_counters = f"SELECT slot FROM {t['counters']} ORDER BY slot FOR UPDATE"  # noqa: S608
        held = f"SELECT slot, count(*)::int AS n FROM {runs} WHERE state IN {_LEASED} "  # noqa: S608
        held += "AND slot IS NOT NULL GROUP BY slot"
        self._sql_recount = (
            f"UPDATE {t['counters']} k SET running = coalesce(x.n, 0) FROM ("  # noqa: S608
            f"SELECT s.slot, h.n FROM unnest($1::text[]) AS s(slot) LEFT JOIN ({held}) h "
            "USING (slot)) x WHERE k.slot = x.slot AND k.running <> coalesce(x.n, 0)"
        )
        self._sql_recount_missing = (
            f"INSERT INTO {t['counters']} (slot, running) {held} " "ON CONFLICT (slot) DO NOTHING"
        )
        self._sql_purge = (
            f"WITH c AS MATERIALIZED (SELECT {_NOW} AS ts), "  # noqa: S608
            f"gone AS (DELETE FROM {runs} WHERE run_id IN (SELECT r.run_id FROM {runs} r, c "
            f"WHERE r.state IN {_TERMINAL} AND r.expires_at <= c.ts ORDER BY r.expires_at "
            "LIMIT $1 FOR UPDATE OF r SKIP LOCKED) RETURNING run_id), "
            f"ev AS (DELETE FROM {t['events']} e USING gone WHERE e.run_id = gone.run_id) "
            "SELECT count(*) FROM gone"
        )

    # ------------------------------------------------------------------ connections

    @property
    def schema(self) -> str:
        return self._schema

    async def _get_pool(self) -> Any:
        if self._closed:
            raise RuntimeError("PostgresRunStore is closed")
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    import asyncpg  # - optional dependency

                    self._pool = await asyncpg.create_pool(self._dsn, **self._pool_config)
                    self._owns_pool = True
        return self._pool

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[Any]:
        pool = await self._get_pool()
        async with pool.acquire(timeout=self.acquire_timeout) as conn:
            yield conn

    # ------------------------------------------------------------------ lifecycle

    async def setup(self) -> None:
        if self._ready:
            return
        async with self._conn() as conn:
            if self._auto_migrate:
                self.ddl_statements = await ensure_schema(conn, self._schema)
            else:
                version = await recorded_version(conn, self._schema)
                if version < CURRENT_SCHEMA_VERSION:
                    raise JobsSchemaError(
                        f"jobs tables in schema {self._schema!r} are at version {version}, "
                        f"this code needs {CURRENT_SCHEMA_VERSION}: run `10xgraph tasks "
                        "migrate` or set auto_migrate"
                    )
        self._ready = True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        pool, self._pool = self._pool, None
        if pool is not None and self._owns_pool:
            await pool.close()

    async def now(self) -> float:
        async with self._conn() as conn:
            return await _now(conn)

    # ------------------------------------------------------------------ helpers

    async def _write(
        self, conn: Any, run: TaskRun, fields: Sequence[str], *, attempt: int
    ) -> TaskRun:
        """Write ``fields`` of a run locked in this transaction (fenced on ``attempt``)."""
        sets = ", ".join(
            f"{name} = ${i}::text::jsonb" if name in JSON_COLUMNS else f"{name} = ${i}"
            for i, name in enumerate(fields, start=3)
        )
        row = await conn.fetchrow(
            f"UPDATE {self._t['runs']} SET {sets} WHERE run_id = $1 AND attempt = $2 "  # noqa: S608
            f"RETURNING {_cols()}",
            run.run_id,
            attempt,
            *(_param(name, getattr(run, name)) for name in fields),
        )
        if row is None:  # pragma: no cover - the row is locked by the caller
            raise RuntimeError(f"run {run.run_id} changed while locked")
        return _run(row)

    async def _take_slot(self, conn: Any, slot: str, cap: int) -> bool:
        if cap <= 0:
            return False
        return await conn.fetchval(self._sql_take_slot, slot, cap) is not None

    # ------------------------------------------------------------------ create and read

    async def create_run(self, new: NewRun, *, key_mode: KeyMode = "keep") -> CreateResult:
        import asyncpg  # - optional dependency

        serialization.check_new_run(new)
        key = (new.task, new.owner_user_id, new.tenant, new.idempotency_key)
        async with self._conn() as conn:
            for _ in range(5):
                async with conn.transaction():
                    now = await _now(conn)
                    available_at = new.at if new.at is not None else now + (new.delay or 0.0)
                    try:
                        row = await conn.fetchrow(
                            self._sql_insert,
                            new.run_id,
                            new.task,
                            new.queue,
                            new.priority,
                            _dumps(new.kwargs, "kwargs"),
                            _dumps(new.identity, "identity"),
                            new.owner_user_id,
                            new.tenant,
                            new.idempotency_key,
                            new.concurrency_key,
                            new.max_retries,
                            new.max_lost,
                            new.retention,
                            _dumps(new.trigger, "trigger"),
                            new.parent_run_id,
                            new.code_version,
                            available_at,
                            now,
                        )
                    except asyncpg.UniqueViolationError as exc:
                        raise ValueError(f"run id {new.run_id} already exists") from exc
                    if row is not None:
                        return CreateResult(run=_run(row), created=True, due=available_at <= now)
                    if key_mode == "replace":
                        row = await conn.fetchrow(
                            self._sql_replace,
                            *key,
                            _dumps(new.kwargs, "kwargs"),
                            new.concurrency_key,
                            now,
                        )
                        if row is not None:
                            run = _run(row)
                            return CreateResult(
                                run=run, created=False, replaced=True, due=run.available_at <= now
                            )
                    row = await conn.fetchrow(self._sql_by_key, *key)
                    if row is not None:
                        return CreateResult(run=_run(row), created=False)
                # The run holding the key was purged between the insert and the read: retry.
        raise RuntimeError(f"create_run could not settle the key {new.idempotency_key!r}")

    async def get_run(self, run_id: str) -> TaskRun | None:
        async with self._conn() as conn:
            row = await conn.fetchrow(self._sql_get, run_id)
        return _run(row) if row is not None else None

    async def list_runs(
        self,
        filters: RunFilter | None = None,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> RunPage:
        limit = max(1, min(MAX_PAGE, limit))
        f = filters or RunFilter()
        where: list[str] = []
        args: list[Any] = []

        def arg(value: Any) -> str:
            args.append(value)
            return f"${len(args)}"

        if f.task is not None:
            where.append(f"task = {arg(f.task)}")
        if f.state is not None:
            raw = f.state if isinstance(f.state, tuple) else (f.state,)
            where.append(f"state = ANY({arg([str(s) for s in raw])}::text[])")
        if f.queue is not None:
            where.append(f"queue = {arg(f.queue)}")
        if f.owner_user_id is not None:
            where.append(f"owner_user_id = {arg(f.owner_user_id)}")
        if cursor:
            enqueued_at, run_id = _decode_cursor(cursor)
            where.append(f"(enqueued_at, run_id) < ({arg(enqueued_at)}, {arg(run_id)})")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (
            f"SELECT {_cols()} FROM {self._t['runs']} {clause} "  # noqa: S608
            f"ORDER BY enqueued_at DESC, run_id DESC LIMIT {arg(limit + 1)}"
        )
        async with self._conn() as conn:
            rows = await conn.fetch(sql, *args)
        items = [_run(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = _encode_cursor(items[-1].enqueued_at, items[-1].run_id)
        return RunPage(items=items, next_cursor=next_cursor)

    # ------------------------------------------------------------------ worker writes

    async def claim(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limit: int | None = None,
    ) -> ClaimResult:
        async with self._conn() as conn:
            if concurrency_limit is None:
                row = await conn.fetchrow(
                    self._sql_claim_now, run_id, worker_id, float(lease_ttl), None
                )
                if row is not None:
                    return ClaimResult(status=ClaimStatus.CLAIMED, run=_run(row))
                row = await conn.fetchrow(self._sql_get, run_id)
            else:
                deadlocks = 0
                while True:
                    try:
                        async with conn.transaction():
                            now = await _now(conn)
                            row = await conn.fetchrow(self._sql_get_for_update, run_id)
                            if row is not None:
                                run = _run(row)
                                if run.state in CLAIMABLE_STATES and run.available_at <= now:
                                    return await self._claim_capped(
                                        conn, run, now, worker_id, float(lease_ttl),
                                        concurrency_limit,
                                    )
                        break
                    except Exception as exc:
                        if not _is_deadlock(exc) or deadlocks >= DEADLOCK_RETRIES:
                            raise
                        deadlocks += 1
                        await asyncio.sleep(DEADLOCK_BACKOFF * deadlocks)
        if row is None:
            return ClaimResult(status=ClaimStatus.NOT_FOUND)
        return ClaimResult(status=ClaimStatus.NOT_READY, run=_run(row))

    async def _claim_capped(
        self, conn: Any, run: TaskRun, now: float, worker_id: str, lease_ttl: float, cap: int
    ) -> ClaimResult:
        """Second half of a capped claim: the run row is locked and claimable."""
        slot = slot_name(run.task, run.concurrency_key)
        if await self._take_slot(conn, slot, cap):
            row = await conn.fetchrow(
                self._sql_claim_at, run.run_id, worker_id, lease_ttl, slot, now
            )
            return ClaimResult(status=ClaimStatus.CLAIMED, run=_run(row))
        run.available_at = now + min(CAP_WAIT_MAX, CAP_WAIT_BASE * 2**run.cap_waits)
        run.cap_waits += 1
        run.next_publish_at = run.available_at
        run.updated_at = now
        capped = await self._write(conn, run, _CAPPED_FIELDS, attempt=run.attempt)
        return ClaimResult(status=ClaimStatus.CAPPED, run=capped)

    async def claim_next(
        self,
        queues: Sequence[str],
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limits: Mapping[str, int] | None = None,
    ) -> TaskRun | None:
        """Pick and claim in one transaction, taking at most one counter row lock.

        The pick reads the counters without locking them (a plain read in its ``NOT EXISTS``),
        so a full cap is skipped without a lock. Only the chosen run's counter is then locked
        by the conditional upsert; when that loses a race (the counter filled up meanwhile, and
        the failed upsert still locks the row), the transaction ends and the next pick runs in
        a new one. One counter per transaction, after the run row, matches every other path
        (claim, transition, mark_lost) and cannot form a cycle with ``recount_concurrency``,
        which locks counters only. A deadlock reported anyway is retried a few times.
        """
        if not queues:
            return None
        limits = dict(concurrency_limits or {})
        skipped: list[str] = []
        deadlocks = 0
        for _ in range(CLAIM_NEXT_PICKS):
            try:
                done, run = await self._claim_next_once(
                    list(queues), worker_id, float(lease_ttl), limits, skipped
                )
            except Exception as exc:
                if not _is_deadlock(exc) or deadlocks >= DEADLOCK_RETRIES:
                    raise
                deadlocks += 1
                await asyncio.sleep(DEADLOCK_BACKOFF * deadlocks)
                continue
            if done:
                return run
        return None

    async def _claim_next_once(
        self,
        queues: list[str],
        worker_id: str,
        lease_ttl: float,
        limits: dict[str, int],
        skipped: list[str],
    ) -> tuple[bool, TaskRun | None]:
        """One transaction of ``claim_next``: ``(True, run_or_None)`` when settled, ``(False,
        None)`` when a counter race ended it and the caller should pick again."""
        tasks, caps = list(limits), [int(v) for v in limits.values()]
        async with self._conn() as conn, conn.transaction():
            now = await _now(conn)
            pick = await conn.fetchrow(self._sql_pick, queues, now, tasks, caps, skipped)
            if pick is None:
                return True, None
            slot = None
            if pick["task"] in limits:
                slot = slot_name(pick["task"], pick["concurrency_key"])
                if not await self._take_slot(conn, slot, limits[pick["task"]]):
                    skipped.append(pick["run_id"])
                    return False, None
            row = await conn.fetchrow(
                self._sql_claim_at, pick["run_id"], worker_id, lease_ttl, slot, now
            )
            return True, _run(row)

    async def renew_leases(
        self,
        worker_id: str,
        held: Sequence[HeldLease],
        *,
        lease_ttl: float,
    ) -> dict[str, bool]:
        if not held:
            return {}
        async with self._conn() as conn:
            rows = await conn.fetch(
                self._sql_renew,
                worker_id,
                [h.run_id for h in held],
                [h.attempt for h in held],
                float(lease_ttl),
            )
        return {r["run_id"]: bool(r["cancel_requested"]) for r in rows}

    async def append_events(
        self,
        run_id: str,
        *,
        attempt: int,
        worker_id: str,
        events: Sequence[EventIn],
        progress: dict | None = None,
    ) -> bool:
        serialization.check_events(events, progress)
        snapshot = None if progress is None else _dumps(progress, "progress")
        async with self._conn() as conn, conn.transaction():
            end = await conn.fetchval(
                self._sql_events_head, run_id, attempt, worker_id, len(events), snapshot
            )
            if end is None:
                return False
            if events:
                first = int(end) - len(events) + 1
                keep_from = int(end) - EVENT_LOG_CAP + 1
                kept = [(first + i, e) for i, e in enumerate(events) if first + i >= keep_from]
                await conn.execute(
                    self._sql_events_insert,
                    run_id,
                    attempt,
                    [seq for seq, _ in kept],
                    [e.type for _, e in kept],
                    [float(e.at) for _, e in kept],
                    [_dumps(e.data, "event.data") for _, e in kept],
                )
                if keep_from > 1:
                    await conn.execute(self._sql_events_trim, run_id, keep_from)
        return True

    async def transition(
        self,
        run_id: str,
        *,
        attempt: int,
        worker_id: str,
        outcome: AttemptOutcome,
    ) -> TaskRun | None:
        serialization.check_outcome(outcome)
        async with self._conn() as conn, conn.transaction():
            now = await _now(conn)
            row = await conn.fetchrow(self._sql_get_fenced, run_id, attempt, worker_id)
            if row is None:
                return None
            run = _run(row)
            slot = run.slot
            _apply_outcome(run, outcome, now)
            after = await self._write(conn, run, _TRANSITION_FIELDS, attempt=attempt)
            if slot is not None:
                await conn.execute(self._sql_release_slot, slot)
            return after

    # ------------------------------------------------------------------ callers

    async def request_cancel(self, run_id: str) -> CancelResult:
        async with self._conn() as conn, conn.transaction():
            now = await _now(conn)
            row = await conn.fetchrow(self._sql_get_for_update, run_id)
            if row is None:
                return CancelResult(status=CancelStatus.NOT_FOUND)
            run = _run(row)
            if run.state in TERMINAL_STATES:
                return CancelResult(status=CancelStatus.TERMINAL, run=run)
            if run.state == RunState.CANCELLING:
                return CancelResult(status=CancelStatus.ALREADY_REQUESTED, run=run)
            run.updated_at = now
            if run.state in CLAIMABLE_STATES:
                _finish(run, RunState.CANCELLED, now)
                status = CancelStatus.CANCELLED
            else:
                run.state = RunState.CANCELLING
                run.cancel_requested = True
                status = CancelStatus.REQUESTED
            after = await self._write(conn, run, _CANCEL_FIELDS, attempt=run.attempt)
            return CancelResult(status=status, run=after)

    async def read_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> list[TaskEvent]:
        limit = max(1, min(MAX_PAGE, limit))
        async with self._conn() as conn:
            rows = await conn.fetch(self._sql_events_read, run_id, after, limit)
        return [
            TaskEvent(
                seq=r["seq"],
                attempt=r["attempt"],
                type=r["type"],
                at=r["at"],
                data=json.loads(r["data"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ sweeper

    async def find_expired_leases(self, *, limit: int = 100) -> list[HeldLease]:
        async with self._conn() as conn:
            rows = await conn.fetch(self._sql_find_expired, max(1, limit))
        return [HeldLease(run_id=r["run_id"], attempt=r["attempt"]) for r in rows]

    async def mark_lost(self, run_id: str, *, attempt: int) -> LostResult | None:
        async with self._conn() as conn, conn.transaction():
            now = await _now(conn)
            row = await conn.fetchrow(self._sql_get_expired, run_id, attempt, now)
            if row is None:
                return None
            run = _run(row)
            slot = run.slot
            _apply_lost(run, now)
            after = await self._write(conn, run, _LOST_FIELDS, attempt=attempt)
            if slot is not None:
                await conn.execute(self._sql_release_slot, slot)
            return LostResult(run=after)

    async def due_runs(self, *, limit: int = 100) -> list[TaskRun]:
        async with self._conn() as conn:
            rows = await conn.fetch(self._sql_due, max(1, limit))
        return [_run(r) for r in rows]

    async def mark_published(
        self, seen: Sequence[TaskRun], *, republish_after: float = REPUBLISH_AFTER
    ) -> None:
        marks = published_marks(seen)
        if not marks:
            return
        async with self._conn() as conn:
            await conn.execute(
                self._sql_published,
                [m[0] for m in marks],
                [m[1] for m in marks],
                [m[2] for m in marks],
                max(0.0, float(republish_after)),
            )

    async def concurrency_count(self, task: str, key: str | None = None) -> int:
        async with self._conn() as conn:
            value = await conn.fetchval(self._sql_count, slot_name(task, key))
        return int(value or 0)

    async def recount_concurrency(self) -> None:
        async with self._conn() as conn, conn.transaction():
            # Lock every counter row first; the count below then runs on a newer snapshot, so
            # a claim or transition either committed before (counted) or waits for us.
            slots = [r["slot"] for r in await conn.fetch(self._sql_lock_counters)]
            if slots:
                await conn.execute(self._sql_recount, slots)
            await conn.execute(self._sql_recount_missing)

    async def purge_expired(self, *, limit: int = 1000) -> int:
        async with self._conn() as conn:
            return int(await conn.fetchval(self._sql_purge, max(1, limit)))


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DDL_LOCK_KEY",
    "JobsSchemaError",
    "PostgresRunStore",
    "ensure_schema",
    "migrate",
    "recorded_version",
    "table_names",
]
