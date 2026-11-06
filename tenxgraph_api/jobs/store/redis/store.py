"""Redis run store: every state change is one Lua script (see ``scripts/``)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from tenxgraph_api.jobs.core import serialization
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
from tenxgraph_api.jobs.store.base import BaseRunStore, published_marks, slot_name

from . import scripts
from .codec import (
    US,
    decode_cursor,
    decode_run,
    decode_runs,
    encode_cursor,
    new_run_fields,
    store_json,
    us,
)
from .connection import (
    DEFAULT_JOBS_DB,
    SHORT_CLIENT_OPTIONS,
    JobsRedis,
    check_prefix,
    open_jobs_redis,
)
from .keys import full_key, idem_field, index_parts, key_base


MAX_PAGE = 1000
#: Expired runs one purge script deletes (short scripts keep Redis responsive).
PURGE_BATCH = 100
#: Most index entries one ``list_runs`` call scans before returning a short page.
LIST_SCAN_BUDGET = 10_000


def _flag(value: object) -> str:
    return "0" if value is None else "1"


class RedisRunStore(BaseRunStore):
    """Run store on Redis 7+ (also Valkey); key layout in ``keys.py``.

    Pass ``url`` (own client, closed here) or ``client`` (used as is, never closed). Neither
    may retry commands: a replayed script could claim or transition twice. ``db`` is the jobs
    database, ``key_prefix`` goes in front of every key, ``client_options`` override
    ``SHORT_CLIENT_OPTIONS``.
    """

    def __init__(
        self,
        url: str | None = None,
        client: Any = None,
        db: int | None = DEFAULT_JOBS_DB,
        *,
        key_prefix: str = "",
        **client_options: Any,
    ) -> None:
        if url is None and client is None:
            raise ValueError("RedisRunStore needs a url or a client")
        self._url = url
        self._given = client
        self._db = db
        self._key_prefix = check_prefix(key_prefix)
        self._options = {**SHORT_CLIENT_OPTIONS, **client_options}
        self._conn: JobsRedis | None = None
        self._scripts: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------ connection

    @property
    def key_prefix(self) -> str:
        """The prefix in front of the hash tag (after setup it includes any fallback prefix)."""
        return self._conn.prefix if self._conn else self._key_prefix

    @property
    def db(self) -> int | None:
        """The database in use (None before setup)."""
        return self._conn.db if self._conn else None

    @property
    def client(self) -> Any:
        """The connected client; raises before setup."""
        if self._conn is None:
            raise RuntimeError("RedisRunStore.setup() has not run")
        return self._conn.client

    def key(self, *parts: str) -> str:
        """A full key of this store."""
        return full_key(self.key_prefix, *parts)

    async def _ready(self) -> Any:
        """Connect and register the scripts once; return the client."""
        if self._closed:
            raise RuntimeError("RedisRunStore is closed")
        if self._conn is None:
            async with self._lock:
                if self._conn is None:
                    conn = await open_jobs_redis(
                        url=self._url,
                        client=self._given,
                        db=self._db,
                        key_prefix=self._key_prefix,
                        options=self._options,
                        what="run store",
                    )
                    self._scripts = {
                        name: conn.client.register_script(source)
                        for name, source in scripts.sources().items()
                    }
                    self._conn = conn
        return self._conn.client

    async def _run(self, name: str, key: str, *args: Any) -> Any:
        """Run script ``name`` with ``KEYS[1] = key`` and ``ARGV = [base, *args]``."""
        await self._ready()
        return await self._scripts[name](keys=[key], args=[key_base(self.key_prefix), *args])

    async def setup(self) -> None:
        """Connect and ping."""
        client = await self._ready()
        await client.ping()

    async def aclose(self) -> None:
        """Close an owned client; a supplied one is left open."""
        if self._closed:
            return
        self._closed = True
        conn, self._conn = self._conn, None
        if conn is not None and conn.owned:
            await conn.client.aclose()

    async def now(self) -> float:
        """The store clock (Redis ``TIME``)."""
        client = await self._ready()
        seconds, micros = await client.time()
        return (int(seconds) * US + int(micros)) / US

    # ------------------------------------------------------------------ create and read

    async def create_run(self, new: NewRun, *, key_mode: KeyMode = "keep") -> CreateResult:
        fields = new_run_fields(new)
        reply = await self._run(
            "create",
            self.key("run", new.run_id),
            new.run_id,
            idem_field(new),
            key_mode,
            "" if new.at is None else str(us(new.at)),
            str(us(new.delay or 0.0)),
            fields["kwargs"],
            _flag(new.concurrency_key),
            new.concurrency_key or "",
            new.task,
            new.owner_user_id or "",
            _flag(new.owner_user_id),
            *[item for pair in fields.items() for item in pair],
        )
        status = reply[0]
        if status == "duplicate":
            raise ValueError(f"run id {new.run_id} already exists")
        run = decode_run(reply[2])
        if run is None:  # pragma: no cover - the script always returns the hash
            raise RuntimeError(f"run {new.run_id} vanished during create")
        return CreateResult(
            run=run, created=status == "created", replaced=status == "replaced", due=reply[1] == "1"
        )

    async def get_run(self, run_id: str) -> TaskRun | None:
        client = await self._ready()
        return decode_run(await client.hgetall(self.key("run", run_id)))

    async def list_runs(
        self,
        filters: RunFilter | None = None,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> RunPage:
        limit = max(1, min(MAX_PAGE, limit))
        after = decode_cursor(cursor) if cursor else None
        f = filters or RunFilter()
        states = None
        if f.state is not None:
            states = sorted(
                {str(s) for s in (f.state if isinstance(f.state, tuple) else (f.state,))}
            )
        spec = {"states": states, "task": f.task, "queue": f.queue, "owner": f.owner_user_id}
        reply = await self._run(
            "list",
            self.key(*index_parts(f, states)),
            json.dumps({k: v for k, v in spec.items() if v is not None}, separators=(",", ":")),
            limit,
            "" if after is None else str(after[0]),
            "" if after is None else after[1],
            LIST_SCAN_BUDGET,
        )
        runs = decode_runs(reply[1])
        next_cursor = None
        if reply[0] == "more":
            runs = runs[:limit]
            next_cursor = encode_cursor(us(runs[-1].enqueued_at), runs[-1].run_id)
        elif reply[0] == "budget":
            next_cursor = encode_cursor(int(float(reply[2])), reply[3])
        return RunPage(items=runs, next_cursor=next_cursor)

    # ------------------------------------------------------------------ worker writes

    async def claim(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limit: int | None = None,
    ) -> ClaimResult:
        reply = await self._run(
            "claim",
            self.key("run", run_id),
            run_id,
            worker_id,
            str(us(lease_ttl)),
            "" if concurrency_limit is None else str(int(concurrency_limit)),
        )
        run = decode_run(reply[1]) if len(reply) > 1 else None
        return ClaimResult(status=ClaimStatus(reply[0]), run=run)

    async def renew_leases(
        self, worker_id: str, held: Sequence[HeldLease], *, lease_ttl: float
    ) -> dict[str, bool]:
        if not held:
            return {}
        args = [item for lease in held for item in (lease.run_id, str(lease.attempt))]
        reply = await self._run("renew", self.key("leases"), worker_id, str(us(lease_ttl)), *args)
        return {reply[i]: reply[i + 1] == "1" for i in range(0, len(reply), 2)}

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
        encoded = [
            store_json({"attempt": attempt, "type": e.type, "at": e.at, "data": e.data}, "event")
            for e in events
        ]
        reply = await self._run(
            "append",
            self.key("run", run_id),
            run_id,
            str(attempt),
            worker_id,
            _flag(progress),
            "" if progress is None else store_json(progress, "progress"),
            *encoded,
        )
        return bool(int(reply))

    async def transition(
        self, run_id: str, *, attempt: int, worker_id: str, outcome: AttemptOutcome
    ) -> TaskRun | None:
        reply = await self._run(
            "transition",
            self.key("run", run_id),
            run_id,
            str(attempt),
            worker_id,
            outcome.kind,
            store_json(outcome.result, "result"),
            "" if outcome.error is None else store_json(outcome.error, "error"),
            str(us(max(0.0, outcome.delay))),
        )
        return decode_run(reply) if reply else None

    # ------------------------------------------------------------------ callers

    async def request_cancel(self, run_id: str) -> CancelResult:
        reply = await self._run("cancel", self.key("run", run_id), run_id)
        run = decode_run(reply[1]) if len(reply) > 1 else None
        return CancelResult(status=CancelStatus(reply[0]), run=run)

    async def read_events(
        self, run_id: str, *, after: int = 0, limit: int = 100
    ) -> list[TaskEvent]:
        limit = max(1, min(MAX_PAGE, limit))
        client = await self._ready()
        start = f"{max(0, int(after)) + 1}-0"
        entries = await client.xrange(self.key("events", run_id), start, "+", count=limit)
        return [
            TaskEvent(seq=int(entry_id.split("-", 1)[0]), **json.loads(fields["e"]))
            for entry_id, fields in entries
        ]

    # ------------------------------------------------------------------ sweeper

    async def find_expired_leases(self, *, limit: int = 100) -> list[HeldLease]:
        reply = await self._run("find_expired", self.key("leases"), max(1, limit))
        return [
            HeldLease(run_id=reply[i], attempt=int(reply[i + 1])) for i in range(0, len(reply), 2)
        ]

    async def mark_lost(self, run_id: str, *, attempt: int) -> LostResult | None:
        reply = await self._run("mark_lost", self.key("run", run_id), run_id, str(attempt))
        run = decode_run(reply) if reply else None
        return LostResult(run=run) if run is not None else None

    async def due_runs(self, *, limit: int = 100) -> list[TaskRun]:
        return decode_runs(await self._run("due", self.key("due"), max(1, limit)))

    async def mark_published(
        self, seen: Sequence[TaskRun], *, republish_after: float = REPUBLISH_AFTER
    ) -> None:
        marks = published_marks(seen)
        if not marks:
            return
        args = [item for rid, attempt, at in marks for item in (rid, str(attempt), str(us(at)))]
        await self._run(
            "mark_published", self.key("due"), str(us(max(0.0, republish_after))), *args
        )

    async def concurrency_count(self, task: str, key: str | None = None) -> int:
        client = await self._ready()
        return int(await client.hget(self.key("counters"), slot_name(task, key)) or 0)

    async def recount_concurrency(self) -> None:
        await self._run("recount", self.key("counters"))

    async def purge_expired(self, *, limit: int = 1000) -> int:
        """In scripts of at most ``PURGE_BATCH`` runs, until ``limit`` or nothing is due."""
        left, deleted = max(1, limit), 0
        while left > 0:
            batch = min(PURGE_BATCH, left)
            n, scanned = await self._run("purge", self.key("expires"), batch)
            deleted += int(n)
            left -= int(scanned)
            if int(scanned) < batch:
                break
        return deleted
