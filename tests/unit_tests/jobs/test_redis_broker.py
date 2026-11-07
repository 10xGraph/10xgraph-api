"""Live tests for ReclaimingRedisStreamBroker (idle-queue reclaim and lease renewal).

Need a real Redis: run with ``--integration``. The URL comes from ``TEST_JOBS_REDIS_URL``
(default ``redis://localhost:16379/7``); the test is skipped when it is not reachable.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from taskiq import AckableMessage
from taskiq.message import BrokerMessage

from tenxgraph_api.jobs.redis_broker import ReclaimingRedisStreamBroker


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

REDIS_URL = os.environ.get("TEST_JOBS_REDIS_URL", "redis://localhost:16379/7")
GROUP = "g"


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    try:
        await asyncio.wait_for(client.ping(), 2)
    except Exception:
        await client.aclose()
        pytest.skip(f"Redis not reachable at {REDIS_URL}")
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def queue(redis: Redis) -> AsyncIterator[str]:
    name = f"test-jobs-{uuid.uuid4().hex}"
    yield name
    await redis.delete(name, f"autoclaim:{GROUP}:{name}")


@pytest_asyncio.fixture
async def make_broker(queue: str) -> AsyncIterator[Callable[[str], ReclaimingRedisStreamBroker]]:
    brokers: list[ReclaimingRedisStreamBroker] = []

    def make(consumer: str) -> ReclaimingRedisStreamBroker:
        broker = ReclaimingRedisStreamBroker(
            REDIS_URL,
            queue_name=queue,
            consumer_group_name=GROUP,
            consumer_name=consumer,
            idle_timeout=1000,
            xread_block=200,
        )
        brokers.append(broker)
        return broker

    yield make
    for broker in brokers:
        await broker.shutdown()


async def kick(broker: ReclaimingRedisStreamBroker, data: bytes) -> None:
    msg = BrokerMessage(task_id=uuid.uuid4().hex, task_name="t", message=data, labels={})
    await broker.kick(msg)


async def collect(broker: ReclaimingRedisStreamBroker, into: list[AckableMessage]) -> None:
    async for message in broker.listen():
        into.append(message)


async def pending_count(redis: Redis, queue: str) -> int:
    info = await redis.xpending(queue, GROUP)
    return int(info["pending"])


async def test_idle_queue_reclaims_dead_consumer_message(redis, queue, make_broker):
    live = make_broker("live")
    await live.startup()
    await kick(live, b"orphan")
    # A dead consumer read it and never acked.
    await redis.xreadgroup(GROUP, "dead", {queue: ">"}, count=1)

    # No new message arrives; the live consumer must still claim it once it is idle.
    gen = live.listen()
    message = await asyncio.wait_for(anext(gen), 5)
    assert message.data == b"orphan"
    await message.ack()
    await gen.aclose()
    assert await pending_count(redis, queue) == 0


async def test_renewal_keeps_long_task_from_being_reclaimed(redis, queue, make_broker):
    worker_a, worker_b = make_broker("a"), make_broker("b")
    await worker_a.startup()
    await worker_b.startup()
    await kick(worker_a, b"long")

    gen_a = worker_a.listen()
    message = await asyncio.wait_for(anext(gen_a), 5)
    seen_by_b: list[AckableMessage] = []
    task_b = asyncio.create_task(collect(worker_b, seen_by_b))
    await asyncio.sleep(3)  # three idle timeouts while "running"

    assert seen_by_b == []
    await message.ack()
    task_b.cancel()
    await asyncio.gather(task_b, return_exceptions=True)
    await gen_a.aclose()
    assert await pending_count(redis, queue) == 0


async def test_ack_stops_renewal_and_late_renew_does_not_resurrect(redis, queue, make_broker):
    worker = make_broker("a")
    await worker.startup()
    await kick(worker, b"quick")
    gen = worker.listen()
    message = await asyncio.wait_for(anext(gen), 5)
    assert len(worker._held) == 1

    await message.ack()
    assert worker._held == set()
    # A renewal racing the ack: renew the acked id directly.
    stream = queue.encode()
    msg_id = (await redis.xrange(queue))[0][0]
    worker._held.add((stream, msg_id))
    await worker._renew_held()
    assert worker._held == set()  # lost lease dropped
    assert await pending_count(redis, queue) == 0
    await gen.aclose()


async def test_shutdown_cancels_renewal(redis, queue, make_broker):
    worker_a, worker_b = make_broker("a"), make_broker("b")
    await worker_a.startup()
    await worker_b.startup()
    await kick(worker_a, b"abandoned")
    gen_a = worker_a.listen()
    await asyncio.wait_for(anext(gen_a), 5)
    renewer = worker_a._renewer
    assert renewer is not None and not renewer.done()

    await gen_a.aclose()
    await worker_a.shutdown()
    assert renewer.cancelled()
    assert worker_a._renewer is None and worker_a._held == set()

    # No longer renewed: it goes idle and another worker claims it.
    gen_b = worker_b.listen()
    message = await asyncio.wait_for(anext(gen_b), 5)
    assert message.data == b"abandoned"
    await message.ack()
    await gen_b.aclose()


async def test_closing_listen_mid_batch_releases_unyielded(redis, queue, make_broker):
    worker = make_broker("a")
    await worker.startup()
    for i in range(3):
        await kick(worker, f"m{i}".encode())
    gen = worker.listen()
    first = await asyncio.wait_for(anext(gen), 5)
    assert len(worker._held) == 3  # whole batch held while waiting for a slot

    await gen.aclose()
    assert len(worker._held) == 1  # only the one handed to the worker
    await first.ack()
    assert worker._held == set()
