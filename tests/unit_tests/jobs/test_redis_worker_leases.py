"""ReclaimingRedisStreamBroker against an in-memory fake of the Redis stream commands it uses.

Covers W1 (connection drop, lost consumer group, ack retry), W2 (one message per request,
release on shutdown), W3 (a callback that dies before the ack releases the message) and L10
(delivery cap). The fake keeps its own clock (``now``, ms) for idle times. The real-Redis checks
live in ``test_redis_broker.py`` (``--integration``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
import pytest_asyncio
import taskiq_redis.redis_broker as upstream
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from taskiq import TaskiqMiddleware
from taskiq.receiver import Receiver

from tenxgraph_api.jobs import redis_broker as rb
from tenxgraph_api.jobs.middleware import TaskMiddleware, wrap_tasks
from tenxgraph_api.jobs.state import DELIVERY_LIMIT_LABEL


pytestmark = pytest.mark.asyncio

STREAM, GROUP = "jobs", "g"


class FakeRedis:
    """The stream commands the broker sends, on one shared in-memory state."""

    def __init__(self) -> None:
        self.now = 0
        self.entries: dict[str, list[tuple[str, dict[bytes, bytes]]]] = {}
        # (stream, group) -> {"next": index of the next new entry, "pel": {id: [consumer, at, n]}}
        self.groups: dict[tuple[str, str], dict[str, Any]] = {}
        self.errors: list[BaseException] = []  # raised by the next commands, in order
        self.seq = 0

    def __call__(self, *args: Any, **kwargs: Any) -> FakeRedis:  # Redis(connection_pool=...)
        return self

    async def __aenter__(self) -> FakeRedis:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def _fail(self) -> None:
        if self.errors:
            raise self.errors.pop(0)

    def _group(self, stream: str, group: str) -> dict[str, Any]:
        found = self.groups.get((stream, group))
        if found is None:
            raise ResponseError("NOGROUP No such key or consumer group")
        return found

    def pel(self, stream: str = STREAM) -> dict[str, list[Any]]:
        return self.groups[(stream, GROUP)]["pel"]

    async def xgroup_create(self, name, groupname, id="$", mkstream=False):  # noqa: A002
        self._fail()
        if (name, groupname) in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        start = len(self.entries.setdefault(name, [])) if id == "$" else 0
        self.groups[(name, groupname)] = {"next": start, "pel": {}}

    async def xadd(self, name, fields, maxlen=None, approximate=True):
        self._fail()
        self.seq += 1
        self.entries.setdefault(name, []).append((f"{self.seq}-0", fields))

    async def xreadgroup(
        self, groupname, consumername, streams, block=None, noack=False, count=None
    ):
        self._fail()
        out = []
        for name in streams:
            group = self._group(name, groupname)
            new = self.entries.get(name, [])[group["next"] :][:count]
            group["next"] += len(new)
            for msg_id, _ in new:
                group["pel"][msg_id] = [consumername, self.now, 1]
            if new:
                out.append([name, new])
        if not out:
            await asyncio.sleep(0.001)
        return out

    async def xautoclaim(self, name, groupname, consumername, min_idle_time, count=None, **kw):
        self._fail()
        group = self._group(name, groupname)
        data = dict(self.entries.get(name, []))
        claimed = []
        for msg_id, entry in sorted(group["pel"].items()):
            if len(claimed) == count:
                break
            if self.now - entry[1] >= min_idle_time:
                group["pel"][msg_id] = [consumername, self.now, entry[2] + 1]
                claimed.append((msg_id, data[msg_id]))
        return ["0-0", claimed, []]

    async def xpending_range(self, name, groupname, min, max, count, **kw):  # noqa: A002
        self._fail()
        entry = self._group(name, groupname)["pel"].get(min)
        if entry is None:
            return []
        return [{"message_id": min, "consumer": entry[0], "times_delivered": entry[2]}]

    async def eval(self, script, numkeys, stream, group, consumer, msg_id, idle):
        self._fail()
        entry = self._group(stream, group)["pel"].get(msg_id)
        if entry is None or entry[0] != consumer:
            return 0
        entry[1] = self.now - idle
        return 1

    async def xack(self, name, groupname, *ids):
        self._fail()
        pel = self._group(name, groupname)["pel"]
        return sum(pel.pop(msg_id, None) is not None for msg_id in ids)


@pytest.fixture
def fake(monkeypatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr(rb, "Redis", redis)
    monkeypatch.setattr(upstream, "Redis", redis)
    monkeypatch.setattr(rb, "MIN_BACKOFF_SECONDS", 0.01)
    return redis


@pytest_asyncio.fixture
async def make_broker(fake):
    brokers: list[rb.ReclaimingRedisStreamBroker] = []

    async def make(consumer: str, **options: Any) -> rb.ReclaimingRedisStreamBroker:
        broker = rb.ReclaimingRedisStreamBroker(
            "redis://fake:6379/0",
            queue_name=STREAM,
            consumer_group_name=GROUP,
            consumer_name=consumer,
            idle_timeout=1000,
            xread_block=0,  # claim on every request
            **options,
        )
        await broker.startup()
        brokers.append(broker)
        return broker

    yield make
    for broker in brokers:
        await broker.shutdown()


async def kick(fake: FakeRedis, count: int = 1) -> None:
    for i in range(count):
        await fake.xadd(STREAM, {b"data": f"m{i}".encode()})


async def first(broker: rb.ReclaimingRedisStreamBroker) -> tuple[Any, Any]:
    gen = broker.listen()
    return gen, await asyncio.wait_for(anext(gen), 2)


# W1


async def test_listen_survives_a_dropped_connection(fake, make_broker, caplog):
    broker = await make_broker("a")
    await kick(fake)
    fake.errors = [RedisConnectionError("Connection closed by server.")] * 3

    gen, message = await first(broker)

    assert message.data == b"m0"
    assert "Connection closed by server." in caplog.text
    await gen.aclose()


async def test_listen_recreates_a_lost_consumer_group(fake, make_broker, caplog):
    broker = await make_broker("a")
    fake.groups.clear()  # Redis restarted without its data
    gen = broker.listen()
    pending = asyncio.ensure_future(anext(gen))
    await asyncio.sleep(0.05)
    assert (STREAM, GROUP) in fake.groups
    await kick(fake)

    message = await asyncio.wait_for(pending, 2)

    assert message.data == b"m0"
    assert "NOGROUP" in caplog.text
    await gen.aclose()


async def test_ack_retries_a_dropped_connection(fake, make_broker):
    broker = await make_broker("a")
    await kick(fake)
    gen, message = await first(broker)
    fake.errors = [RedisConnectionError("Connection closed by server.")]

    await message.ack()

    assert fake.pel() == {}
    assert broker._held == set()
    await gen.aclose()


# W2


async def test_worker_takes_one_message_per_request(fake, make_broker):
    busy, idle = await make_broker("busy"), await make_broker("idle")
    await kick(fake, 5)

    gen, _ = await first(busy)
    assert len(busy._held) == 1
    assert len(fake.pel()) == 1

    other, message = await first(idle)
    assert message.data == b"m1"  # the rest of the queue is left for other workers
    await gen.aclose()
    await other.aclose()


async def test_claims_one_idle_message_per_request(fake, make_broker):
    live = await make_broker("live")
    await kick(fake, 3)
    await fake.xreadgroup(GROUP, "dead", {STREAM: ">"}, count=3)
    fake.now += 1000

    gen, message = await first(live)

    assert message.data == b"m0"
    assert [entry[0] for entry in fake.pel().values()] == ["live", "dead", "dead"]
    await gen.aclose()


@pytest.mark.parametrize("option", ["xread_count", "unacknowledged_batch_size"])
async def test_batch_options_are_refused(option):
    with pytest.raises(ValueError, match=option):
        rb.ReclaimingRedisStreamBroker("redis://fake:6379/0", **{option: 100})


async def test_shutdown_releases_held_messages_at_once(fake, make_broker):
    leaving, staying = await make_broker("leaving"), await make_broker("staying")
    await kick(fake)
    gen, _ = await first(leaving)
    await gen.aclose()

    await leaving.shutdown()

    # No idle_timeout wait: the other worker claims it right away.
    other, message = await first(staying)
    assert message.data == b"m0"
    assert fake.pel()["1-0"][0] == "staying"
    await other.aclose()


# W3


class FailingMiddleware(TaskiqMiddleware):
    async def post_execute(self, message: Any, result: Any) -> None:
        raise RuntimeError("post_execute broke")


async def test_callback_dying_before_ack_releases_the_message(fake, make_broker, caplog):
    broker = await make_broker("a")
    ran: list[int] = []

    @broker.task
    async def job() -> None:
        ran.append(1)

    broker.add_middlewares(FailingMiddleware())
    await job.kiq()
    gen, message = await first(broker)

    await Receiver(broker).callback(message)

    assert ran == [1]
    assert "post_execute broke" in caplog.text
    assert broker._held == set()  # no longer renewed
    entry = fake.pel()["1-0"]
    assert fake.now - entry[1] >= broker.idle_timeout  # claimable now
    await gen.aclose()


# L10


async def test_message_over_the_delivery_limit_fails_without_running(fake, make_broker):
    broker = await make_broker("a", max_deliveries=2)
    ran: list[int] = []

    @broker.task
    async def job() -> None:
        ran.append(1)

    broker.add_middlewares(TaskMiddleware())
    wrap_tasks(broker)
    await job.kiq()
    # Two deliveries killed their worker.
    await fake.xreadgroup(GROUP, "dead1", {STREAM: ">"}, count=1)
    fake.now += 1000
    await fake.xautoclaim(STREAM, GROUP, "dead2", 1000, count=1)
    fake.now += 1000

    gen, message = await first(broker)
    assert message.deliveries == 3
    assert DELIVERY_LIMIT_LABEL in broker.formatter.loads(message.data).labels

    results = []
    broker.result_backend.set_result = lambda task_id, result: _record(results, result)
    await Receiver(broker).callback(message)

    assert ran == []
    assert type(results[0].error).__name__ == "DeliveryLimitError"
    assert "limit 2" in str(results[0].error)
    assert fake.pel() == {}  # acked
    await gen.aclose()


async def _record(results: list[Any], result: Any) -> None:
    results.append(result)


async def test_over_limit_message_whose_callback_dies_again_is_dropped(fake, make_broker, caplog):
    broker = await make_broker("a", max_deliveries=1)
    await kick(fake)
    await fake.xreadgroup(GROUP, "dead", {STREAM: ">"}, count=1)
    fake.now += 1000
    gen, message = await first(broker)
    assert message.deliveries == 2

    with caplog.at_level(logging.ERROR):
        await message.release()  # the callback ended without an ack

    assert fake.pel() == {}
    assert "dropped" in caplog.text
    await gen.aclose()
