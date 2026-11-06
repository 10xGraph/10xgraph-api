"""The consume side of the Redis Streams transport: one consumer's reads, recovery and buffer."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tenxgraph_api.jobs.core.models import PRIORITIES
from tenxgraph_api.jobs.transport.base import Delivery, TransportMessage, UnknownMessageFormat


logger = logging.getLogger("tenxgraph_api.jobs")

#: Options of the blocking-read client (no socket timeout: a read must not race it).
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
_Found = list[tuple[int, Delivery]]


class Closed(Exception):
    """The transport was closed while a read was in flight."""


def _entries(stream: str, items: Iterable[Any]) -> list[_Entry]:
    return [(stream, entry_id, fields or None) for entry_id, fields in items]


class StreamReader:
    """Reads for one consumer: pending re-read, XAUTOCLAIM, high first, then a blocking read.

    ``client`` runs short commands (XACK, XAUTOCLAIM); ``read_client`` only XREADGROUP;
    ``is_closed`` is the transport's closed flag.
    """

    def __init__(
        self,
        client: Any,
        read_client: Any,
        *,
        group: str,
        consumer: str,
        max_block: float,
        claim_idle: float,
        claim_interval: float,
        is_closed: Callable[[], bool],
    ) -> None:
        self.client = client
        self.read_client = read_client
        self.group = group
        self.consumer = consumer
        self.max_block = max_block
        self.claim_idle = claim_idle
        self.claim_interval = claim_interval
        self.buffer: dict[str, deque[tuple[str, TransportMessage]]] = {}
        self.inflight: set[Receipt] = set()
        #: Re-read this consumer's pending entries (``XREADGROUP ... 0``) on the next read.
        self.recover = True
        self.next_claim = 0.0
        self.backoff = BACKOFF_START
        self._is_closed = is_closed
        self.dropped = 0
        #: XREADGROUP calls in flight (``cancel_reads`` cuts them off on close).
        self.reads: set[asyncio.Task[Any]] = set()

    @property
    def closed(self) -> bool:
        """Whether the transport is closed."""
        return self._is_closed()

    def _live(self) -> None:
        if self.closed:
            raise RuntimeError("transport is closed")

    # ------------------------------------------------------------------ parsing and buffer

    def parse(self, entries: Iterable[_Entry], out: _Found) -> list[Receipt]:
        """Turn raw entries into deliveries in ``out``; return the receipts to ack and drop."""
        bad: list[Receipt] = []
        for stream, entry_id, fields in entries:
            receipt = (stream, entry_id)
            if receipt in self.inflight:
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

    def buffered(self, streams: Iterable[str]) -> set[Receipt]:
        """Receipts of the entries waiting in the local buffer."""
        return {(s, e) for s in streams for e, _ in self.buffer.get(s, ())}

    def parse_new(self, entries: list[_Entry], known: set[Receipt], out: _Found) -> list[Receipt]:
        """``parse`` without the ``known`` (buffered) entries."""
        return self.parse((e for e in entries if (e[0], e[1]) not in known), out)

    def take_buffer(self, streams: Iterable[str], out: _Found) -> None:
        """Move the buffered entries of ``streams`` into ``out``."""
        for stream in streams:
            lane = self.buffer.pop(stream, None)
            while lane:
                entry_id, message = lane.popleft()
                delivery = Delivery(message=message, receipt=(stream, entry_id))
                out.append((_RANK.get(message.priority, 1), delivery))

    # ------------------------------------------------------------------ Redis reads

    async def read(self, streams: dict[str, str], count: int, block: float | None) -> list[_Entry]:
        """One XREADGROUP on the read client, bounded by ``asyncio.timeout(block + grace)``."""
        if not streams or count <= 0:
            return []
        block_ms = None if block is None else max(1, int(block * 1000))
        limit = (block or 0.0) + READ_GRACE
        if self.closed:
            raise Closed
        read = asyncio.ensure_future(
            self.read_client.xreadgroup(
                self.group, self.consumer, streams, count=count, block=block_ms
            )
        )
        self.reads.add(read)
        try:
            async with asyncio.timeout(limit):
                reply = await read
        except TimeoutError as exc:
            raise RedisTimeoutError(f"stream read took longer than {limit:.1f} s") from exc
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if self.closed and read.cancelled() and not (task and task.cancelling()):
                raise Closed from None  # aclose cut this read off; the caller was not cancelled
            raise
        finally:
            self.reads.discard(read)
        return [entry for stream, items in reply or [] for entry in _entries(stream, items)]

    async def autoclaim(
        self, streams: Sequence[str], *, min_idle: float, limit: int | None
    ) -> list[_Entry]:
        """XAUTOCLAIM (full entries, not ``justid``) idle entries of any consumer."""
        self._live()
        entries: list[_Entry] = []
        for stream in streams:
            start = "0-0"
            while limit is None or len(entries) < limit:
                count = 100 if limit is None else max(1, min(100, limit - len(entries)))
                reply = await self.client.xautoclaim(
                    stream, self.group, self.consumer, int(min_idle * 1000),
                    start_id=start, count=count,
                )  # fmt: skip
                start, claimed = reply[0], reply[1]
                for entry_id, _ in claimed:
                    self.inflight.discard((stream, entry_id))
                entries += _entries(stream, claimed)
                if start in ("0-0", b"0-0"):
                    break
        return entries

    async def ack(self, receipts: Sequence[Receipt]) -> None:
        """XACK ``receipts`` in one pipeline, grouped by stream."""
        if not receipts:
            return
        self._live()
        by_stream: dict[str, list[str]] = {}
        for stream, entry_id in receipts:
            by_stream.setdefault(stream, []).append(entry_id)
        async with self.client.pipeline(transaction=False) as pipe:
            for stream, ids in by_stream.items():
                pipe.xack(stream, self.group, *ids)
            await pipe.execute()
        for receipt in receipts:
            self.inflight.discard(receipt)

    async def reclaim(self, streams: Sequence[str], *, min_idle: float) -> int:
        """XAUTOCLAIM every idle entry of ``streams`` into the buffer; returns how many."""
        entries = await self.autoclaim(streams, min_idle=min_idle, limit=None)
        out: _Found = []
        await self.ack(self.parse_new(entries, self.buffered(streams), out))
        for _, delivery in out:
            stream, entry_id = delivery.receipt
            self.buffer.setdefault(stream, deque()).append((entry_id, delivery.message))
        return len(out)

    # ------------------------------------------------------------------ consume

    async def fetch(
        self,
        by_priority: dict[str, list[str]],
        max_messages: int,
        deadline: float,
        *,
        full: bool,
    ) -> list[Delivery]:
        """``collect`` with reconnect and backoff on a failed read (then the error is raised)."""
        try:
            out = await self.collect(by_priority, max_messages, deadline, full=full)
        except ResponseError:
            raise
        except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
            if self.closed:
                raise Closed from exc
            await self.reset()
            loop = asyncio.get_running_loop()
            wait = min(self.backoff, max(0.0, deadline - loop.time()))
            self.backoff = min(BACKOFF_MAX, self.backoff * 2)
            logger.warning("jobs transport read failed (%r); reconnecting", exc)
            if wait > 0:
                await asyncio.sleep(wait)
            raise
        except BaseException:
            # A cancel or any other error may have cut off a read whose entries Redis already
            # delivered to this consumer: re-read our pending entries first next time.
            self.recover = True
            raise
        self.backoff = BACKOFF_START
        return out

    async def collect(
        self, by_priority: dict[str, list[str]], max_messages: int, deadline: float, *, full: bool
    ) -> list[Delivery]:
        """Recover, claim, drain the buffer, read ``high``, then the rest (blocking if empty)."""
        loop = asyncio.get_running_loop()
        every = [s for p in PRIORITIES for s in by_priority[p]]
        found: _Found = []
        bad: list[Receipt] = []
        if self.recover:
            # Entries already delivered to this consumer name but never seen (a read cut off by
            # a timeout, a restart under the same worker id).
            known = self.buffered(every)
            pending = await self.read(dict.fromkeys(every, "0"), max_messages, None)
            bad += self.parse_new(pending, known, found)
            # A read limited to some levels did not look at the other streams' pending entries.
            self.recover = not full
        if loop.time() >= self.next_claim:
            self.next_claim = loop.time() + self.claim_interval
            room = max_messages - len(found)
            if room > 0:
                known = self.buffered(every)
                claimed = await self.autoclaim(every, min_idle=self.claim_idle, limit=room)
                bad += self.parse_new(claimed, known, found)
        self.take_buffer(every, found)
        high_waiting = sum(1 for rank, _ in found if rank == 0)
        high = await self.read(
            dict.fromkeys(by_priority["high"], ">"), max_messages - high_waiting, None
        )
        bad += self.parse(high, found)
        if len(found) < max_messages:
            rest = [s for p in PRIORITIES[1:] for s in by_priority[p]]
            bad += await self._read_rest(rest, every, max_messages, deadline, found)
        await self.ack(bad)
        return self._hand_out(found, max_messages)

    async def _read_rest(
        self, rest: list[str], every: list[str], max_messages: int, deadline: float, found: _Found
    ) -> list[Receipt]:
        """Read the lower levels: at once when something was found, else block until deadline."""
        loop = asyncio.get_running_loop()
        if found:
            entries = await self.read(dict.fromkeys(rest, ">"), max_messages - len(found), None)
            return self.parse(entries, found)
        bad: list[Receipt] = []
        while True:
            remaining = deadline - loop.time()
            if remaining < MIN_BLOCK:
                entries = await self.read(dict.fromkeys(rest, ">"), max_messages, None)
                return bad + self.parse(entries, found)
            entries = await self.read(
                dict.fromkeys(every, ">"), max_messages, min(self.max_block, remaining)
            )
            bad += self.parse(entries, found)
            if found or bad:
                return bad

    def _hand_out(self, found: _Found, max_messages: int) -> list[Delivery]:
        """Highest priority first; the overflow goes back to the front of the buffer."""
        found.sort(key=lambda item: item[0])
        handed = [d for _, d in found[:max_messages]]
        for _, delivery in reversed(found[max_messages:]):
            stream, entry_id = delivery.receipt
            self.buffer.setdefault(stream, deque()).appendleft((entry_id, delivery.message))
        self.inflight.update(d.receipt for d in handed)
        return handed

    # ------------------------------------------------------------------ connection

    async def reset(self) -> None:
        """Drop the read client's connections; the next read reconnects and recovers."""
        self.recover = True
        if self.read_client is not None:
            try:
                await self.read_client.connection_pool.disconnect()
            except Exception:  # best effort; the next read reconnects
                logger.debug("jobs transport: disconnect after a failed read raised", exc_info=True)

    def cancel_reads(self) -> None:
        """Cancel every XREADGROUP in flight (the transport is already closed)."""
        for read in list(self.reads):
            read.cancel()
