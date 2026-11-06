"""Redis-only checks for the jobs run store and Streams transport (real Redis 8 service).

Run with ``--integration`` and the compose services up (see README.md). Every test uses a
unique key prefix and removes its keys; the fault tests pause, kill or signal only containers
and processes of this test project.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import redis.asyncio as redis_asyncio
from redis.asyncio.cluster import RedisCluster
from redis.crc import key_slot
from redis.exceptions import RedisError

from tenxgraph_api.jobs.core.models import AttemptOutcome, ClaimStatus, EventIn, HeldLease, NewRun
from tenxgraph_api.jobs.store.base import REDIS_HASH_TAG
from tenxgraph_api.jobs.store.redis import FALLBACK_PREFIX, RedisRunStore, open_jobs_redis
from tenxgraph_api.jobs.transport.base import TransportMessage
from tenxgraph_api.jobs.transport.redis import RedisStreamsTransport


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ROOT = Path(__file__).resolve().parents[3]


def new_run(**over) -> NewRun:
    data = {
        "run_id": f"r-{uuid.uuid4().hex}",
        "task": f"task-{uuid.uuid4().hex[:8]}",
        "owner_user_id": "u1",
        "identity": {"user_id": "u1"},
        "retention": 0.0,
    }
    data.update(over)
    return NewRun(**data)


async def delete_prefix(client, prefix: str) -> None:
    keys = [k async for k in client.scan_iter(match=f"{prefix}*", count=500)]
    if keys:
        await client.delete(*keys)


def url_with_db(url: str, db: int) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{db}"


async def consume_all(transport, queues, want: int, *, timeout: float) -> list:
    """Consume (and ack) until ``want`` run ids arrived; errors are retried like the worker."""
    got: list = []
    deadline = time.monotonic() + timeout
    while len(got) < want and time.monotonic() < deadline:
        try:
            batch = await transport.consume(queues, max_messages=50, block=2.0)
            await transport.ack(batch)
        except (RedisError, OSError):
            await asyncio.sleep(0.2)
            continue
        got += [d.message.run_id for d in batch]
    return got


# ---------------------------------------------------------------------- own storage


async def test_db_fallback_when_select_is_refused(redis_client, redis_url, key_prefix, caplog):
    """An ACL user that may not SELECT: the store uses db 0 with the fallback prefix."""
    user, password = f"jt-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    await redis_client.acl_setuser(
        user,
        enabled=True,
        passwords=[f"+{password}"],
        keys=["*"],
        channels=["*"],
        commands=["+@all", "-select"],
    )
    host = redis_url.split("//", 1)[1].split("/", 1)[0]
    url = f"redis://{user}:{password}@{host}/1"
    db0 = redis_asyncio.from_url(url_with_db(redis_url, 0), decode_responses=True)
    store = RedisRunStore(url=url, db=1, key_prefix=key_prefix)
    try:
        with caplog.at_level(logging.WARNING, logger="tenxgraph_api.jobs"):
            await store.setup()
            run = (await store.create_run(new_run())).run
        warnings = [r for r in caplog.records if "refused database 1" in r.getMessage()]
        assert len(warnings) == 1
        assert store.db == 0
        assert store.key_prefix == key_prefix + FALLBACK_PREFIX
        key = store.key("run", run.run_id)
        assert key.startswith(key_prefix + FALLBACK_PREFIX + REDIS_HASH_TAG)
        assert await db0.exists(key) == 1
        assert await redis_client.exists(key) == 0  # nothing landed in db 1
        assert (await store.get_run(run.run_id)).run_id == run.run_id
    finally:
        await delete_prefix(db0, key_prefix)
        await store.aclose()
        await db0.aclose()
        await redis_client.acl_deluser(user)


async def test_own_db_and_cluster_client_fallback(redis_client, redis_url, key_prefix):
    store = RedisRunStore(url=url_with_db(redis_url, 0), db=1, key_prefix=key_prefix)
    try:
        await store.setup()
        assert store.db == 1 and store.key_prefix == key_prefix  # db param wins over the URL
        run = (await store.create_run(new_run())).run
        assert await redis_client.exists(store.key("run", run.run_id)) == 1
    finally:
        await store.aclose()
    cluster = RedisCluster(host="localhost", port=16379)  # never connected
    conn = await open_jobs_redis(
        url=None, client=cluster, db=1, key_prefix=key_prefix, options={}, what="test"
    )
    assert conn.db == 0 and conn.prefix == key_prefix + FALLBACK_PREFIX and not conn.owned


async def test_every_key_shares_one_cluster_slot(redis_client, redis_url, key_prefix):
    """All keys the store and transport write hash to the slot of REDIS_HASH_TAG."""
    store = RedisRunStore(url=redis_url, key_prefix=key_prefix)
    transport = RedisStreamsTransport(url=redis_url, key_prefix=key_prefix, consumer="slot-test")
    try:
        await store.setup()
        task = f"task:{{odd}}:{uuid.uuid4().hex[:6]}"  # braces and colons in user values
        a = (
            await store.create_run(new_run(task=task, idempotency_key="k{1}", concurrency_key="c"))
        ).run
        b = (await store.create_run(new_run(task=task, delay=100))).run
        await store.claim(a.run_id, worker_id="w", lease_ttl=5, concurrency_limit=1)
        await store.append_events(
            a.run_id, attempt=1, worker_id="w", events=[EventIn(type="log", at=1.0)]
        )
        await store.request_cancel(b.run_id)
        await store.transition(
            a.run_id, attempt=1, worker_id="w", outcome=AttemptOutcome(kind="succeeded")
        )
        await store.recount_concurrency()
        await transport.publish(TransportMessage(run_id=a.run_id, queue="q:{x}", priority="high"))
        await transport.consume(["q:{x}"], max_messages=5, block=0.5)
        keys = [k async for k in redis_client.scan_iter(match=f"{key_prefix}*", count=500)]
        assert len(keys) >= 8
        slot = key_slot(REDIS_HASH_TAG.encode())
        assert {key_slot(k) for k in keys} == {slot}
        assert key_slot((FALLBACK_PREFIX + store.key("run", "x")).encode()) == slot
    finally:
        await store.aclose()
        await transport.aclose()
        await delete_prefix(redis_client, key_prefix)


# ---------------------------------------------------------------------- faults


async def test_blocking_read_survives_docker_pause(fault, redis_client, redis_url, key_prefix):
    """A read stuck on a paused server times out, reconnects and loses nothing."""
    transport = RedisStreamsTransport(url=redis_url, key_prefix=key_prefix, consumer="pause")
    queue = "paused"
    try:
        await transport.setup()
        await transport.publish(TransportMessage(run_id="before", queue=queue))
        assert await consume_all(transport, [queue], 1, timeout=10) == ["before"]
        reader = asyncio.create_task(consume_all(transport, [queue], 1, timeout=60))
        await asyncio.sleep(0.5)  # the reader is inside a blocking XREADGROUP
        await asyncio.to_thread(fault.docker_pause, "jobs-redis", 9)
        ids = [f"after-{i}" for i in range(20)]
        await transport.publish_many([TransportMessage(run_id=i, queue=queue) for i in ids])
        got = await reader
        got += await consume_all(transport, [queue], len(ids) - len(got), timeout=30)
        assert sorted(got) == sorted(ids)
        pending = await redis_client.xpending(
            transport.stream_key(queue, "normal"), transport.group
        )
        assert pending["pending"] == 0
    finally:
        await transport.aclose()
        await delete_prefix(redis_client, key_prefix)


async def test_kill_and_restart_redis_mid_consume(fault, redis_url, key_prefix):
    """Redis dies (its streams and groups with it); the consumer recreates its group and goes on."""
    transport = RedisStreamsTransport(url=redis_url, key_prefix=key_prefix, consumer="kill")
    queue = "killed"
    try:
        await transport.setup()
        await transport.publish(TransportMessage(run_id="first", queue=queue))
        assert await consume_all(transport, [queue], 1, timeout=10) == ["first"]
        reader = asyncio.create_task(consume_all(transport, [queue], 10, timeout=90))
        await asyncio.sleep(0.5)
        await asyncio.to_thread(fault.docker_kill, "jobs-redis")
        ids = [f"after-{i}" for i in range(10)]
        for _ in range(50):
            try:
                await transport.publish_many([TransportMessage(run_id=i, queue=queue) for i in ids])
                break
            except (RedisError, OSError):
                await asyncio.sleep(0.2)
        got = await reader
        assert sorted(got) == sorted(ids)
    finally:
        await transport.aclose()
        client = redis_asyncio.from_url(redis_url)
        await delete_prefix(client, key_prefix)
        await client.aclose()


async def test_duplicate_delivery_after_autoclaim_is_fenced(redis_client, redis_url, key_prefix):
    store = RedisRunStore(url=redis_url, key_prefix=key_prefix)
    a = RedisStreamsTransport(url=redis_url, key_prefix=key_prefix, consumer="a")
    b = RedisStreamsTransport(url=redis_url, key_prefix=key_prefix, consumer="b")
    try:
        run = (await store.create_run(new_run(queue="dup"))).run
        await a.publish(TransportMessage(run_id=run.run_id, queue="dup"))
        first = await a.consume(["dup"], max_messages=1, block=1)
        assert (await store.claim(run.run_id, worker_id="a", lease_ttl=5)).status == (
            ClaimStatus.CLAIMED
        )
        await b.consume(["dup"], max_messages=1)  # b joins the group
        assert await b.reclaim(min_idle=0) == 1  # a "died" before acking
        second = await b.consume(["dup"], max_messages=1, block=1)
        assert [d.message.run_id for d in second] == [run.run_id]
        assert first[0].receipt == second[0].receipt
        late = await store.claim(run.run_id, worker_id="b", lease_ttl=5)
        assert late.status == ClaimStatus.NOT_READY
        await b.ack(second)
        await a.ack(first)  # acking an entry another consumer owns now is a no-op
        stored = await store.get_run(run.run_id)
        assert stored.attempt == 1 and stored.worker_id == "a"
    finally:
        await a.aclose()
        await b.aclose()
        await store.aclose()
        await delete_prefix(redis_client, key_prefix)


CHILD = r"""
import asyncio, json, sys
from tenxgraph_api.jobs.core.models import AttemptOutcome, HeldLease
from tenxgraph_api.jobs.store.redis import RedisRunStore
from tenxgraph_api.jobs.transport.redis import RedisStreamsTransport

async def main(url, prefix, queue):
    store = RedisRunStore(url=url, key_prefix=prefix)
    transport = RedisStreamsTransport(url=url, key_prefix=prefix, consumer="child")
    deliveries = []
    while not deliveries:
        deliveries = await transport.consume([queue], max_messages=1, block=2)
    run_id = deliveries[0].message.run_id
    claim = await store.claim(run_id, worker_id="child", lease_ttl=1.0)
    await transport.ack(deliveries)
    print(json.dumps({"claimed": claim.status.value, "attempt": claim.run.attempt}), flush=True)
    while True:
        owned = await store.renew_leases("child", [HeldLease(run_id=run_id, attempt=1)], lease_ttl=1.0)
        if run_id not in owned:
            break
        await asyncio.sleep(0.2)
    done = await store.transition(run_id, attempt=1, worker_id="child",
                                  outcome=AttemptOutcome(kind="succeeded", result="stale"))
    print(json.dumps({"lost": True, "fenced": done is None}), flush=True)
    await transport.aclose()
    await store.aclose()

asyncio.run(main(*sys.argv[1:]))
"""


async def test_sigstop_consumer_past_lease(fault, redis_client, redis_url, key_prefix):
    """A consumer frozen past its lease loses the run to the sweeper and is fenced on wake-up."""
    store = RedisRunStore(url=redis_url, key_prefix=key_prefix)
    transport = RedisStreamsTransport(url=redis_url, key_prefix=key_prefix, consumer="parent")
    run = (await store.create_run(new_run(queue="stop"))).run
    await transport.publish(TransportMessage(run_id=run.run_id, queue="stop"))
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, redis_url, key_prefix, "stop"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    try:
        line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 30)
        assert json.loads(line) == {"claimed": "claimed", "attempt": 1}
        fault.kill_process(child.pid, signal.SIGSTOP)
        await asyncio.sleep(1.5)
        expired = await store.find_expired_leases()
        assert HeldLease(run_id=run.run_id, attempt=1) in expired
        lost = await store.mark_lost(run.run_id, attempt=1)
        assert lost is not None and lost.run.state == "queued"
        again = await store.claim(run.run_id, worker_id="parent", lease_ttl=30)
        assert again.status == ClaimStatus.CLAIMED and again.run.attempt == 2
        fault.kill_process(child.pid, signal.SIGCONT)
        line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 30)
        assert json.loads(line) == {"lost": True, "fenced": True}
        assert await asyncio.to_thread(child.wait, 30) == 0
        stored = await store.get_run(run.run_id)
        assert stored.state == "running" and stored.worker_id == "parent"
        assert stored.attempt == 2 and stored.result is None and stored.lost_count == 1
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGCONT)
            child.kill()
            child.wait()
        await transport.aclose()
        await store.aclose()
        await delete_prefix(redis_client, key_prefix)


# ---------------------------------------------------------------------- close with a read in flight (M2)


async def test_aclose_with_consume_in_flight_drops_nothing(redis_client, redis_url, key_prefix):
    """M2: a message delivered to the in-flight read after aclose checked XPENDING was lost by
    DELCONSUMER. The hook publishes exactly in that window; it must reach a new consumer."""
    queue = f"q-{uuid.uuid4().hex[:8]}"
    closing = RedisStreamsTransport(url=redis_url, key_prefix=key_prefix, consumer="closing")
    later = RedisStreamsTransport(url=redis_url, key_prefix=key_prefix, consumer="later")
    message = TransportMessage(run_id=f"r-{uuid.uuid4().hex}", queue=queue)
    try:
        await closing.consume([queue], max_messages=1)  # groups exist
        waiting = asyncio.create_task(closing.consume([queue], max_messages=10, block=3.0))
        await asyncio.sleep(0.3)  # blocked in XREADGROUP
        client = closing._conn.client
        real_xpending = client.xpending_range

        async def xpending_then_publish(*args, **kwargs):
            pending = await real_xpending(*args, **kwargs)
            if not getattr(xpending_then_publish, "done", False):
                xpending_then_publish.done = True  # type: ignore[attr-defined]
                await later.publish(message)
                await asyncio.sleep(0.3)  # an in-flight read would take it now
            return pending

        client.xpending_range = xpending_then_publish
        await closing.aclose()
        assert await asyncio.wait_for(waiting, 2.0) == []
        got = await later.consume([queue], max_messages=10, block=2.0)
        if not got:  # delivered to "closing" before it left: it is pending, so reclaimable
            await later.reclaim(min_idle=0.0)
            got = await later.consume([queue], max_messages=10, block=0.5)
        assert [d.message for d in got] == [message]
        await later.ack(got)
    finally:
        await closing.aclose()
        await later.aclose()
        await delete_prefix(redis_client, key_prefix)


async def test_cancelled_consume_rereads_pending_entries(redis_client, redis_url, key_prefix):
    """L1: a consume cancelled after Redis delivered entries re-reads them (XREADGROUP 0)."""
    queue = f"q-{uuid.uuid4().hex[:8]}"
    transport = RedisStreamsTransport(url=redis_url, key_prefix=key_prefix, consumer="c1")
    message = TransportMessage(run_id=f"r-{uuid.uuid4().hex}", queue=queue)
    try:
        await transport.consume([queue], max_messages=1)
        assert transport._recover is False
        reader = transport._reader
        real = reader.xreadgroup

        async def read_then_hang(*args, **kwargs):
            reply = await real(*args, **kwargs)
            if reply:
                await asyncio.sleep(10)  # entries are delivered (pending), the reply never lands
            return reply

        reader.xreadgroup = read_then_hang
        await transport.publish(message)
        task = asyncio.create_task(transport.consume([queue], max_messages=10, block=1.0))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport._recover is True
        reader.xreadgroup = real
        got = await transport.consume([queue], max_messages=10, block=0.5)
        assert [d.message for d in got] == [message]
        await transport.ack(got)
    finally:
        await transport.aclose()
        await delete_prefix(redis_client, key_prefix)


# ---------------------------------------------------------------------- purge (L3)


async def test_purge_in_batches(redis_client, redis_url, key_prefix):
    store = RedisRunStore(url=redis_url, key_prefix=key_prefix)
    try:
        await store.setup()
        ids = []
        for _ in range(230):
            run = (await store.create_run(new_run())).run
            claim = await store.claim(run.run_id, worker_id="w", lease_ttl=30)
            await store.transition(
                run.run_id,
                attempt=claim.run.attempt,
                worker_id="w",
                outcome=AttemptOutcome(kind="succeeded"),
            )
            ids.append(run.run_id)
        await asyncio.sleep(0.01)
        assert await store.purge_expired(limit=120) == 120
        assert await store.purge_expired(limit=1000) == 110
        assert await store.purge_expired() == 0
        for run_id in ids[:5]:
            assert await store.get_run(run_id) is None
            assert await redis_client.exists(store.key("events", run_id)) == 0
    finally:
        await store.aclose()
        await delete_prefix(redis_client, key_prefix)
