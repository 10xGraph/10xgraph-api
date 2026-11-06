"""The transport contract (BaseTransport docstring). Runs against every factory in conftest."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from tenxgraph_api.jobs.transport.base import (
    FORMAT_VERSION,
    TransportMessage,
    UnknownMessageFormat,
)


pytestmark = pytest.mark.asyncio


def q() -> str:
    return f"q-{uuid.uuid4().hex[:12]}"


def msg(queue: str, priority: str = "normal") -> TransportMessage:
    return TransportMessage(run_id=uuid.uuid4().hex, queue=queue, priority=priority)


async def test_roundtrip_and_ack(tut):
    queue = q()
    m = msg(queue)
    await tut.transport.publish(m)
    got = await tut.transport.consume([queue], max_messages=10, block=0.5)
    assert [d.message for d in got] == [m]
    assert got[0].message.version == FORMAT_VERSION
    await tut.transport.ack(got)
    await tut.transport.ack(got)  # twice is a no-op
    await tut.redeliver()
    assert await tut.transport.consume([queue], max_messages=10) == []


async def test_empty_consume_returns_fast_and_respects_block(tut):
    queue = q()
    start = time.monotonic()
    assert await tut.transport.consume([queue], max_messages=5, block=0) == []
    assert await tut.transport.consume([queue], max_messages=5, block=0.1) == []
    assert time.monotonic() - start < 2.0


async def test_priority_order(tut):
    queue = q()
    low, normal, high = msg(queue, "low"), msg(queue, "normal"), msg(queue, "high")
    for m in (low, normal, high):
        await tut.transport.publish(m)
    got = []
    for _ in range(3):
        batch = await tut.transport.consume([queue], max_messages=1, block=0.5)
        got += batch
        await tut.transport.ack(batch)
    assert [d.message.run_id for d in got] == [high.run_id, normal.run_id, low.run_id]


async def test_max_messages_and_publish_many(tut):
    queue = q()
    await tut.transport.publish_many([msg(queue) for _ in range(5)])
    first = await tut.transport.consume([queue], max_messages=3, block=0.5)
    assert len(first) == 3
    rest = await tut.transport.consume([queue], max_messages=10, block=0.5)
    assert len(rest) == 2
    await tut.transport.ack(first + rest)


async def test_queue_isolation(tut):
    a, b = q(), q()
    await tut.transport.publish(msg(a))
    assert await tut.transport.consume([b], max_messages=5) == []
    got = await tut.transport.consume([a, b], max_messages=5, block=0.5)
    assert len(got) == 1
    await tut.transport.ack(got)


async def test_unacked_is_redelivered(tut):
    queue = q()
    m = msg(queue)
    await tut.transport.publish(m)
    got = await tut.transport.consume([queue], max_messages=1, block=0.5)
    assert len(got) == 1
    await tut.redeliver()
    again = await tut.transport.consume([queue], max_messages=1, block=0.5)
    assert [d.message.run_id for d in again] == [m.run_id]
    await tut.transport.ack(again)


async def test_blocking_consume_wakes_on_publish(tut):
    queue = q()
    m = msg(queue)

    async def later():
        await asyncio.sleep(0.05)
        await tut.transport.publish(m)

    publisher = asyncio.create_task(later())
    got = await tut.transport.consume([queue], max_messages=1, block=2.0)
    await publisher
    assert [d.message.run_id for d in got] == [m.run_id]
    await tut.transport.ack(got)


async def test_message_format_version():
    m = TransportMessage(run_id="r1", queue="default", priority="high")
    assert TransportMessage.from_dict(m.to_dict()) == m
    with pytest.raises(UnknownMessageFormat):
        TransportMessage.from_dict({"v": FORMAT_VERSION + 1, "run_id": "r1"})


async def test_unknown_format_is_acked_and_dropped(tut):
    """Never handed out and never left unacked (no XAUTOCLAIM hot loop)."""
    queue = q()
    await tut.inject_raw(
        {"v": FORMAT_VERSION + 1, "run_id": "r-future", "queue": queue, "p": "normal"}
    )
    await tut.inject_raw({"garbage": True, "queue": queue})
    good = msg(queue)
    await tut.transport.publish(good)
    got = await tut.transport.consume([queue], max_messages=10, block=0.5)
    assert [d.message.run_id for d in got] == [good.run_id]
    await tut.transport.ack(got)
    await tut.redeliver()
    assert await tut.transport.consume([queue], max_messages=10) == []


async def test_consume_after_close_returns_empty(tut):
    queue = q()
    await tut.transport.publish(msg(queue))
    await tut.transport.aclose()
    assert await tut.transport.consume([queue], max_messages=10, block=0.2) == []


async def test_close_during_blocking_consume_returns_empty(tut):
    queue = q()
    await tut.transport.consume([queue], max_messages=1)  # connect / create groups
    waiting = asyncio.create_task(tut.transport.consume([queue], max_messages=10, block=3.0))
    await asyncio.sleep(0.3)
    started = time.monotonic()
    await tut.transport.aclose()
    assert await asyncio.wait_for(waiting, 2.0) == []
    assert time.monotonic() - started < 2.0


async def test_consume_priorities_filter(tut):
    queue = q()
    low, normal, high = msg(queue, "low"), msg(queue, "normal"), msg(queue, "high")
    for m in (low, normal, high):
        await tut.transport.publish(m)
    assert await tut.transport.consume([queue], max_messages=10, priorities=[]) == []
    only_high = await tut.transport.consume(
        [queue], max_messages=10, block=0.5, priorities=["high"]
    )
    assert [d.message for d in only_high] == [high]
    await tut.transport.ack(only_high)
    assert (
        await tut.transport.consume([queue], max_messages=10, block=0.2, priorities=["high"]) == []
    )
    rest = await tut.transport.consume(
        [queue], max_messages=10, block=0.5, priorities=["low", "normal"]
    )
    assert [d.message for d in rest] == [normal, low]  # still highest first
    await tut.transport.ack(rest)
    assert await tut.transport.consume([queue], max_messages=10) == []
