# SQL here is built from module constants (table names, fixed IN lists, "?" marks);
# every value is a bound parameter.
# ruff: noqa: S608
"""SQLite polling transport: the ``local`` backend's wake-up channel.

Lives in the same file as ``SqliteRunStore`` (table ``tenxgraph_task_messages``), so the API
process that enqueues and every local worker process see the same messages. No server, so no
push: ``consume`` polls every ``poll_interval`` (200 ms) until ``block`` runs out; a publish
from the same transport object wakes a waiting ``consume`` at once.

Why a transport at all when the store has ``claim_next``: the worker (``jobs/worker.py``)
consumes a transport and then claims each run, the same loop for every backend. Keeping that
loop for ``local`` means the worker, sweeper and delayed-run republishing behave exactly as in
production. ``claim_next`` remains available for a local fast path later; both go through the
same fenced run row, so mixing them is safe.

Delivery: ``consume`` hides a message for ``visibility_timeout`` seconds (store clock) and
hands out a fresh receipt; ``ack`` deletes it only when the receipt still matches. A message
that is not acked in time becomes visible again (at least once). Messages carry the wire
format version; an unknown version or unreadable body is deleted (acked and dropped) with a
warning, never handed out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tenxgraph_api.jobs.models import PRIORITIES
from tenxgraph_api.jobs.store.sqlite import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DEFAULT_LOCK_RETRIES,
    DEFAULT_PATH,
    check_sqlite_version,
    clock_sql,
    migrate,
    open_connection,
    write_tx,
)
from tenxgraph_api.jobs.transport.base import (
    BaseTransport,
    Delivery,
    TransportMessage,
    UnknownMessageFormat,
)


logger = logging.getLogger("tenxgraph_api.jobs")

MESSAGES = "tenxgraph_task_messages"
#: Poll period while ``consume`` waits (JOBS_PLAN: 200 ms polling).
DEFAULT_POLL_INTERVAL = 0.2
#: Seconds a delivered, unacked message stays hidden before it is delivered again.
DEFAULT_VISIBILITY_TIMEOUT = 60.0

TRANSPORT_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        f"""CREATE TABLE {MESSAGES} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue TEXT NOT NULL,
            rank INTEGER NOT NULL,
            body TEXT NOT NULL,
            visible_at REAL NOT NULL DEFAULT 0,
            receipt TEXT,
            deliveries INTEGER NOT NULL DEFAULT 0
        )""",
        f"CREATE INDEX {MESSAGES}_ready ON {MESSAGES} (queue, visible_at, rank, id)",
    ),
)

_RANK = {p: i for i, p in enumerate(PRIORITIES)}


def _rank(priority: Any) -> int:
    return _RANK.get(str(priority), _RANK["normal"])


class SqliteTransport(BaseTransport):
    """Polling transport over one SQLite file. See the module docstring."""

    def __init__(
        self,
        path: str | Path = DEFAULT_PATH,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        visibility_timeout: float = DEFAULT_VISIBILITY_TIMEOUT,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        lock_retries: int = DEFAULT_LOCK_RETRIES,
    ) -> None:
        check_sqlite_version()
        self.path = path
        self.poll_interval = max(0.01, poll_interval)
        self.visibility_timeout = max(0.0, visibility_timeout)
        self.busy_timeout_ms = busy_timeout_ms
        self.lock_retries = lock_retries
        self._clock = clock_sql()
        self._conn: Any = None
        self._lock = asyncio.Lock()
        self._open_lock = asyncio.Lock()
        self._ready = False
        self._closed = False
        self._wakeup = asyncio.Event()
        self.dropped = 0

    # ------------------------------------------------------------------ connection

    async def _connection(self) -> Any:
        if self._ready and self._conn is not None:
            return self._conn
        async with self._open_lock:
            if self._closed:
                raise RuntimeError("SqliteTransport is closed")
            if self._conn is None:
                self._conn = await open_connection(self.path, busy_timeout_ms=self.busy_timeout_ms)
            if not self._ready:
                await migrate(
                    self._conn,
                    self._lock,
                    "transport",
                    TRANSPORT_MIGRATIONS,
                    retries=self.lock_retries,
                )
                self._ready = True
        return self._conn

    async def _write(self, fn: Any) -> Any:
        conn = await self._connection()
        return await write_tx(conn, self._lock, fn, retries=self.lock_retries)

    async def setup(self) -> None:
        await self._connection()

    async def aclose(self) -> None:
        async with self._open_lock:
            self._closed = True
            conn, self._conn = self._conn, None
            self._ready = False
        self._wakeup.set()
        if conn is not None:
            async with self._lock:
                await conn.close()

    # ------------------------------------------------------------------ publish

    async def _insert(self, wires: Sequence[dict[str, Any]]) -> None:
        if self._closed:
            raise RuntimeError("transport is closed")
        rows = [(str(w.get("queue", "default")), _rank(w.get("p")), json.dumps(w)) for w in wires]
        if not rows:
            return

        async def run(conn: Any) -> None:
            await conn.executemany(
                f"INSERT INTO {MESSAGES} (queue, rank, body) VALUES (?, ?, ?)", rows
            )

        await self._write(run)
        self._wakeup.set()

    async def publish(self, message: TransportMessage) -> None:
        await self._insert([message.to_dict()])

    async def publish_many(self, messages: Sequence[TransportMessage]) -> None:
        await self._insert([m.to_dict() for m in messages])

    async def publish_raw(self, wire: dict[str, Any]) -> None:
        """Put a raw wire dict on the transport (tests: messages from other code versions)."""
        await self._insert([dict(wire)])

    # ------------------------------------------------------------------ consume and ack

    async def _take(self, queues: Sequence[str], max_messages: int) -> list[Delivery]:
        wanted = list(dict.fromkeys(queues))
        marks = ", ".join("?" for _ in wanted)

        async def run(conn: Any) -> list[Delivery]:
            rows = await conn.execute_fetchall(f"SELECT {self._clock}")
            now = float(next(iter(rows))[0])
            out: list[Delivery] = []
            while len(out) < max_messages:
                rows = await conn.execute_fetchall(
                    f"SELECT id, body FROM {MESSAGES} WHERE queue IN ({marks}) "
                    "AND visible_at <= ? ORDER BY rank, id LIMIT ?",
                    (*wanted, now, max_messages - len(out)),
                )
                if not rows:
                    break
                for row in rows:
                    msg_id, body = int(row[0]), row[1]
                    try:
                        wire = json.loads(body)
                        if not isinstance(wire, dict):
                            raise TypeError("message body is not an object")
                        message = TransportMessage.from_dict(wire)
                    except (UnknownMessageFormat, KeyError, TypeError, ValueError) as exc:
                        # Acked and dropped: the store republishes the run (next_publish_at).
                        await conn.execute(f"DELETE FROM {MESSAGES} WHERE id = ?", (msg_id,))
                        self.dropped += 1
                        logger.warning("dropped unreadable jobs message %r: %s", body, exc)
                        continue
                    receipt = f"{msg_id}:{uuid.uuid4().hex}"
                    await conn.execute(
                        f"UPDATE {MESSAGES} SET visible_at = ?, receipt = ?, "
                        "deliveries = deliveries + 1 WHERE id = ?",
                        (now + self.visibility_timeout, receipt, msg_id),
                    )
                    out.append(Delivery(message=message, receipt=receipt))
            return out

        result: list[Delivery] = await self._write(run)
        return result

    async def consume(
        self,
        queues: Sequence[str],
        *,
        max_messages: int,
        block: float = 0.0,
    ) -> list[Delivery]:
        if max_messages <= 0 or self._closed or not queues:
            return []
        deadline = time.monotonic() + max(0.0, block)
        while True:
            self._wakeup.clear()
            out = await self._take(queues, max_messages)
            remaining = deadline - time.monotonic()
            if out or remaining <= 0 or self._closed:
                return out
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wakeup.wait(), timeout=min(self.poll_interval, remaining)
                )

    async def ack(self, deliveries: Sequence[Delivery]) -> None:
        receipts = [str(d.receipt) for d in deliveries]
        if not receipts:
            return
        pairs = []
        for receipt in receipts:
            msg_id, _, _token = receipt.partition(":")
            if msg_id.isdigit():
                pairs.append((int(msg_id), receipt))

        async def run(conn: Any) -> None:
            await conn.executemany(f"DELETE FROM {MESSAGES} WHERE id = ? AND receipt = ?", pairs)

        if pairs:
            await self._write(run)

    # ------------------------------------------------------------------ test / ops helpers

    async def requeue_unacked(self) -> int:
        """Make every delivered, unacked message visible again now (tests; the stand-in for
        waiting out ``visibility_timeout``). Returns how many."""

        async def run(conn: Any) -> int:
            cursor = await conn.execute(
                f"UPDATE {MESSAGES} SET visible_at = 0, receipt = NULL WHERE receipt IS NOT NULL"
            )
            return int(cursor.rowcount)

        count: int = await self._write(run)
        if count:
            self._wakeup.set()
        return count

    async def size(self) -> int:
        """Messages in the table (waiting or delivered and unacked)."""
        conn = await self._connection()
        async with self._lock:
            rows = await conn.execute_fetchall(f"SELECT count(*) FROM {MESSAGES}")
        return int(next(iter(rows))[0])
