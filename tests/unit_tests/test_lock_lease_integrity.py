"""Lease integrity of the shared limiter behind the thread lock (G-91, G-94)."""

# ruff: noqa: S101

import asyncio

import pytest

from tenxgraph_api.src.app.core import concurrency
from tenxgraph_api.src.app.core.concurrency import RedisConcurrencyLimiter

from .test_concurrency_limiter import _FakeRedis


KEY = "10xgraph:concurrency:runs:thread:t1"


class _SlowPipeRedis(_FakeRedis):
    """A Redis whose transaction takes a while, so a caller can be cancelled inside it."""

    def pipeline(self, transaction=True):
        pipe = super().pipeline(transaction)
        execute = pipe.execute

        async def slow_execute():
            results = await execute()  # the server applied it ...
            await asyncio.sleep(10)  # ... but the reply is slow
            return results

        pipe.execute = slow_execute
        return pipe


class _StrictCloseRedis(_FakeRedis):
    """Refuses commands once closed, like a real client."""

    async def zrem(self, key, member):
        await asyncio.sleep(0.01)
        if self.closed:
            raise ConnectionError("client closed")
        await super().zrem(key, member)


@pytest.mark.asyncio
async def test_redis_outage_keeps_one_run_per_thread_in_this_process():
    redis = _FakeRedis()
    redis.fail = True
    limiter = RedisConcurrencyLimiter(redis)
    first = await limiter.acquire("runs:thread:t1", 1)
    assert first is not None
    assert await limiter.acquire("runs:thread:t1", 1) is None
    first.release()
    assert await limiter.acquire("runs:thread:t1", 1) is not None
    await limiter.aclose()


@pytest.mark.asyncio
async def test_lost_lease_is_detected_and_restored():
    redis = _FakeRedis()
    limiter = RedisConcurrencyLimiter(redis, lease_ttl=0.06)
    lease = await limiter.acquire("runs:thread:t1", 1)
    assert lease is not None and not lease.lost
    redis.zsets[KEY].clear()  # evicted, or expired during a stall
    await asyncio.sleep(0.05)
    assert lease.lost
    assert len(redis.zsets[KEY]) == 1  # held again, so a second run is still refused
    assert await limiter.acquire("runs:thread:t1", 1) is None
    lease.release()
    await limiter.aclose()


@pytest.mark.asyncio
async def test_a_cancelled_waiter_leaves_no_ghost_lease():
    redis = _SlowPipeRedis()
    limiter = RedisConcurrencyLimiter(redis, lease_ttl=60)
    waiter = asyncio.create_task(limiter.acquire("runs:thread:t1", 1))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0.05)
    assert redis.zsets.get(KEY, {}) == {}
    await limiter.aclose()


@pytest.mark.asyncio
async def test_aclose_waits_for_pending_releases_before_closing_the_client():
    redis = _StrictCloseRedis()
    limiter = RedisConcurrencyLimiter(redis, lease_ttl=60)
    lease = await limiter.acquire("runs:thread:t1", 1)
    assert lease is not None
    lease.release()  # scheduled, not yet run
    await limiter.aclose()
    assert redis.zsets[KEY] == {}
    assert redis.closed
    assert not concurrency._pending_releases
