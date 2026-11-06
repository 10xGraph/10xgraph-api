"""SQLite jobs file: opening it, the write transaction and migrations.

Shared with ``transport/sqlite.py`` so both open the file the same way: ``journal_mode=WAL``,
``synchronous=NORMAL``, ``busy_timeout``. Every write is one ``BEGIN IMMEDIATE`` transaction
(``write_tx``), retried with backoff while the database is locked. Schema versions live in
``tenxgraph_task_schema`` (one row per component); a file from a newer version is refused.
Requires SQLite 3.35+ (``RETURNING``) and ``aiosqlite`` (the ``jobs`` extra).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar


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

RUNS = "tenxgraph_task_runs"
EVENTS = "tenxgraph_task_events"
COUNTERS = "tenxgraph_task_counters"
SCHEMA = "tenxgraph_task_schema"


class SqliteVersionError(RuntimeError):
    """The SQLite library linked into Python is too old for the jobs store."""


class SqliteSchemaError(RuntimeError):
    """The jobs database was written by a newer version of 10xgraph-api."""


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


#: Write units still running after their caller was cancelled (kept so they are not
#: garbage collected before they release the write lock).
_UNITS: set[asyncio.Future[Any]] = set()


def _settle(unit: asyncio.Future[Any]) -> None:
    _UNITS.discard(unit)
    if not unit.cancelled():
        unit.exception()  # retrieved: a unit whose caller left must not log "never retrieved"


async def write_tx(
    conn: Any,
    lock: asyncio.Lock,
    fn: Callable[[Any], Awaitable[T]],
    *,
    retries: int,
    backoff: float = 0.02,
) -> T:
    """Run ``fn(conn)`` in one ``BEGIN IMMEDIATE`` transaction and commit; a lock error rolls
    back and runs it again (at most ``retries`` times). Cancel-safe: the unit runs on its own
    task and a cancelled caller waits for its rollback (JOBS_DESIGN.md, "SQL stores").
    """
    stop = asyncio.Event()
    unit = asyncio.ensure_future(
        _write_unit(conn, lock, fn, stop, retries=retries, backoff=backoff)
    )
    _UNITS.add(unit)
    unit.add_done_callback(_settle)
    try:
        return await asyncio.shield(unit)
    except asyncio.CancelledError:
        if unit.done():
            raise
        stop.set()
        with contextlib.suppress(BaseException):
            await asyncio.shield(unit)
        raise


class _Stopped(Exception):
    """The caller of a write unit was cancelled; the unit rolled back."""


async def _write_unit(
    conn: Any,
    lock: asyncio.Lock,
    fn: Callable[[Any], Awaitable[T]],
    stop: asyncio.Event,
    *,
    retries: int,
    backoff: float,
) -> T:
    attempt = 0
    while True:
        async with lock:
            try:
                if conn.in_transaction:  # left open by an interrupted call: undo it
                    await conn.execute("ROLLBACK")
                if stop.is_set():
                    raise _Stopped
                await conn.execute("BEGIN IMMEDIATE")
                if stop.is_set():
                    raise _Stopped
                result = await fn(conn)
                if stop.is_set():
                    raise _Stopped
                await conn.execute("COMMIT")
                return result
            except BaseException as exc:
                await _rollback(conn)
                if stop.is_set() or not is_lock_error(exc) or attempt >= retries:
                    raise
        attempt += 1
        delay = min(1.0, backoff * 2**attempt)
        logger.debug("jobs sqlite database is locked; retry %d in %.2f s", attempt, delay)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        if stop.is_set():
            raise _Stopped


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


