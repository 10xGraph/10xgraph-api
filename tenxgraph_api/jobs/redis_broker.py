"""Redis stream broker that reclaims on an idle queue and renews leases of running tasks.

Two changes over taskiq-redis ``RedisStreamBroker``:

1. ``listen()`` runs ``XAUTOCLAIM`` after every read, also when the read returned nothing.
   Upstream skips it on an empty read, so a dead worker's task waits until new work arrives.
2. Every message this worker holds (fetched, not yet acked) has its idle time reset every
   ``idle_timeout / 3`` by one background renewer task. A running task is therefore never
   claimed by another worker, however long it runs, while a dead worker's messages go idle
   and are claimed after ``idle_timeout``. The renewer stops on ``shutdown()``.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from taskiq import AckableMessage
from taskiq_redis import RedisStreamBroker


logger = logging.getLogger("tenxgraph_api.jobs.redis_broker")

# Reset the idle time of one pending entry, only while this consumer still owns it.
# An acked entry, or one another worker has claimed, is left alone (returns 0).
RENEW_SCRIPT = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1, ARGV[2])
if #pending == 0 then return 0 end
redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[2], 0, ARGV[3], 'JUSTID')
return 1
"""

Lease = tuple[Any, Any]  # (stream name, message id) as returned by Redis


class ReclaimingRedisStreamBroker(RedisStreamBroker):
    """``RedisStreamBroker`` with idle-queue reclaim and lease renewal."""

    def __init__(self, url: str, unacknowledged_lock_timeout: float = 30, **kwargs: Any) -> None:
        super().__init__(url, unacknowledged_lock_timeout=unacknowledged_lock_timeout, **kwargs)
        self._held: set[Lease] = set()
        self._renewer: asyncio.Task[None] | None = None

    async def shutdown(self) -> None:
        """Stop renewing (held messages go idle and are reclaimed), then close the pool."""
        renewer, self._renewer = self._renewer, None
        if renewer is not None:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
        self._held.clear()
        await super().shutdown()

    async def listen(self) -> AsyncGenerator[AckableMessage, None]:
        """Yield new messages, then idle pending ones claimed from dead consumers."""
        if self._renewer is None or self._renewer.done():
            self._renewer = asyncio.create_task(self._renew_forever())
        unyielded: list[Lease] = []
        try:
            async with Redis(connection_pool=self.connection_pool) as redis_conn:
                while True:
                    fetched: Any = await redis_conn.xreadgroup(
                        self.consumer_group_name,
                        self.consumer_name,
                        {self.queue_name: ">", **self.additional_streams},  # type: ignore[dict-item]
                        block=self.block,
                        noack=False,
                        count=self.count,
                    )
                    batch: list[Any] = [(s, i, m) for s, msgs in fetched or [] for i, m in msgs]
                    for stream in [self.queue_name, *self.additional_streams.keys()]:
                        claimed = await self._claim(redis_conn, stream)
                        batch.extend((stream, i, m) for i, m in claimed)
                    # Hold the whole batch now: a message waiting here for a free slot must
                    # not go idle and be claimed by another worker.
                    unyielded = [(stream, msg_id) for stream, msg_id, _ in batch]
                    self._held.update(unyielded)
                    for stream, msg_id, msg in batch:
                        unyielded.pop(0)
                        yield AckableMessage(
                            data=msg[b"data"],
                            ack=self._ack_generator(id=msg_id, queue_name=stream),
                        )
        finally:
            # Closed mid-batch: release what was never handed to the worker.
            self._held.difference_update(unyielded)

    async def _claim(self, redis_conn: Redis, stream: str) -> list[Any]:
        """``XAUTOCLAIM`` idle pending entries of ``stream`` (same lock pattern as upstream)."""
        pipe = redis_conn.pipeline()
        lock = pipe.lock(
            f"autoclaim:{self.consumer_group_name}:{stream}",
            timeout=self.unacknowledged_lock_timeout,
        )
        await lock.acquire()
        await pipe.xautoclaim(
            name=stream,
            groupname=self.consumer_group_name,
            consumername=self.consumer_name,
            min_idle_time=self.idle_timeout,
            count=self.unacknowledged_batch_size,
        )
        await lock.release()
        results = await pipe.execute()
        claimed: list[Any] = results[1][1]
        if claimed:
            logger.info("Reclaimed %d idle message(s) from stream %s", len(claimed), stream)
        return claimed

    def _ack_generator(
        self,
        id: str,  # noqa: A002 (upstream signature)
        queue_name: str,
    ) -> Callable[[], Awaitable[None]]:
        """Stop renewing the message, then ack it (a failed ack leaves it to be reclaimed)."""
        upstream_ack = super()._ack_generator(id=id, queue_name=queue_name)

        async def _ack() -> None:
            self._held.discard((queue_name, id))
            await upstream_ack()

        return _ack

    async def _renew_forever(self) -> None:
        """Reset the idle time of every held message every ``idle_timeout / 3``."""
        interval = self.idle_timeout / 3000
        while True:
            await asyncio.sleep(interval)
            try:
                await self._renew_held()
            except Exception:
                logger.warning("Lease renewal failed; retrying", exc_info=True)

    async def _renew_held(self) -> None:
        if not self._held:
            return
        async with Redis(connection_pool=self.connection_pool) as redis_conn:
            for lease in list(self._held):
                stream, msg_id = lease
                args = (self.consumer_group_name, self.consumer_name, msg_id)
                owned = await redis_conn.eval(RENEW_SCRIPT, 1, stream, *args)  # type: ignore[misc]
                if not owned and lease in self._held:
                    self._held.discard(lease)
                    logger.warning("Lost lease on message %s in %s", msg_id, stream)
