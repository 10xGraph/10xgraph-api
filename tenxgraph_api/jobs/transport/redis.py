"""Redis Streams transport.

One stream per (queue, priority), ``<prefix>{tenxgraph-jobs}:stream:<queue>:<priority>``, one
consumer group (``group``), consumer name = the worker id. Streams are trimmed approximately
(``MAXLEN ~ maxlen``); a trimmed message is harmless because the run store republishes the run
(``next_publish_at``).

Two clients (JOBS_SPRINT0.md section 1):

- the read client has ``socket_timeout=None`` (a blocking read must not race the socket
  timeout), ``socket_connect_timeout=5``, keepalive and ``health_check_interval=30``. Every read
  runs inside ``asyncio.timeout(block + 5 s)`` (a paused server keeps the TCP connection open,
  so only this timeout notices) and uses BLOCK of at most ``max_block`` seconds, never 0. A
  failed read drops the read client's connections, waits a backoff (0.1 s doubling to 5 s,
  bounded by the call's ``block``) and raises; the next read reconnects and first re-reads this
  consumer's own pending entries (``XREADGROUP ... 0``), so an entry delivered to a dead socket
  is not left waiting for ``XAUTOCLAIM``.
- the short-command client (``XADD``, ``XACK``, ``XAUTOCLAIM``, ``XGROUP``) keeps
  ``socket_timeout=5``.

``consume`` reads ``high`` alone first (non-blocking, ``COUNT`` = free slots), then one read
over every stream of the queues (blocking when nothing was found). One ``XREADGROUP`` over
several streams applies ``COUNT`` per stream, so entries beyond ``max_messages`` are kept in a
local buffer (still pending in Redis) and handed out first by the next call, in priority
order. Entries idle longer than ``claim_idle`` in any consumer are taken over with
``XAUTOCLAIM`` (without ``justid``) every ``claim_interval``. Messages in a format this code
does not read are acked and dropped (L4). ``aclose`` removes the consumer from each group it
has no pending entries in (``XGROUP DELCONSUMER``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from collections import deque
from collections.abc import Iterable, Sequence
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError, ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tenxgraph_api.jobs.models import PRIORITIES
from tenxgraph_api.jobs.store.base import redis_key
from tenxgraph_api.jobs.store.redis import (
    DEFAULT_JOBS_DB,
    SHORT_CLIENT_OPTIONS,
    JobsRedis,
    check_prefix,
    jobs_client_for,
    open_jobs_redis,
)
from tenxgraph_api.jobs.transport.base import (
    BaseTransport,
    Delivery,
    TransportMessage,
    UnknownMessageFormat,
)


logger = logging.getLogger("tenxgraph_api.jobs")

DEFAULT_GROUP = "tenxgraph-workers"
#: Options of the blocking-read client.
READ_CLIENT_OPTIONS: dict[str, Any] = {
    "socket_timeout": None,
    "socket_connect_timeout": 5.0,
    "socket_keepalive": True,
    "health_check_interval": 30,
    "decode_responses": True,
}
#: Extra time a read may take beyond its BLOCK before it counts as hung.
READ_GRACE = 5.0
BACKOFF_START = 0.1
BACKOFF_MAX = 5.0
#: Longest BLOCK of one read (JOBS_PLAN: 2-5 s, never 0).
MAX_BLOCK = 5.0
#: Remaining wait below which a blocking read is not worth sending.
MIN_BLOCK = 0.001

_RANK = {p: i for i, p in enumerate(PRIORITIES)}

#: (stream key, entry id); the delivery receipt.
Receipt = tuple[str, str]
_Entry = tuple[str, str, dict[str, str] | None]


class _Closed(Exception):
    """The transport was closed while a read was in flight."""


def default_consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class RedisStreamsTransport(BaseTransport):
    """Redis Streams transport. See the module docstring.

    Pass ``url`` (both clients are opened and closed here, with command retries off), or
    ``client`` and ``read_client`` (used as they are, never closed; they must not retry
    commands either, see ``RedisRunStore``: a retried XREADGROUP or XAUTOCLAIM hands entries to
    a reply nobody reads). A supplied client that retries gets one WARNING. ``consumer`` is the worker id (default host-pid-random).
    ``db`` and ``key_prefix`` follow the run store's own-storage rule.
    """

    def __init__(  # noqa: PLR0913 - every option is a keyword
        self,
        url: str | None = None,
        client: Any = None,
        read_client: Any = None,
        *,
        consumer: str | None = None,
        group: str = DEFAULT_GROUP,
        db: int | None = DEFAULT_JOBS_DB,
        key_prefix: str = "",
        maxlen: int = 100_000,
        max_block: float = 5.0,
        claim_idle: float = 60.0,
        claim_interval: float = 5.0,
    ) -> None:
        if url is None and (client is None or read_client is None):
            raise ValueError("RedisStreamsTransport needs a url, or a client and a read_client")
        if not 0 < max_block <= MAX_BLOCK:
            raise ValueError("max_block must be in (0, 5] seconds")
        self._url = url
        self._given = client
        self._given_reader = read_client
        self._db = db
        self._key_prefix = check_prefix(key_prefix)
        self.consumer = consumer or default_consumer_name()
        self.group = group
        self.maxlen = int(maxlen)
        self.max_block = float(max_block)
        self.claim_idle = float(claim_idle)
        self.claim_interval = float(claim_interval)
        self._conn: JobsRedis | None = None
        self._reader: Any = None
        self._owns_reader = False
        self._lock = asyncio.Lock()
        self._groups: set[str] = set()
        self._buffer: dict[str, deque[tuple[str, TransportMessage]]] = {}
        self._inflight: set[Receipt] = set()
        self._recover = True
        self._next_claim = 0.0
        self._backoff = BACKOFF_START
        self._closed = False
        #: XREADGROUP calls in flight (``aclose`` cancels them before leaving the groups).
        self._reads: set[asyncio.Task[Any]] = set()
        #: ``consume`` calls in flight; ``_idle`` is set when there are none.
        self._consuming = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self.dropped = 0

    # ------------------------------------------------------------------ connection

    async def _ready(self) -> Any:
        if self._closed:
            raise RuntimeError("transport is closed")
        if self._conn is None:
            async with self._lock:
                if self._conn is None:
                    conn = await open_jobs_redis(
                        url=self._url,
                        client=self._given,
                        db=self._db,
                        key_prefix=self._key_prefix,
                        options=dict(SHORT_CLIENT_OPTIONS),
                        what="transport",
                    )
                    if self._given_reader is not None:
                        self._reader = self._given_reader
                    elif self._url is not None:
                        self._reader = jobs_client_for(
                            self._url, conn.db, dict(READ_CLIENT_OPTIONS)
                        )
                        self._owns_reader = True
                    self._conn = conn
        return self._conn.client

    @property
    def key_prefix(self) -> str:
        return self._conn.prefix if self._conn else self._key_prefix

    def stream_key(self, queue: str, priority: str) -> str:
        """The stream of ``(queue, priority)``."""
        return self.key_prefix + redis_key("stream", queue, priority)

    async def setup(self) -> None:
        client = await self._ready()
        await client.ping()

    async def _ensure_groups(self, streams: Iterable[str]) -> None:
        missing = [s for s in streams if s not in self._groups]
        if not missing:
            return
        client = await self._ready()
        for stream in missing:
            try:
                await client.xgroup_create(stream, self.group, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            self._groups.add(stream)

    # ------------------------------------------------------------------ publish

    async def publish(self, message: TransportMessage) -> None:
        await self.publish_many([message])

    async def publish_many(self, messages: Sequence[TransportMessage]) -> None:
        if not messages:
            return
        client = await self._ready()
        async with client.pipeline(transaction=False) as pipe:
            for message in messages:
                fields = {k: str(v) for k, v in message.to_dict().items()}
                pipe.xadd(
                    self.stream_key(message.queue, message.priority),
                    fields,
                    maxlen=self.maxlen,
                    approximate=True,
                )
            await pipe.execute()

    async def publish_raw(self, stream: str, fields: dict[str, Any]) -> None:
        """XADD raw fields (tests: messages written by other code versions)."""
        client = await self._ready()
        await client.xadd(stream, {k: str(v) for k, v in fields.items()})

    # ------------------------------------------------------------------ consume

    def _parse(self, entries: Iterable[_Entry], out: list[tuple[int, Delivery]]) -> list[Receipt]:
        """Turn raw entries into deliveries; return the receipts to ack and drop."""
        bad: list[Receipt] = []
        for stream, entry_id, fields in entries:
            receipt = (stream, entry_id)
            if receipt in self._inflight:
                continue
            if not fields:
                bad.append(receipt)  # trimmed or deleted while pending
                continue
            try:
                message = TransportMessage.from_dict(fields)
            except (UnknownMessageFormat, KeyError, TypeError, ValueError) as exc:
                self.dropped += 1
                logger.warning("dropped unreadable jobs message %r: %s", fields, exc)
                bad.append(receipt)
                continue
            out.append((_RANK.get(message.priority, 1), Delivery(message=message, receipt=receipt)))
        return bad

    async def _read(self, streams: dict[str, str], count: int, block: float | None) -> list[_Entry]:
        """One XREADGROUP on the read client, bounded by ``asyncio.timeout``."""
        if not streams or count <= 0:
            return []
        block_ms = None if block is None else max(1, int(block * 1000))
        limit = (block or 0.0) + READ_GRACE
        if self._closed:
            raise _Closed
        read = asyncio.ensure_future(
            self._reader.xreadgroup(self.group, self.consumer, streams, count=count, block=block_ms)
        )
        self._reads.add(read)
        try:
            async with asyncio.timeout(limit):
                reply = await read
        except TimeoutError as exc:
            raise RedisTimeoutError(f"stream read took longer than {limit:.1f} s") from exc
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if self._closed and read.cancelled() and not (task and task.cancelling()):
                raise _Closed from None  # aclose cut this read off; the caller was not cancelled
            raise
        finally:
            self._reads.discard(read)
        entries: list[_Entry] = []
        for stream, items in reply or []:
            for entry_id, fields in items:
                entries.append((stream, entry_id, fields or None))
        return entries

    async def _autoclaim(
        self, streams: Sequence[str], *, min_idle: float, limit: int | None
    ) -> list[_Entry]:
        client = await self._ready()
        entries: list[_Entry] = []
        for stream in streams:
            start = "0-0"
            while limit is None or len(entries) < limit:
                count = 100 if limit is None else max(1, min(100, limit - len(entries)))
                reply = await client.xautoclaim(
                    stream,
                    self.group,
                    self.consumer,
                    int(min_idle * 1000),
                    start_id=start,
                    count=count,
                )
                start, claimed = reply[0], reply[1]
                for entry_id, fields in claimed:
                    self._inflight.discard((stream, entry_id))
                    entries.append((stream, entry_id, fields or None))
                if start in ("0-0", b"0-0"):
                    break
        return entries

    def _take_buffer(self, streams: Iterable[str], out: list[tuple[int, Delivery]]) -> None:
        for stream in streams:
            lane = self._buffer.pop(stream, None)
            while lane:
                entry_id, message = lane.popleft()
                out.append(
                    (
                        _RANK.get(message.priority, 1),
                        Delivery(message=message, receipt=(stream, entry_id)),
                    )
                )

    def _buffered(self, streams: Iterable[str]) -> set[Receipt]:
        return {(s, e) for s in streams for e, _ in self._buffer.get(s, ())}

    async def reclaim(self, *, min_idle: float = 0.0) -> int:
        """XAUTOCLAIM every entry idle at least ``min_idle`` seconds in any consumer of the
        known streams; the next ``consume`` hands them out. Returns how many."""
        streams = sorted(self._groups)
        entries = await self._autoclaim(streams, min_idle=min_idle, limit=None)
        known = self._buffered(streams)
        out: list[tuple[int, Delivery]] = []
        bad = self._parse((e for e in entries if (e[0], e[1]) not in known), out)
        await self._ack_receipts(bad)
        for _, delivery in out:
            stream, entry_id = delivery.receipt
            self._buffer.setdefault(stream, deque()).append((entry_id, delivery.message))
        return len(out)

    async def consume(
        self,
        queues: Sequence[str],
        *,
        max_messages: int,
        block: float = 0.0,
    ) -> list[Delivery]:
        if max_messages <= 0 or self._closed or not queues:
            return []
        self._consuming += 1
        self._idle.clear()
        try:
            return await self._consume(queues, max_messages, block)
        except _Closed:
            return []
        except Exception:
            if self._closed:  # closed while this call was running
                return []
            raise
        finally:
            self._consuming -= 1
            if self._consuming == 0:
                self._idle.set()

    async def _consume(
        self, queues: Sequence[str], max_messages: int, block: float
    ) -> list[Delivery]:
        await self._ready()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, block)
        by_priority = {
            p: [self.stream_key(q, p) for q in dict.fromkeys(queues)] for p in PRIORITIES
        }
        every = [s for p in PRIORITIES for s in by_priority[p]]
        await self._ensure_groups(every)
        try:
            out = await self._collect(by_priority, every, max_messages, deadline)
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                self._groups.difference_update(every)
            raise
        except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
            if self._closed:
                raise _Closed from exc
            await self._reset_reader()
            wait = min(self._backoff, max(0.0, deadline - loop.time()))
            self._backoff = min(BACKOFF_MAX, self._backoff * 2)
            logger.warning("jobs transport read failed (%r); reconnecting", exc)
            if wait > 0:
                await asyncio.sleep(wait)
            raise
        except BaseException:
            # A cancel or any other error may have cut off a read whose entries Redis already
            # delivered to this consumer: re-read our pending entries first next time.
            self._recover = True
            raise
        self._backoff = BACKOFF_START
        return out

    async def _collect(
        self,
        by_priority: dict[str, list[str]],
        every: list[str],
        max_messages: int,
        deadline: float,
    ) -> list[Delivery]:
        loop = asyncio.get_running_loop()
        found: list[tuple[int, Delivery]] = []
        bad: list[Receipt] = []
        if self._recover:
            # After (re)connecting: entries already delivered to this consumer name but never
            # seen (a read cut off by a timeout, a restart under the same worker id).
            known = self._buffered(every)
            pending = await self._read(dict.fromkeys(every, "0"), max_messages, None)
            bad += self._parse((e for e in pending if (e[0], e[1]) not in known), found)
            self._recover = False
        if loop.time() >= self._next_claim:
            self._next_claim = loop.time() + self.claim_interval
            room = max_messages - len(found)
            if room > 0:
                known = self._buffered(every)
                claimed = await self._autoclaim(every, min_idle=self.claim_idle, limit=room)
                bad += self._parse((e for e in claimed if (e[0], e[1]) not in known), found)
        self._take_buffer(every, found)
        high_waiting = sum(1 for rank, _ in found if rank == 0)
        high = await self._read(
            dict.fromkeys(by_priority["high"], ">"), max_messages - high_waiting, None
        )
        bad += self._parse(high, found)
        if len(found) < max_messages:
            rest = [s for p in PRIORITIES[1:] for s in by_priority[p]]
            if found:
                entries = await self._read(
                    dict.fromkeys(rest, ">"), max_messages - len(found), None
                )
                bad += self._parse(entries, found)
            else:
                while True:
                    remaining = deadline - loop.time()
                    if remaining < MIN_BLOCK:
                        entries = await self._read(dict.fromkeys(rest, ">"), max_messages, None)
                        bad += self._parse(entries, found)
                        break
                    entries = await self._read(
                        dict.fromkeys(every, ">"), max_messages, min(self.max_block, remaining)
                    )
                    bad += self._parse(entries, found)
                    if found or bad:
                        break
        await self._ack_receipts(bad)
        found.sort(key=lambda item: item[0])
        handed = [d for _, d in found[:max_messages]]
        for _, delivery in reversed(found[max_messages:]):
            stream, entry_id = delivery.receipt
            self._buffer.setdefault(stream, deque()).appendleft((entry_id, delivery.message))
        self._inflight.update(d.receipt for d in handed)
        return handed

    async def _reset_reader(self) -> None:
        self._recover = True
        if self._reader is not None:
            try:
                await self._reader.connection_pool.disconnect()
            except Exception:  # best effort; the next read reconnects
                logger.debug("jobs transport: disconnect after a failed read raised", exc_info=True)

    # ------------------------------------------------------------------ ack and close

    async def _ack_receipts(self, receipts: Sequence[Receipt]) -> None:
        if not receipts:
            return
        client = await self._ready()
        by_stream: dict[str, list[str]] = {}
        for stream, entry_id in receipts:
            by_stream.setdefault(stream, []).append(entry_id)
        async with client.pipeline(transaction=False) as pipe:
            for stream, ids in by_stream.items():
                pipe.xack(stream, self.group, *ids)
            await pipe.execute()
        for receipt in receipts:
            self._inflight.discard(receipt)

    async def ack(self, deliveries: Sequence[Delivery]) -> None:
        await self._ack_receipts([tuple(d.receipt) for d in deliveries])  # type: ignore[misc]

    async def aclose(self) -> None:
        """Stop reading first, then leave the groups.

        Order matters: a read still in flight could have entries delivered to this consumer
        after the XPENDING check, and DELCONSUMER would then drop them. So: mark closed, cancel
        the in-flight reads and wait for every ``consume`` to return (each returns ``[]``),
        close the read client, and only then XPENDING / DELCONSUMER. Entries a cut-off read
        received are pending under this consumer, so the consumer is kept for XAUTOCLAIM.
        """
        if self._closed:
            return
        self._closed = True
        for read in list(self._reads):
            read.cancel()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=self.max_block + READ_GRACE)
        except TimeoutError:
            logger.warning("jobs transport: a consume call did not return before close")
        conn = self._conn
        reader, self._reader = self._reader, None
        try:
            if reader is not None and self._owns_reader:
                await reader.aclose()
            if conn is not None:
                await self._leave_groups()
        except RedisError as exc:
            logger.warning("jobs transport: could not leave the consumer groups: %r", exc)
        finally:
            self._conn = None
            if conn is not None and conn.owned:
                await conn.client.aclose()

    async def _leave_groups(self) -> None:
        """XGROUP DELCONSUMER on every stream where this consumer has nothing pending (deleting a
        consumer drops its pending entries, so those are left for XAUTOCLAIM)."""
        if self._conn is None:
            return
        client = self._conn.client
        for stream in sorted(self._groups):
            pending = await client.xpending_range(
                stream, self.group, "-", "+", 1, consumername=self.consumer
            )
            if not pending:
                dropped = await client.xgroup_delconsumer(stream, self.group, self.consumer)
                if dropped:
                    logger.warning(
                        "jobs transport: leaving %s dropped %s pending entries of consumer %s "
                        "(their runs are republished by the sweeper)",
                        stream,
                        dropped,
                        self.consumer,
                    )
