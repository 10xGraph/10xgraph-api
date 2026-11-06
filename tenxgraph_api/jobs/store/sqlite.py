# SQL here is built from module constants (table names, fixed IN lists, "?" marks);
# every value is a bound parameter.
# ruff: noqa: S608
"""SQLite run store: the ``local`` backend (``"worker": {"backend": "local"}``).

One file, ``.10xgraph/jobs.db`` by default, shared by the API process (enqueue) and one or more
local worker processes. Safe across processes that share the file:

- ``journal_mode=WAL`` (readers never block the writer), ``synchronous=NORMAL``,
  ``busy_timeout=5000``.
- Every write is one ``BEGIN IMMEDIATE`` transaction: the write lock is taken up front, so a
  read-check-write inside it is atomic for every process. "database is locked" (or busy) is
  retried a bounded number of times with backoff, the transaction rolled back and run again.
- The store clock is SQLite's: ``unixepoch('subsec')`` (SQLite 3.42+), else
  ``(julianday('now') - 2440587.5) * 86400``. Read inside the write transaction, so every
  timestamp of one call is the same instant.
- Fencing by ``attempt``; concurrency counters live in ``tenxgraph_task_counters`` and change in
  the same transaction as the claim or exit that takes or gives back the slot.
- Schema version in ``tenxgraph_task_schema`` (one row per component); migrations are applied
  in order under ``BEGIN IMMEDIATE``, so ``setup()`` is idempotent and safe from many
  processes at once. A file written by a newer version is refused.

``claim_next`` is the single-statement claim from Sprint 0 (``UPDATE ... WHERE run_id =
(SELECT ... ORDER BY priority, available_at, run_id LIMIT 1) RETURNING``). The worker today
claims through the transport (``SqliteTransport``, a polling table in the same file, 200 ms);
``claim_next`` lets a local worker skip the transport altogether later. Both paths go through
the same row and the same fencing, so they can be mixed.

Requires SQLite 3.35+ (``RETURNING``) and ``aiosqlite`` (the ``jobs`` extra).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from tenxgraph_api.jobs.models import (
    CAP_WAIT_BASE,
    CAP_WAIT_MAX,
    CLAIMABLE_STATES,
    EVENT_LOG_CAP,
    LEASED_STATES,
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

from .base import BaseRunStore, slot_name


try:  # the ``jobs`` extra
    import aiosqlite
except ImportError:  # pragma: no cover - exercised only without the extra
    aiosqlite = None  # type: ignore[assignment]


logger = logging.getLogger("tenxgraph_api.jobs")

T = TypeVar("T")

DEFAULT_PATH = ".10xgraph/jobs.db"
#: ``RETURNING`` arrived in 3.35.0.
MIN_SQLITE_VERSION = (3, 35, 0)
#: ``unixepoch('subsec')`` arrived in 3.42.0.
SUBSEC_SQLITE_VERSION = (3, 42, 0)
DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_LOCK_RETRIES = 5
MAX_PAGE = 1000
#: Most bound parameters per ``IN (...)`` list (well under SQLite's limit).
IN_CHUNK = 500

RUNS = "tenxgraph_task_runs"
EVENTS = "tenxgraph_task_events"
COUNTERS = "tenxgraph_task_counters"
SCHEMA = "tenxgraph_task_schema"

_LEASED_SQL = "('running', 'cancelling')"
_CLAIMABLE_SQL = "('queued', 'retrying')"
_TERMINAL_SQL = "('succeeded', 'failed', 'cancelled')"
_PRIORITY_RANK_SQL = "CASE {col} WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"

#: ``TaskRun`` fields stored as JSON text.
_JSON_FIELDS = frozenset({"kwargs", "identity", "trigger", "progress", "result", "error"})
_FIELDS: tuple[str, ...] = tuple(TaskRun.model_fields)


class SqliteVersionError(RuntimeError):
    """The SQLite library linked into Python is too old for the jobs store."""


class SqliteSchemaError(RuntimeError):
    """The jobs database was written by a newer version of 10xgraph-api."""


# ---------------------------------------------------------------------- shared helpers
# Used by ``transport/sqlite.py`` too, so both open the file the same way.


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".")[:3] if part.isdigit())


def check_sqlite_version(version: str | None = None) -> tuple[int, ...]:
    """Raise ``SqliteVersionError`` unless SQLite is at least 3.35 (``RETURNING``)."""
    found = version if version is not None else sqlite3.sqlite_version
    parsed = _version_tuple(found)
    if parsed < MIN_SQLITE_VERSION:
        need = ".".join(str(p) for p in MIN_SQLITE_VERSION)
        raise SqliteVersionError(
            f"the jobs 'local' backend needs SQLite {need} or newer (for RETURNING); this "
            f"Python is linked against SQLite {found}. Upgrade Python or its SQLite library, "
            "or use the Postgres or Redis jobs backend."
        )
    return parsed


def clock_sql(version: str | None = None) -> str:
    """SQL expression for the store clock in epoch seconds (REAL)."""
    found = version if version is not None else sqlite3.sqlite_version
    if _version_tuple(found) >= SUBSEC_SQLITE_VERSION:
        return "unixepoch('subsec')"
    return "((julianday('now') - 2440587.5) * 86400.0)"


def require_aiosqlite() -> None:
    if aiosqlite is None:
        raise ImportError(
            "the jobs 'local' backend needs aiosqlite: pip install '10xgraph-api[jobs]'"
        )


def is_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def prepare_file(path: str | Path) -> None:
    """Create the parent directory (0o700) and an empty file (0o600) before SQLite does, so the
    ``-wal`` and ``-shm`` files get owner-only modes too. Existing ones keep their modes."""
    if str(path) == ":memory:" or str(path).startswith("file:"):
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not target.exists():
        target.touch(mode=0o600, exist_ok=True)


async def open_connection(path: str | Path, *, busy_timeout_ms: int) -> Any:
    """An aiosqlite connection in autocommit mode (we issue BEGIN/COMMIT) with the pragmas."""
    require_aiosqlite()
    prepare_file(path)
    conn = await aiosqlite.connect(str(path), isolation_level=None)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        rows = await conn.execute_fetchall("PRAGMA journal_mode=WAL")
        mode = str(next(iter(rows))[0]).lower() if rows else ""
        if mode != "wal" and str(path) != ":memory:":
            raise RuntimeError(f"could not switch {path} to WAL (journal_mode={mode!r})")
        await conn.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        await conn.close()
        raise
    return conn


async def write_tx(
    conn: Any,
    lock: asyncio.Lock,
    fn: Callable[[Any], Awaitable[T]],
    *,
    retries: int,
    backoff: float = 0.02,
) -> T:
    """Run ``fn(conn)`` inside one ``BEGIN IMMEDIATE`` transaction and commit.

    A lock error (after ``busy_timeout`` ran out) rolls back and runs ``fn`` again, at most
    ``retries`` more times; any other error (or a cancel) rolls back and propagates.
    """
    attempt = 0
    while True:
        async with lock:
            try:
                if conn.in_transaction:  # left open by an interrupted call: undo it
                    await conn.execute("ROLLBACK")
                await conn.execute("BEGIN IMMEDIATE")
                result = await fn(conn)
                await conn.execute("COMMIT")
                return result
            except BaseException as exc:
                await _rollback(conn)
                if not is_lock_error(exc) or attempt >= retries:
                    raise
        attempt += 1
        delay = min(1.0, backoff * 2**attempt)
        logger.debug("jobs sqlite database is locked; retry %d in %.2f s", attempt, delay)
        await asyncio.sleep(delay)


async def _rollback(conn: Any) -> None:
    try:
        if conn.in_transaction:
            await asyncio.shield(conn.execute("ROLLBACK"))
    except Exception:  # the connection may be gone; nothing else to undo
        logger.debug("jobs sqlite rollback failed", exc_info=True)


async def migrate(
    conn: Any,
    lock: asyncio.Lock,
    component: str,
    migrations: Sequence[Sequence[str]],
    *,
    retries: int,
) -> int:
    """Apply ``migrations[n:]`` for ``component`` (version n) in one transaction."""

    async def run(c: Any) -> int:
        await c.execute(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA} "
            "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        rows = await c.execute_fetchall(
            f"SELECT version FROM {SCHEMA} WHERE component = ?", (component,)
        )
        current = int(next(iter(rows))[0]) if rows else 0
        target = len(migrations)
        if current > target:
            raise SqliteSchemaError(
                f"jobs database {component} schema is version {current}, newer than this "
                f"code ({target}); upgrade 10xgraph-api or use another file"
            )
        for statements in migrations[current:]:
            for statement in statements:
                await c.execute(statement)
        if current != target:
            await c.execute(
                f"INSERT INTO {SCHEMA} (component, version) VALUES (?, ?) "
                "ON CONFLICT(component) DO UPDATE SET version = excluded.version",
                (component, target),
            )
        return target

    return await write_tx(conn, lock, run, retries=retries)


# ---------------------------------------------------------------------- schema

STORE_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        f"""CREATE TABLE {RUNS} (
            run_id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            queue TEXT NOT NULL,
            state TEXT NOT NULL,
            priority TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            lost_count INTEGER NOT NULL DEFAULT 0,
            max_lost INTEGER NOT NULL DEFAULT 2,
            defer_count INTEGER NOT NULL DEFAULT 0,
            cap_waits INTEGER NOT NULL DEFAULT 0,
            kwargs TEXT NOT NULL,
            identity TEXT NOT NULL,
            owner_user_id TEXT,
            tenant TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT,
            concurrency_key TEXT,
            slot TEXT,
            trigger TEXT NOT NULL,
            worker_id TEXT,
            lease_expires_at REAL,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            progress TEXT NOT NULL,
            result TEXT,
            error TEXT,
            parent_run_id TEXT,
            batch_id TEXT,
            code_version TEXT,
            retention REAL,
            available_at REAL NOT NULL,
            enqueued_at REAL NOT NULL,
            published_at REAL,
            next_publish_at REAL,
            started_at REAL,
            finished_at REAL,
            updated_at REAL NOT NULL,
            expires_at REAL,
            event_seq INTEGER NOT NULL DEFAULT 0
        )""",
        f"CREATE UNIQUE INDEX {RUNS}_key ON {RUNS} "
        "(task, owner_user_id, tenant, idempotency_key) WHERE idempotency_key IS NOT NULL",
        f"CREATE INDEX {RUNS}_claim ON {RUNS} (queue, state, available_at)",
        f"CREATE INDEX {RUNS}_publish ON {RUNS} (state, next_publish_at)",
        f"CREATE INDEX {RUNS}_lease ON {RUNS} (state, lease_expires_at)",
        f"CREATE INDEX {RUNS}_expires ON {RUNS} (expires_at) WHERE expires_at IS NOT NULL",
        f"CREATE INDEX {RUNS}_list ON {RUNS} (enqueued_at DESC, run_id DESC)",
        f"""CREATE TABLE {EVENTS} (
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            attempt INTEGER NOT NULL,
            type TEXT NOT NULL,
            at REAL NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (run_id, seq)
        ) WITHOUT ROWID""",
        f"""CREATE TABLE {COUNTERS} (
            name TEXT PRIMARY KEY,
            running INTEGER NOT NULL DEFAULT 0 CHECK (running >= 0)
        ) WITHOUT ROWID""",
    ),
)


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _params(run: TaskRun) -> list[Any]:
    out: list[Any] = []
    for name in _FIELDS:
        value = getattr(run, name)
        if name in _JSON_FIELDS:
            value = None if value is None and name in ("result", "error") else _dump(value)
        elif name == "cancel_requested":
            value = 1 if value else 0
        elif name == "state":
            value = RunState(value).value
        out.append(value)
    return out


def _from_row(row: Any) -> TaskRun:
    data: dict[str, Any] = {}
    for name in _FIELDS:
        value = row[name]
        if name in _JSON_FIELDS:
            value = None if value is None else json.loads(value)
        elif name == "cancel_requested":
            value = bool(value)
        data[name] = value
    return TaskRun(**data)


_INSERT_SQL = (
    f"INSERT INTO {RUNS} ({', '.join(_FIELDS)}) VALUES ({', '.join('?' for _ in _FIELDS)})"
)
_SAVE_SQL = (
    f"UPDATE {RUNS} SET {', '.join(f'{n} = ?' for n in _FIELDS if n != 'run_id')} "
    "WHERE run_id = ?"
)
_SELECT_COLS = ", ".join(_FIELDS)


class SqliteRunStore(BaseRunStore):
    """Run store in one SQLite file. See the module docstring.

    ``busy_timeout_ms`` is how long SQLite waits for the write lock; ``lock_retries`` how many
    times a write that still found the database locked is run again.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_PATH,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        lock_retries: int = DEFAULT_LOCK_RETRIES,
    ) -> None:
        check_sqlite_version()
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self.lock_retries = lock_retries
        self._clock = clock_sql()
        self._conn: Any = None
        self._lock = asyncio.Lock()
        self._open_lock = asyncio.Lock()
        self._ready = False
        self._closed = False

    # ------------------------------------------------------------------ connection

    async def _connection(self) -> Any:
        if self._ready and self._conn is not None:
            return self._conn
        async with self._open_lock:
            if self._closed:
                raise RuntimeError("SqliteRunStore is closed")
            if self._conn is None:
                self._conn = await open_connection(self.path, busy_timeout_ms=self.busy_timeout_ms)
            if not self._ready:
                await migrate(
                    self._conn, self._lock, "store", STORE_MIGRATIONS, retries=self.lock_retries
                )
                self._ready = True
        return self._conn

    async def _write(self, fn: Callable[[Any], Awaitable[T]]) -> T:
        conn = await self._connection()
        return await write_tx(conn, self._lock, fn, retries=self.lock_retries)

    async def _read(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        conn = await self._connection()
        async with self._lock:
            return list(await conn.execute_fetchall(sql, tuple(params)))

    async def _now(self, conn: Any) -> float:
        rows = await conn.execute_fetchall(f"SELECT {self._clock}")
        return float(next(iter(rows))[0])

    @staticmethod
    async def _get(conn: Any, run_id: str) -> TaskRun | None:
        rows = await conn.execute_fetchall(
            f"SELECT {_SELECT_COLS} FROM {RUNS} WHERE run_id = ?", (run_id,)
        )
        return _from_row(next(iter(rows))) if rows else None

    @staticmethod
    async def _save(conn: Any, run: TaskRun) -> None:
        params = _params(run)
        await conn.execute(_SAVE_SQL, (*params[1:], params[0]))

    @staticmethod
    async def _fenced(conn: Any, run_id: str, attempt: int, worker_id: str) -> TaskRun | None:
        run = await SqliteRunStore._get(conn, run_id)
        if (
            run is None
            or run.state not in LEASED_STATES
            or run.attempt != attempt
            or run.worker_id != worker_id
        ):
            return None
        return run

    @staticmethod
    async def _counter(conn: Any, slot: str) -> int:
        rows = await conn.execute_fetchall(
            f"SELECT running FROM {COUNTERS} WHERE name = ?", (slot,)
        )
        return int(next(iter(rows))[0]) if rows else 0

    @staticmethod
    async def _take_slot(conn: Any, slot: str) -> None:
        await conn.execute(
            f"INSERT INTO {COUNTERS} (name, running) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET running = running + 1",
            (slot,),
        )

    @staticmethod
    async def _release_slot(conn: Any, run: TaskRun) -> None:
        if run.slot is not None:
            await conn.execute(
                f"UPDATE {COUNTERS} SET running = max(0, running - 1) WHERE name = ?",
                (run.slot,),
            )
            run.slot = None

    @staticmethod
    def _finish(run: TaskRun, state: RunState, now: float) -> None:
        run.state = state
        run.next_publish_at = None
        run.finished_at = now
        run.expires_at = None if run.retention is None else now + run.retention

    @staticmethod
    def _clear_lease(run: TaskRun) -> None:
        run.worker_id = None
        run.lease_expires_at = None

    # ------------------------------------------------------------------ lifecycle

    async def setup(self) -> None:
        await self._connection()

    async def aclose(self) -> None:
        async with self._open_lock:
            self._closed = True
            conn, self._conn = self._conn, None
            self._ready = False
        if conn is not None:
            async with self._lock:
                await _rollback(conn)
                await conn.close()

    async def now(self) -> float:
        rows = await self._read(f"SELECT {self._clock}")
        return float(next(iter(rows))[0])

    # ------------------------------------------------------------------ create and read

    async def create_run(self, new: NewRun, *, key_mode: KeyMode = "keep") -> CreateResult:
        async def run(conn: Any) -> CreateResult:
            now = await self._now(conn)
            if new.idempotency_key is not None:
                rows = await conn.execute_fetchall(
                    f"SELECT {_SELECT_COLS} FROM {RUNS} WHERE task = ? AND owner_user_id IS ? "
                    "AND tenant = ? AND idempotency_key = ?",
                    (new.task, new.owner_user_id, new.tenant, new.idempotency_key),
                )
                if rows:
                    existing = _from_row(next(iter(rows)))
                    if key_mode == "replace" and existing.state == RunState.QUEUED:
                        existing.kwargs = dict(new.kwargs)
                        existing.concurrency_key = new.concurrency_key
                        existing.updated_at = now
                        await self._save(conn, existing)
                        return CreateResult(
                            run=existing,
                            created=False,
                            replaced=True,
                            due=existing.available_at <= now,
                        )
                    return CreateResult(run=existing, created=False)
            exists = await conn.execute_fetchall(
                f"SELECT 1 FROM {RUNS} WHERE run_id = ?", (new.run_id,)
            )
            if exists:
                raise ValueError(f"run id {new.run_id} already exists")
            available_at = new.at if new.at is not None else now + (new.delay or 0.0)
            record = TaskRun(
                **new.model_dump(exclude={"delay", "at"}),
                state=RunState.QUEUED,
                available_at=available_at,
                next_publish_at=available_at,
                enqueued_at=now,
                updated_at=now,
            )
            await conn.execute(_INSERT_SQL, _params(record))
            return CreateResult(run=record, created=True, due=available_at <= now)

        return await self._write(run)

    async def get_run(self, run_id: str) -> TaskRun | None:
        rows = await self._read(f"SELECT {_SELECT_COLS} FROM {RUNS} WHERE run_id = ?", (run_id,))
        return _from_row(rows[0]) if rows else None

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
        params: list[Any] = []
        for column, value in (
            ("task", f.task),
            ("queue", f.queue),
            ("owner_user_id", f.owner_user_id),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if f.state is not None:
            raw = f.state if isinstance(f.state, tuple) else (f.state,)
            states = sorted({str(s.value if isinstance(s, RunState) else s) for s in raw})
            where.append(f"state IN ({', '.join('?' for _ in states)})")
            params.extend(states)
        if cursor:
            enqueued_at, run_id = _decode_cursor(cursor)
            where.append("(enqueued_at, run_id) < (?, ?)")
            params.extend((enqueued_at, run_id))
        sql = f"SELECT {_SELECT_COLS} FROM {RUNS}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY enqueued_at DESC, run_id DESC LIMIT ?"
        params.append(limit + 1)
        rows = [_from_row(r) for r in await self._read(sql, params)]
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = _encode_cursor(last.enqueued_at, last.run_id)
        return RunPage(items=page, next_cursor=next_cursor)

    # ------------------------------------------------------------------ worker writes

    async def claim(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limit: int | None = None,
    ) -> ClaimResult:
        async def run(conn: Any) -> ClaimResult:
            now = await self._now(conn)
            record = await self._get(conn, run_id)
            if record is None:
                return ClaimResult(status=ClaimStatus.NOT_FOUND)
            if record.state not in CLAIMABLE_STATES or record.available_at > now:
                return ClaimResult(status=ClaimStatus.NOT_READY, run=record)
            slot = None
            if concurrency_limit is not None:
                slot = slot_name(record.task, record.concurrency_key)
                if await self._counter(conn, slot) >= concurrency_limit:
                    record.available_at = now + min(
                        CAP_WAIT_MAX, CAP_WAIT_BASE * 2**record.cap_waits
                    )
                    record.cap_waits += 1
                    record.next_publish_at = record.available_at
                    record.updated_at = now
                    await self._save(conn, record)
                    return ClaimResult(status=ClaimStatus.CAPPED, run=record)
                await self._take_slot(conn, slot)
            record.state = RunState.RUNNING
            record.attempt += 1
            record.worker_id = worker_id
            record.lease_expires_at = now + lease_ttl
            record.started_at = now
            record.cancel_requested = False
            record.error = None
            record.progress = {}
            record.cap_waits = 0
            record.slot = slot
            record.next_publish_at = None
            record.updated_at = now
            await self._save(conn, record)
            return ClaimResult(status=ClaimStatus.CLAIMED, run=record)

        return await self._write(run)

    async def claim_next(
        self,
        queues: Sequence[str],
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limits: Mapping[str, int] | None = None,
    ) -> TaskRun | None:
        """One ``UPDATE ... WHERE run_id = (SELECT ... LIMIT 1) RETURNING`` plus the counter
        increment, in one ``BEGIN IMMEDIATE`` transaction."""
        wanted = list(dict.fromkeys(queues))
        if not wanted:
            return None
        limits = dict(concurrency_limits or {})
        if limits:
            limits_cte = " UNION ALL ".join("SELECT ? AS task, ? AS cap" for _ in limits)
            limit_params: list[Any] = [v for item in limits.items() for v in item]
        else:
            limits_cte = "SELECT NULL AS task, NULL AS cap WHERE 0"
            limit_params = []
        rank = _PRIORITY_RANK_SQL.format(col="r.priority")
        queue_marks = ", ".join("?" for _ in wanted)
        # Positional parameters, in the order they appear: limits, the SET values, the queues,
        # then "now" for available_at.
        sql = f"""
            WITH limits(task, cap) AS ({limits_cte})
            UPDATE {RUNS} SET
                state = 'running',
                attempt = attempt + 1,
                worker_id = ?,
                lease_expires_at = ?,
                started_at = ?,
                cancel_requested = 0,
                error = NULL,
                progress = '{{}}',
                cap_waits = 0,
                slot = CASE WHEN task IN (SELECT l.task FROM limits l)
                    THEN task || char(31) || ifnull(concurrency_key, '') END,
                next_publish_at = NULL,
                updated_at = ?
            WHERE run_id = (
                SELECT r.run_id FROM {RUNS} r
                WHERE r.queue IN ({queue_marks})
                  AND r.state IN {_CLAIMABLE_SQL}
                  AND r.available_at <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM limits l JOIN {COUNTERS} c
                      ON c.name = l.task || char(31) || ifnull(r.concurrency_key, '')
                    WHERE l.task = r.task AND c.running >= l.cap
                  )
                ORDER BY {rank}, r.available_at, r.run_id
                LIMIT 1
            )
            RETURNING {_SELECT_COLS}
        """

        async def run(conn: Any) -> TaskRun | None:
            now = await self._now(conn)
            params = (*limit_params, worker_id, now + lease_ttl, now, now, *wanted, now)
            rows = await conn.execute_fetchall(sql, params)
            if not rows:
                return None
            record = _from_row(next(iter(rows)))
            if record.slot is not None:
                await self._take_slot(conn, record.slot)
            return record

        return await self._write(run)

    async def renew_leases(
        self,
        worker_id: str,
        held: Sequence[HeldLease],
        *,
        lease_ttl: float,
    ) -> dict[str, bool]:
        if not held:
            return {}

        async def run(conn: Any) -> dict[str, bool]:
            now = await self._now(conn)
            out: dict[str, bool] = {}
            for lease in held:
                rows = await conn.execute_fetchall(
                    f"UPDATE {RUNS} SET lease_expires_at = ? WHERE run_id = ? AND attempt = ? "
                    f"AND worker_id = ? AND state IN {_LEASED_SQL} RETURNING cancel_requested",
                    (now + lease_ttl, lease.run_id, lease.attempt, worker_id),
                )
                if rows:
                    out[lease.run_id] = bool(next(iter(rows))[0])
            return out

        return await self._write(run)

    async def append_events(
        self,
        run_id: str,
        *,
        attempt: int,
        worker_id: str,
        events: Sequence[EventIn],
        progress: dict | None = None,
    ) -> bool:
        progress_text = None if progress is None else _dump(progress)

        async def run(conn: Any) -> bool:
            now = await self._now(conn)
            rows = await conn.execute_fetchall(
                f"SELECT event_seq FROM {RUNS} WHERE run_id = ? AND attempt = ? "
                f"AND worker_id = ? AND state IN {_LEASED_SQL}",
                (run_id, attempt, worker_id),
            )
            if not rows:
                return False
            seq = int(next(iter(rows))[0])
            batch = []
            for event in events:
                seq += 1
                batch.append((run_id, seq, attempt, event.type, event.at, _dump(event.data)))
            if batch:
                await conn.executemany(
                    f"INSERT INTO {EVENTS} (run_id, seq, attempt, type, at, data) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    batch,
                )
                await conn.execute(
                    f"DELETE FROM {EVENTS} WHERE run_id = ? AND seq <= ?",
                    (run_id, seq - EVENT_LOG_CAP),
                )
            if progress_text is None:
                await conn.execute(
                    f"UPDATE {RUNS} SET event_seq = ?, updated_at = ? WHERE run_id = ?",
                    (seq, now, run_id),
                )
            else:
                await conn.execute(
                    f"UPDATE {RUNS} SET event_seq = ?, progress = ?, updated_at = ? "
                    "WHERE run_id = ?",
                    (seq, progress_text, now, run_id),
                )
            return True

        return await self._write(run)

    async def transition(
        self,
        run_id: str,
        *,
        attempt: int,
        worker_id: str,
        outcome: AttemptOutcome,
    ) -> TaskRun | None:
        async def run(conn: Any) -> TaskRun | None:
            record = await self._fenced(conn, run_id, attempt, worker_id)
            if record is None:
                return None
            now = await self._now(conn)
            await self._release_slot(conn, record)
            self._clear_lease(record)
            record.updated_at = now
            kind = "cancelled" if record.state == RunState.CANCELLING else outcome.kind
            if kind == "succeeded":
                record.result = outcome.result
                record.error = None
                self._finish(record, RunState.SUCCEEDED, now)
            elif kind == "failed":
                record.error = outcome.error
                record.failure_count += 1
                self._finish(record, RunState.FAILED, now)
            elif kind == "cancelled":
                record.error = outcome.error if outcome.kind == "cancelled" else None
                self._finish(record, RunState.CANCELLED, now)
            elif kind == "retry":
                record.error = outcome.error
                record.failure_count += 1
                record.state = RunState.RETRYING
                record.available_at = now + max(0.0, outcome.delay)
                record.next_publish_at = record.available_at
            elif kind == "defer":
                record.defer_count += 1
                record.state = RunState.QUEUED
                record.available_at = now + max(0.0, outcome.delay)
                record.next_publish_at = record.available_at
            else:  # requeue
                record.error = outcome.error
                record.state = RunState.QUEUED
                record.available_at = now
                record.next_publish_at = now
            await self._save(conn, record)
            return record

        return await self._write(run)

    # ------------------------------------------------------------------ callers

    async def request_cancel(self, run_id: str) -> CancelResult:
        async def run(conn: Any) -> CancelResult:
            record = await self._get(conn, run_id)
            if record is None:
                return CancelResult(status=CancelStatus.NOT_FOUND)
            now = await self._now(conn)
            if record.state in TERMINAL_STATES:
                return CancelResult(status=CancelStatus.TERMINAL, run=record)
            if record.state == RunState.CANCELLING:
                return CancelResult(status=CancelStatus.ALREADY_REQUESTED, run=record)
            record.updated_at = now
            if record.state in CLAIMABLE_STATES:
                self._finish(record, RunState.CANCELLED, now)
                status = CancelStatus.CANCELLED
            else:
                record.state = RunState.CANCELLING
                record.cancel_requested = True
                status = CancelStatus.REQUESTED
            await self._save(conn, record)
            return CancelResult(status=status, run=record)

        return await self._write(run)

    async def read_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> list[TaskEvent]:
        limit = max(1, min(MAX_PAGE, limit))
        rows = await self._read(
            f"SELECT seq, attempt, type, at, data FROM {EVENTS} WHERE run_id = ? AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (run_id, after, limit),
        )
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
        rows = await self._read(
            f"SELECT run_id, attempt FROM {RUNS} WHERE state IN {_LEASED_SQL} "
            f"AND lease_expires_at IS NOT NULL AND lease_expires_at <= {self._clock} "
            "ORDER BY lease_expires_at LIMIT ?",
            (max(1, limit),),
        )
        return [HeldLease(run_id=r["run_id"], attempt=r["attempt"]) for r in rows]

    async def mark_lost(self, run_id: str, *, attempt: int) -> LostResult | None:
        async def run(conn: Any) -> LostResult | None:
            now = await self._now(conn)
            record = await self._get(conn, run_id)
            if (
                record is None
                or record.state not in LEASED_STATES
                or record.attempt != attempt
                or record.lease_expires_at is None
                or record.lease_expires_at > now
            ):
                return None
            await self._release_slot(conn, record)
            self._clear_lease(record)
            record.lost_count += 1
            record.updated_at = now
            if record.state == RunState.CANCELLING:
                self._finish(record, RunState.CANCELLED, now)
            elif record.lost_count > record.max_lost:
                record.error = {
                    "code": "lost",
                    "type": "LeaseLost",
                    "message": (
                        f"worker lost the run {record.lost_count} times "
                        f"(max_lost={record.max_lost})"
                    ),
                }
                self._finish(record, RunState.FAILED, now)
            else:
                record.state = RunState.QUEUED
                record.available_at = now
                record.next_publish_at = now
            await self._save(conn, record)
            return LostResult(run=record)

        return await self._write(run)

    async def due_runs(self, *, limit: int = 100) -> list[TaskRun]:
        rows = await self._read(
            f"SELECT {_SELECT_COLS} FROM {RUNS} WHERE state IN {_CLAIMABLE_SQL} "
            f"AND next_publish_at IS NOT NULL AND next_publish_at <= {self._clock} "
            "ORDER BY next_publish_at, run_id LIMIT ?",
            (max(1, limit),),
        )
        return [_from_row(r) for r in rows]

    async def mark_published(
        self, run_ids: Sequence[str], *, republish_after: float = REPUBLISH_AFTER
    ) -> None:
        ids = list(dict.fromkeys(run_ids))
        if not ids:
            return
        wait = max(0.0, republish_after)

        async def run(conn: Any) -> None:
            now = await self._now(conn)
            for start in range(0, len(ids), IN_CHUNK):
                chunk = ids[start : start + IN_CHUNK]
                await conn.execute(
                    f"UPDATE {RUNS} SET published_at = ?, next_publish_at = ? "
                    f"WHERE state IN {_CLAIMABLE_SQL} "
                    f"AND run_id IN ({', '.join('?' for _ in chunk)})",
                    (now, now + wait, *chunk),
                )

        await self._write(run)

    async def concurrency_count(self, task: str, key: str | None = None) -> int:
        rows = await self._read(
            f"SELECT running FROM {COUNTERS} WHERE name = ?", (slot_name(task, key),)
        )
        return int(rows[0][0]) if rows else 0

    async def recount_concurrency(self) -> None:
        """Serialised with every claim by SQLite's single writer (``BEGIN IMMEDIATE``)."""

        async def run(conn: Any) -> None:
            await conn.execute(f"DELETE FROM {COUNTERS}")
            await conn.execute(
                f"INSERT INTO {COUNTERS} (name, running) SELECT slot, count(*) FROM {RUNS} "
                f"WHERE state IN {_LEASED_SQL} AND slot IS NOT NULL GROUP BY slot"
            )

        await self._write(run)

    async def purge_expired(self, *, limit: int = 1000) -> int:
        async def run(conn: Any) -> int:
            now = await self._now(conn)
            rows = await conn.execute_fetchall(
                f"SELECT run_id FROM {RUNS} WHERE state IN {_TERMINAL_SQL} "
                "AND expires_at IS NOT NULL AND expires_at <= ? LIMIT ?",
                (now, max(1, limit)),
            )
            ids = [r[0] for r in rows]
            for start in range(0, len(ids), IN_CHUNK):
                chunk = ids[start : start + IN_CHUNK]
                marks = ", ".join("?" for _ in chunk)
                await conn.execute(f"DELETE FROM {EVENTS} WHERE run_id IN ({marks})", chunk)
                await conn.execute(f"DELETE FROM {RUNS} WHERE run_id IN ({marks})", chunk)
            return len(ids)

        return await self._write(run)

    # ------------------------------------------------------------------ test introspection

    async def counters(self) -> dict[str, int]:
        """Every counter row (tests check invariants with it)."""
        rows = await self._read(f"SELECT name, running FROM {COUNTERS}")
        return {r["name"]: int(r["running"]) for r in rows}


def _encode_cursor(enqueued_at: float, run_id: str) -> str:
    raw = json.dumps([enqueued_at, run_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        enqueued_at, run_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return float(enqueued_at), str(run_id)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc
