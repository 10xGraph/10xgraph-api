"""Redis stream broker for the worker: takes only what it can start, renews the leases of running
tasks, survives a Redis restart and caps redeliveries.

Changes over taskiq-redis ``RedisStreamBroker``:

1. One message per read (``XREADGROUP`` and ``XAUTOCLAIM`` with count 1). TaskIQ's receiver asks
   for the next message only when it has a free slot, so a busy worker leaves the rest of the
   queue to other workers.
2. Idle pending messages are claimed also when the queue is empty (upstream claims only after
   new work arrives), at most once per ``xread_block``.
3. Every held message (handed to the worker, not acked) has its idle time reset every
   ``idle_timeout / 3``, so a long task is never claimed by another worker, while a dead
   worker's messages go idle and are claimed after ``idle_timeout``.
4. The lease ends with the message's callback (``_taskiq_patches.patch_receiver``):
   a callback that ends without an ack releases the message for redelivery instead of renewing
   it forever, and ``shutdown()`` releases what is still held, so another worker claims it at
   once instead of after ``idle_timeout``.
5. A dropped connection or a Redis restart does not end ``listen()``: it logs, backs off and
   reads again, and recreates a missing consumer group. Running tasks are not touched.
6. A message delivered more than ``max_deliveries`` times gets the ``DELIVERY_LIMIT_LABEL``;
   ``TaskMiddleware`` then fails the task with that error without running it, and it is acked.
"""

import asyncio
import functools
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from typing import Any, NamedTuple

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from taskiq import AckableMessage
from taskiq_redis import RedisStreamBroker

from tenxgraph_api.jobs._taskiq_patches import patch_receiver
from tenxgraph_api.jobs.state import DELIVERY_LIMIT_LABEL


logger = logging.getLogger("tenxgraph_api.jobs.redis_broker")

# Ends the lease of a message whose callback ended without an ack (see the module docstring).
patch_receiver()

DEFAULT_MAX_DELIVERIES = 5
MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
ACK_ATTEMPTS = 4  # one try plus three retries, 1, 2 and 4 s apart
REDIS_DOWN = (RedisConnectionError, RedisTimeoutError)

# Set the idle time (ms) of one pending entry, only while this consumer still owns it: 0 renews
# the lease, ``idle_timeout`` releases it (claimable at once). JUSTID keeps the delivery count.
# An acked entry, or one another worker has claimed, is left alone (returns 0).
LEASE_SCRIPT = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1, ARGV[2])
if #pending == 0 then return 0 end
redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[2], 0, ARGV[3], 'IDLE', ARGV[4], 'JUSTID')
return 1
"""

Lease = tuple[Any, Any]  # (stream name, message id) as returned by Redis


class _Delivery(NamedTuple):
    stream: Any
    msg_id: Any
    data: bytes
    deliveries: int


class LeasedMessage(AckableMessage):
    """An ``AckableMessage`` with its delivery count and the lease release that the receiver
    patch calls when the message's callback ends."""

    deliveries: int = 1
    release: Callable[[], Awaitable[None]]


class ReclaimingRedisStreamBroker(RedisStreamBroker):
    """``RedisStreamBroker`` that reads one message at a time, renews and releases leases,
    retries a lost connection and caps redeliveries at ``max_deliveries``."""

    def __init__(
        self,
        url: str,
        unacknowledged_lock_timeout: float = 30,
        max_deliveries: int = DEFAULT_MAX_DELIVERIES,
        **kwargs: Any,
    ) -> None:
        for option in ("xread_count", "unacknowledged_batch_size"):
            if kwargs.pop(option, 1) != 1:
                raise ValueError(
                    f"worker.broker.options.{option} is fixed at 1: a worker takes only the "
                    "messages it can start"
                )
        if max_deliveries < 1:
            raise ValueError("worker.broker.options.max_deliveries must be at least 1")
        super().__init__(
            url,
            unacknowledged_lock_timeout=unacknowledged_lock_timeout,
            xread_count=1,
            unacknowledged_batch_size=1,
            **kwargs,
        )
        self.max_deliveries = max_deliveries
        self._held: set[Lease] = set()
        self._renewer: asyncio.Task[None] | None = None
        self._next_claim = 0.0

    async def shutdown(self) -> None:
        """Stop renewing, close the broker, then release what is still held (deliveries never
        started, tasks cut by the shutdown) so another worker claims them now."""
        renewer, self._renewer = self._renewer, None
        if renewer is not None:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
        try:
            await super().shutdown()
        finally:
            held = list(self._held)
            if held:
                logger.warning("Releasing %d unfinished message(s) to other workers", len(held))
                await self._release(held)
                await self.connection_pool.disconnect()
            self._held.clear()

    async def listen(self) -> AsyncGenerator[AckableMessage, None]:
        """Yield one message per request: an idle one claimed from a dead consumer, else a new
        one. Redis errors are logged and retried, never raised."""
        if self._renewer is None or self._renewer.done():
            self._renewer = asyncio.create_task(self._renew_forever())
        ready: list[_Delivery] = []
        try:
            async with Redis(connection_pool=self.connection_pool) as redis_conn:
                while True:
                    if not ready:
                        ready = await self._fetch_retrying(redis_conn)
                        continue
                    yield self._message(ready.pop(0))
        finally:
            # Read (one per extra stream) but never handed to the worker.
            if ready:
                await self._release([(d.stream, d.msg_id) for d in ready])

    async def _fetch_retrying(self, redis_conn: Redis) -> list[_Delivery]:
        """``_fetch`` until Redis answers: back off on a lost connection, recreate a missing
        consumer group (Redis restarted without its data)."""
        delay = 0.0
        group_missing = False
        while True:
            try:
                if group_missing:
                    await self._declare_consumer_group()
                deliveries = await self._fetch(redis_conn)
            except (*REDIS_DOWN, ResponseError) as exc:
                if isinstance(exc, ResponseError):
                    if not str(exc).startswith("NOGROUP"):
                        raise
                    group_missing = True
                delay = min(max(delay * 2, MIN_BACKOFF_SECONDS), MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Reading stream %s failed (%s: %s); retrying in %.0f s",
                    self.queue_name,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            if delay:
                logger.info("Reading stream %s again", self.queue_name)
            return deliveries

    async def _fetch(self, redis_conn: Redis) -> list[_Delivery]:
        """Claim one idle message per stream (at most once per ``xread_block``), else read one
        new message per stream. What is returned is held (renewed) from here on."""
        found: list[_Delivery] = []
        now = time.monotonic()
        if now >= self._next_claim:
            self._next_claim = now + self.block / 1000
            for stream in [self.queue_name, *self.additional_streams]:
                found += await self._claim(redis_conn, stream)
        if not found:
            fetched: Any = await redis_conn.xreadgroup(
                self.consumer_group_name,
                self.consumer_name,
                {self.queue_name: ">", **self.additional_streams},  # type: ignore[dict-item]
                block=self.block,
                noack=False,
                count=1,
            )
            found = [
                _Delivery(stream, msg_id, msg[b"data"], 1)
                for stream, msgs in fetched or []
                for msg_id, msg in msgs
            ]
        self._held.update((d.stream, d.msg_id) for d in found)
        return found

    async def _claim(self, redis_conn: Redis, stream: str) -> list[_Delivery]:
        """``XAUTOCLAIM`` one entry idle for ``idle_timeout``, with its delivery count.
        ``XAUTOCLAIM`` is atomic, so no lock is needed."""
        result: Any = await redis_conn.xautoclaim(
            stream,
            self.consumer_group_name,
            self.consumer_name,
            min_idle_time=self.idle_timeout,
            count=1,
        )
        found = []
        for msg_id, msg in result[1]:
            pending: Any = await redis_conn.xpending_range(
                stream, self.consumer_group_name, min=msg_id, max=msg_id, count=1
            )
            deliveries = int(pending[0]["times_delivered"]) if pending else 1
            logger.info("Reclaimed message %s from %s (delivery %d)", msg_id, stream, deliveries)
            found.append(_Delivery(stream, msg_id, msg[b"data"], deliveries))
        return found

    def _message(self, delivery: _Delivery) -> LeasedMessage:
        data = delivery.data
        if delivery.deliveries > self.max_deliveries:
            data = self._mark_over_limit(delivery)
        return LeasedMessage(
            data=data,
            ack=self._ack_generator(id=delivery.msg_id, queue_name=delivery.stream),
            deliveries=delivery.deliveries,
            release=functools.partial(self._end_lease, delivery),
        )

    def _mark_over_limit(self, delivery: _Delivery) -> bytes:
        """The message with ``DELIVERY_LIMIT_LABEL`` set: the task fails without running."""
        reason = (
            f"Delivered {delivery.deliveries} times without finishing "
            f"(limit {self.max_deliveries}); not run again"
        )
        logger.error("Message %s in %s: %s", delivery.msg_id, delivery.stream, reason)
        try:
            message = self.formatter.loads(delivery.data)
        except Exception:
            return delivery.data  # unparseable: the receiver logs and acks it
        message.labels[DELIVERY_LIMIT_LABEL] = reason
        return self.formatter.dumps(message).message

    def _ack_generator(
        self,
        id: str,  # noqa: A002 (upstream signature)
        queue_name: str,
    ) -> Callable[[], Awaitable[None]]:
        """Ack, retrying a lost connection (a completed task must not run again), then stop
        renewing. A failed ack leaves the message to be released and run again."""
        upstream_ack = super()._ack_generator(id=id, queue_name=queue_name)

        async def _ack() -> None:
            for attempt in range(ACK_ATTEMPTS):
                try:
                    await upstream_ack()
                    break
                except REDIS_DOWN as exc:
                    if attempt == ACK_ATTEMPTS - 1:
                        raise
                    logger.warning("Ack of message %s failed (%s); retrying", id, exc)
                    await asyncio.sleep(MIN_BACKOFF_SECONDS * 2**attempt)
            self._held.discard((queue_name, id))

        return _ack

    async def _end_lease(self, delivery: _Delivery) -> None:
        """Called when the message's callback ends. Acked: nothing to do. Not acked: release it
        for redelivery, or ack it when it is already over the delivery limit."""
        lease = (delivery.stream, delivery.msg_id)
        if lease not in self._held:
            return
        if delivery.deliveries > self.max_deliveries:
            logger.error(
                "Message %s in %s dropped: delivery %d ended without an ack again",
                delivery.msg_id,
                delivery.stream,
                delivery.deliveries,
            )
            await self._ack_generator(id=delivery.msg_id, queue_name=delivery.stream)()
            return
        logger.warning(
            "Message %s in %s ended without an ack; released for redelivery",
            delivery.msg_id,
            delivery.stream,
        )
        await self._release([lease])

    async def _release(self, leases: Iterable[Lease]) -> None:
        """Stop renewing ``leases`` and make them claimable now. Errors are logged: an entry
        not released goes idle and is claimed after ``idle_timeout``."""
        leases = list(leases)
        self._held.difference_update(leases)
        try:
            async with Redis(connection_pool=self.connection_pool) as redis_conn:
                for stream, msg_id in leases:
                    await self._set_idle(redis_conn, stream, msg_id, self.idle_timeout)
        except Exception:
            logger.exception("Releasing %d message(s) failed", len(leases))

    async def _set_idle(self, redis_conn: Redis, stream: Any, msg_id: Any, idle: int) -> bool:
        args = (self.consumer_group_name, self.consumer_name, msg_id, idle)
        return bool(await redis_conn.eval(LEASE_SCRIPT, 1, stream, *args))  # type: ignore[misc]

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
                owned = await self._set_idle(redis_conn, *lease, 0)
                if not owned and lease in self._held:
                    self._held.discard(lease)
                    logger.warning("Lost lease on message %s in %s", lease[1], lease[0])
