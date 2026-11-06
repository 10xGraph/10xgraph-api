"""Postgres jobs schema: table DDL, ordered migrations, ``setup`` and ``migrate``.

Every table is ``tenxgraph_task_<name>`` in one schema (default ``public``). ``ensure_schema``
runs under core's ``ddl_session`` (advisory lock ``DDL_LOCK_KEY`` plus ``lock_timeout``) and
does nothing, with no lock, when the recorded version is current. Advisory session locks need
a direct or session-mode connection (not PgBouncer transaction pooling).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from tenxgraph.storage.columns import ddl_session

from tenxgraph_api.jobs.errors import JobsError

from .queries import NOW


logger = logging.getLogger("tenxgraph_api.jobs.store.postgres")

#: Bump with a new entry in ``_MIGRATIONS`` (and the table DDL) when the schema changes.
CURRENT_SCHEMA_VERSION = 1
#: Session advisory lock key for jobs setup DDL ("TXGTASK1"); not shared with the checkpointer.
DDL_LOCK_KEY = 0x5458475441534B31
#: Seconds a setup DDL statement waits for a table lock.
DDL_LOCK_TIMEOUT = 10.0
TABLE_PREFIX = "tenxgraph_task_"

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

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
                f"VALUES ($1, {NOW}) ON CONFLICT (version) DO NOTHING",
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


