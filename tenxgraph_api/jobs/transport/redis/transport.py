"""Redis Streams transport: one stream per (queue, priority), one consumer group.

Design notes (two clients, read limits, recovery, close order): JOBS_DESIGN.md, "Redis backend".
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from redis.exceptions import RedisError, ResponseError

from tenxgraph_api.jobs.core.models import PRIORITIES
from tenxgraph_api.jobs.store.base import redis_key
from tenxgraph_api.jobs.store.redis.connection import (
    DEFAULT_JOBS_DB,
    SHORT_CLIENT_OPTIONS,
    JobsRedis,
    check_prefix,
    jobs_client_for,
    open_jobs_redis,
)
from tenxgraph_api.jobs.transport.base import BaseTransport, Delivery, TransportMessage

from .reader import MAX_BLOCK, READ_CLIENT_OPTIONS, READ_GRACE, Closed, Receipt, StreamReader


logger = logging.getLogger("tenxgraph_api.jobs")

DEFAULT_GROUP = "tenxgraph-workers"


def default_consumer_name() -> str:
    """``host-pid-random``: unique per worker process."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class RedisStreamsTransport(BaseTransport):
    """Redis Streams transport.

    Pass ``url`` (both clients opened and closed here, retries off) or ``client`` and
    ``read_client`` (used as is, never closed, must not retry). ``consumer`` is the worker id.
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
        self._streams: StreamReader | None = None
        self._owns_reader = False
        self._lock = asyncio.Lock()
        self._groups: set[str] = set()
        self._closed = False
        #: ``consume`` calls in flight; ``_idle`` is set when there are none.
        self._consuming = 0
        self._idle = asyncio.Event()
        self._idle.set()

    # ------------------------------------------------------------------ connection

    async def _ready(self) -> StreamReader:
        """Open both clients once; return the reader."""
        if self._closed:
            raise RuntimeError("transport is closed")
        if self._streams is None:
            async with self._lock:
                if self._streams is None:
                    conn = await open_jobs_redis(
                        url=self._url,
                        client=self._given,
                        db=self._db,
                        key_prefix=self._key_prefix,
                        options=dict(SHORT_CLIENT_OPTIONS),
                        what="transport",
                    )
                    read_client = self._given_reader
                    if read_client is None and self._url is not None:
                        read_client = jobs_client_for(self._url, conn.db, dict(READ_CLIENT_OPTIONS))
                        self._owns_reader = True
                    self._streams = StreamReader(
                        conn.client,
                        read_client,
                        group=self.group,
                        consumer=self.consumer,
                        max_block=self.max_block,
                        claim_idle=self.claim_idle,
                        claim_interval=self.claim_interval,
                        is_closed=lambda: self._closed,
                    )
                    self._conn = conn
        return self._streams

    @property
    def _reader(self) -> Any:
        """The blocking-read client (None before setup and after close)."""
        return self._streams.read_client if self._streams else None

    @property
    def _recover(self) -> bool:
        """Whether the next read first re-reads this consumer's pending entries."""
        return self._streams.recover if self._streams else True

    @property
    def dropped(self) -> int:
        """Messages dropped because their format could not be read."""
        return self._streams.dropped if self._streams else 0

    @property
    def key_prefix(self) -> str:
        """The prefix in front of the hash tag (after setup it includes any fallback prefix)."""
        return self._conn.prefix if self._conn else self._key_prefix

    def stream_key(self, queue: str, priority: str) -> str:
        """The stream of ``(queue, priority)``."""
        return self.key_prefix + redis_key("stream", queue, priority)

    async def setup(self) -> None:
        """Connect and ping."""
        await (await self._ready()).client.ping()

    async def _ensure_groups(self, streams: Iterable[str]) -> None:
        """XGROUP CREATE (MKSTREAM) on every stream not seen yet; BUSYGROUP is fine."""
        missing = [s for s in streams if s not in self._groups]
        if not missing:
            return
        client = (await self._ready()).client
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
        client = (await self._ready()).client
        async with client.pipeline(transaction=False) as pipe:
            for message in messages:
                pipe.xadd(
                    self.stream_key(message.queue, message.priority),
                    {k: str(v) for k, v in message.to_dict().items()},
                    maxlen=self.maxlen,
                    approximate=True,
                )
            await pipe.execute()

    async def publish_raw(self, stream: str, fields: dict[str, Any]) -> None:
        """XADD raw fields (tests: messages written by other code versions)."""
        client = (await self._ready()).client
        await client.xadd(stream, {k: str(v) for k, v in fields.items()})

    # ------------------------------------------------------------------ consume and ack

    async def reclaim(self, *, min_idle: float = 0.0) -> int:
        """XAUTOCLAIM entries idle ``min_idle`` s in any consumer, for the next ``consume``."""
        return await (await self._ready()).reclaim(sorted(self._groups), min_idle=min_idle)

    async def consume(
        self,
        queues: Sequence[str],
        *,
        max_messages: int,
        block: float = 0.0,
        priorities: Sequence[str] | None = None,
    ) -> list[Delivery]:
        if max_messages <= 0 or self._closed or not queues:
            return []
        levels = [p for p in PRIORITIES if priorities is None or p in priorities]
        if not levels:
            return []
        self._consuming += 1
        self._idle.clear()
        try:
            return await self._consume(queues, max_messages, block, levels)
        except Closed:
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
        self, queues: Sequence[str], max_messages: int, block: float, levels: Sequence[str]
    ) -> list[Delivery]:
        """Create missing groups, then read; a NOGROUP reply forgets the groups."""
        reader = await self._ready()
        deadline = asyncio.get_running_loop().time() + max(0.0, block)
        # Levels not asked for get no streams: nothing of theirs is read, acked or buffered.
        by_priority = {
            p: [self.stream_key(q, p) for q in dict.fromkeys(queues)] if p in levels else []
            for p in PRIORITIES
        }
        every = [s for p in PRIORITIES for s in by_priority[p]]
        await self._ensure_groups(every)
        try:
            return await reader.fetch(
                by_priority, max_messages, deadline, full=len(levels) == len(PRIORITIES)
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                self._groups.difference_update(every)
            raise

    async def ack(self, deliveries: Sequence[Delivery]) -> None:
        receipts: list[Receipt] = [tuple(d.receipt) for d in deliveries]  # type: ignore[misc]
        if receipts:
            await (await self._ready()).ack(receipts)

    # ------------------------------------------------------------------ close

    async def aclose(self) -> None:
        """Stop reading (cancel reads, wait for ``consume``), close the read client, leave groups.

        Leaving last means no entry is delivered to this consumer after its XPENDING check.
        """
        if self._closed:
            return
        self._closed = True
        streams = self._streams
        if streams is not None:
            streams.cancel_reads()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=self.max_block + READ_GRACE)
        except TimeoutError:
            logger.warning("jobs transport: a consume call did not return before close")
        conn = self._conn
        try:
            if streams is not None and self._owns_reader:
                reader, streams.read_client = streams.read_client, None
                await reader.aclose()
            if conn is not None:
                await self._leave_groups(conn.client)
        except RedisError as exc:
            logger.warning("jobs transport: could not leave the consumer groups: %r", exc)
        finally:
            self._conn = None
            if streams is not None:
                streams.read_client = None
            if conn is not None and conn.owned:
                await conn.client.aclose()

    async def _leave_groups(self, client: Any) -> None:
        """XGROUP DELCONSUMER where this consumer has nothing pending (left for XAUTOCLAIM)."""
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
