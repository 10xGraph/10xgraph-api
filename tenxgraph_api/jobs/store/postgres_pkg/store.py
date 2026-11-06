"""Postgres run store (asyncpg).

Each state change is one transaction: lock the run row (``SELECT ... FOR UPDATE``), apply
``core/transitions.py`` in Python, write the changed fields back fenced on ``attempt``. Set-based
SQL stays where it carries concurrency or speed: the uncapped claim CAS, capped counters,
``SKIP LOCKED`` scans, batched renew and publish marks, recount and purge. Lock order: run row,
then at most one counter row per transaction; a reported deadlock (40P01) is retried.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from tenxgraph_api.jobs.core import serialization
from tenxgraph_api.jobs.core import transitions as tr
from tenxgraph_api.jobs.core.models import (
    REPUBLISH_AFTER,
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
    TaskEvent,
    TaskRun,
)

from ..base import BaseRunStore, published_marks, slot_name
from .queries import NOW, RUN_FIELDS, Queries, to_param, to_run
from .schema import (
    CURRENT_SCHEMA_VERSION,
    JobsSchemaError,
    check_schema_name,
    ensure_schema,
    recorded_version,
    table_names,
)


T = TypeVar("T")

#: Default seconds one statement may run (asyncpg ``command_timeout``; ``pool_config`` wins).
DEFAULT_COMMAND_TIMEOUT = 30.0
#: Default seconds to wait for a pool connection (``pool_config["acquire_timeout"]`` wins).
DEFAULT_ACQUIRE_TIMEOUT = 30.0
#: Runs ``claim_next`` may pick (and skip) before it reports nothing claimable.
CLAIM_NEXT_PICKS = 10
#: Times a claim is retried after Postgres reports a deadlock (SQLSTATE 40P01).
DEADLOCK_RETRIES = 3
DEADLOCK_BACKOFF = 0.01


def _is_deadlock(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "40P01"


class PostgresRunStore(BaseRunStore):
    """Run store in Postgres.

    Pass ``dsn`` (the store owns a pool: ``min_size=1``, ``max_size=5``, ``command_timeout=30``,
    overridden by ``pool_config``) or an asyncpg ``pool`` (never closed by the store). A pool
    connection is awaited at most ``acquire_timeout`` seconds (``pool_config``, default 30,
    None waits forever). ``auto_migrate=False`` makes ``setup()`` refuse old tables.
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
        self._q = Queries(table_names(schema))

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

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[tuple[Any, float]]:
        """A connection in a transaction, with the store clock read inside it."""
        async with self._conn() as conn, conn.transaction():
            yield conn, float(await conn.fetchval(f"SELECT {NOW}"))

    async def _save(self, conn: Any, before: TaskRun, after: TaskRun) -> TaskRun:
        """Write the fields ``after`` changed on the row locked in this transaction."""
        fields = tr.changed(before, after)
        if not fields:
            return after
        row = await conn.fetchrow(
            self._q.update(fields),
            before.run_id,
            before.attempt,
            *(to_param(name, getattr(after, name)) for name in fields),
        )
        if row is None:  # pragma: no cover - the row is locked by the caller
            raise RuntimeError(f"run {before.run_id} changed while locked")
        return to_run(row)

    async def _take_slot(self, conn: Any, slot: str, cap: int) -> bool:
        return cap > 0 and await conn.fetchval(self._q.take_slot, slot, cap) is not None

    @staticmethod
    async def _retry_deadlock(fn: Callable[[], Awaitable[T]]) -> T:
        deadlocks = 0
        while True:
            try:
                return await fn()
            except Exception as exc:
                if not _is_deadlock(exc) or deadlocks >= DEADLOCK_RETRIES:
                    raise
            deadlocks += 1
            await asyncio.sleep(DEADLOCK_BACKOFF * deadlocks)

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
            return float(await conn.fetchval(f"SELECT {NOW}"))

    # ------------------------------------------------------------------ create and read

    async def create_run(self, new: NewRun, *, key_mode: KeyMode = "keep") -> CreateResult:
        import asyncpg  # - optional dependency

        serialization.check_new_run(new)
        key = (new.task, new.owner_user_id, new.tenant, new.idempotency_key)
        for _ in range(5):
            async with self._tx() as (conn, now):
                run = tr.created(new, now)
                try:
                    row = await conn.fetchrow(
                        self._q.insert, *(to_param(n, getattr(run, n)) for n in RUN_FIELDS)
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise ValueError(f"run id {new.run_id} already exists") from exc
                if row is not None:
                    return CreateResult(run=to_run(row), created=True, due=run.available_at <= now)
                row = await conn.fetchrow(self._q.by_key_for_update, *key)
                if row is None:  # the run holding the key was purged meanwhile: try again
                    continue
                existing = to_run(row)
                after = tr.replaced(existing, new, now) if key_mode == "replace" else None
                if after is None:
                    return CreateResult(run=existing, created=False)
                saved = await self._save(conn, existing, after)
                return CreateResult(
                    run=saved, created=False, replaced=True, due=saved.available_at <= now
                )
        raise RuntimeError(f"create_run could not settle the key {new.idempotency_key!r}")

    async def get_run(self, run_id: str) -> TaskRun | None:
        async with self._conn() as conn:
            row = await conn.fetchrow(self._q.get, run_id)
        return to_run(row) if row is not None else None

    async def list_runs(
        self,
        filters: RunFilter | None = None,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> RunPage:
        limit = tr.page_limit(limit)
        f = filters or RunFilter()
        where: list[str] = []
        args: list[Any] = []

        def arg(value: Any) -> str:
            args.append(value)
            return f"${len(args)}"

        for column, value in (("task", f.task), ("queue", f.queue), ("owner_user_id", f.owner_user_id)):
            if value is not None:
                where.append(f"{column} = {arg(value)}")
        if f.state is not None:
            where.append(f"state = ANY({arg(tr.state_values(f.state))}::text[])")
        if cursor:
            enqueued_at, run_id = tr.decode_cursor(cursor)
            where.append(f"(enqueued_at, run_id) < ({arg(enqueued_at)}, {arg(run_id)})")
        sql = self._q.list(where, arg(limit + 1))
        async with self._conn() as conn:
            rows = await conn.fetch(sql, *args)
        items = [to_run(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = tr.encode_cursor(items[-1].enqueued_at, items[-1].run_id)
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
        if concurrency_limit is not None:
            cap = concurrency_limit
            return await self._retry_deadlock(
                lambda: self._claim_capped(run_id, worker_id, float(lease_ttl), cap)
            )
        async with self._conn() as conn:
            row = await conn.fetchrow(self._q.claim, run_id, worker_id, float(lease_ttl))
            if row is not None:
                return ClaimResult(status=ClaimStatus.CLAIMED, run=to_run(row))
            row = await conn.fetchrow(self._q.get, run_id)
        if row is None:
            return ClaimResult(status=ClaimStatus.NOT_FOUND)
        return ClaimResult(status=ClaimStatus.NOT_READY, run=to_run(row))

    async def _claim_capped(
        self, run_id: str, worker_id: str, lease_ttl: float, cap: int
    ) -> ClaimResult:
        """Lock the run row, then take the counter slot (``running < cap``)."""
        async with self._tx() as (conn, now):
            row = await conn.fetchrow(self._q.get_for_update, run_id)
            if row is None:
                return ClaimResult(status=ClaimStatus.NOT_FOUND)
            run = to_run(row)
            if not tr.claimable(run, now):
                return ClaimResult(status=ClaimStatus.NOT_READY, run=run)
            slot = slot_name(run.task, run.concurrency_key)
            if not await self._take_slot(conn, slot, cap):
                return ClaimResult(
                    status=ClaimStatus.CAPPED,
                    run=await self._save(conn, run, tr.capped(run, now)),
                )
            after = tr.claimed(run, now, worker_id=worker_id, lease_ttl=lease_ttl, slot=slot)
            return ClaimResult(status=ClaimStatus.CLAIMED, run=await self._save(conn, run, after))

    async def claim_next(
        self,
        queues: Sequence[str],
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limits: Mapping[str, int] | None = None,
    ) -> TaskRun | None:
        """Pick (``SKIP LOCKED``) and claim; a counter that filled up since the pick ends the
        transaction (one counter lock per transaction) and the next pick runs in a new one."""
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
        """``(True, run or None)`` when settled, ``(False, None)`` to pick again."""
        tasks, caps = list(limits), [int(v) for v in limits.values()]
        async with self._tx() as (conn, now):
            row = await conn.fetchrow(self._q.pick, queues, now, tasks, caps, skipped)
            if row is None:
                return True, None
            run = to_run(row)
            slot = None
            if run.task in limits:
                slot = slot_name(run.task, run.concurrency_key)
                if not await self._take_slot(conn, slot, limits[run.task]):
                    skipped.append(run.run_id)
                    return False, None
            after = tr.claimed(run, now, worker_id=worker_id, lease_ttl=lease_ttl, slot=slot)
            return True, await self._save(conn, run, after)

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
                self._q.renew,
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
        snapshot = None if progress is None else serialization.dumps(progress, field="progress")
        async with self._conn() as conn, conn.transaction():
            end = await conn.fetchval(
                self._q.events_head, run_id, attempt, worker_id, len(events), snapshot
            )
            if end is None:
                return False
            if events:
                kept, keep_from = tr.log_events(int(end) - len(events), attempt, events)
                await conn.execute(
                    self._q.events_insert,
                    run_id,
                    attempt,
                    [e.seq for e in kept],
                    [e.type for e in kept],
                    [float(e.at) for e in kept],
                    [serialization.dumps(e.data, field="event.data") for e in kept],
                )
                if keep_from > 1:
                    await conn.execute(self._q.events_trim, run_id, keep_from)
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
        async with self._tx() as (conn, now):
            row = await conn.fetchrow(self._q.get_for_update, run_id)
            run = None if row is None else to_run(row)
            if run is None or not tr.fenced(run, attempt, worker_id):
                return None
            after = await self._save(conn, run, tr.ended(run, outcome, now))
            if run.slot is not None:
                await conn.execute(self._q.release_slot, run.slot)
            return after

    # ------------------------------------------------------------------ callers

    async def request_cancel(self, run_id: str) -> CancelResult:
        async with self._tx() as (conn, now):
            row = await conn.fetchrow(self._q.get_for_update, run_id)
            if row is None:
                return CancelResult(status=CancelStatus.NOT_FOUND)
            run = to_run(row)
            status, after = tr.cancel(run, now)
            if after is not None:
                run = await self._save(conn, run, after)
            return CancelResult(status=status, run=run)

    async def read_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> list[TaskEvent]:
        async with self._conn() as conn:
            rows = await conn.fetch(self._q.events_read, run_id, after, tr.page_limit(limit))
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
            rows = await conn.fetch(self._q.find_expired, max(1, limit))
        return [HeldLease(run_id=r["run_id"], attempt=r["attempt"]) for r in rows]

    async def mark_lost(self, run_id: str, *, attempt: int) -> LostResult | None:
        async with self._tx() as (conn, now):
            row = await conn.fetchrow(self._q.get_for_update, run_id)
            run = None if row is None else to_run(row)
            if run is None or not tr.lease_expired(run, attempt, now):
                return None
            after = await self._save(conn, run, tr.lost(run, now))
            if run.slot is not None:
                await conn.execute(self._q.release_slot, run.slot)
            return LostResult(run=after)

    async def due_runs(self, *, limit: int = 100) -> list[TaskRun]:
        async with self._conn() as conn:
            rows = await conn.fetch(self._q.due, max(1, limit))
        return [to_run(r) for r in rows]

    async def mark_published(
        self, seen: Sequence[TaskRun], *, republish_after: float = REPUBLISH_AFTER
    ) -> None:
        marks = published_marks(seen)
        if not marks:
            return
        async with self._conn() as conn:
            await conn.execute(
                self._q.published,
                [m[0] for m in marks],
                [m[1] for m in marks],
                [m[2] for m in marks],
                max(0.0, float(republish_after)),
            )

    async def concurrency_count(self, task: str, key: str | None = None) -> int:
        async with self._conn() as conn:
            value = await conn.fetchval(self._q.count, slot_name(task, key))
        return int(value or 0)

    async def recount_concurrency(self) -> None:
        """Locks every counter row first, so the count sees every claim and exit."""
        async with self._conn() as conn, conn.transaction():
            slots = [r["slot"] for r in await conn.fetch(self._q.lock_counters)]
            if slots:
                await conn.execute(self._q.recount, slots)
            await conn.execute(self._q.recount_missing)

    async def purge_expired(self, *, limit: int = 1000) -> int:
        async with self._conn() as conn:
            return int(await conn.fetchval(self._q.purge, max(1, limit)))
