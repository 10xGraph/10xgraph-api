"""Shared run / WebSocket caps across workers."""

# ruff: noqa: S101

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import WebSocketException
from injectq import InjectQ

from tenxgraph_api.src.app.core import concurrency
from tenxgraph_api.src.app.core.concurrency import (
    ConcurrencyLimiter,
    Lease,
    MemoryConcurrencyLimiter,
    RedisConcurrencyLimiter,
)


class _FakeRedis:
    """Enough of redis.asyncio for sorted-set leases; one instance = one shared server."""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.fail = False
        self.closed = False

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    async def zrem(self, key, member):
        self._check()
        self.zsets.get(key, {}).pop(member, None)

    async def zadd(self, key, mapping, xx=False):
        self._check()
        zset = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            if xx and member not in zset:
                continue
            zset[member] = score

    async def expire(self, key, seconds):
        self._check()

    async def aclose(self):
        self.closed = True

    def pipeline(self, transaction=True):
        redis, ops = self, []

        class _Pipe:
            def zremrangebyscore(self, key, low, high):
                ops.append(("zrem_range", key, high))

            def zadd(self, key, mapping):
                ops.append(("zadd", key, mapping))

            def zcard(self, key):
                ops.append(("zcard", key))

            def expire(self, key, seconds):
                ops.append(("expire", key))

            async def execute(self):
                redis._check()
                results = []
                for op in ops:
                    zset = redis.zsets.setdefault(op[1], {})
                    if op[0] == "zrem_range":
                        for member in [m for m, score in zset.items() if score <= op[2]]:
                            del zset[member]
                        results.append(None)
                    elif op[0] == "zadd":
                        zset.update(op[2])
                        results.append(1)
                    elif op[0] == "zcard":
                        results.append(len(zset))
                    else:
                        results.append(True)
                return results

        return _Pipe()


# ---------------------------------------------------------------------------
# in-process limiter and leases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_limiter_counts_per_key():
    limiter = MemoryConcurrencyLimiter()
    first = await limiter.acquire("k", 2)
    assert await limiter.acquire("k", 2) is not None
    assert await limiter.acquire("k", 2) is None
    assert await limiter.acquire("other", 2) is not None
    first.release()
    first.release()  # idempotent
    assert limiter.held("k") == 1


@pytest.mark.asyncio
async def test_lease_schedules_async_release_from_sync_code():
    released = asyncio.Event()

    async def release():
        released.set()

    lease = Lease(release)
    lease.release()
    await asyncio.wait_for(released.wait(), 1)
    assert lease.released


def test_lease_release_without_a_loop_lets_the_lease_expire():
    async def release():  # pragma: no cover - must not run
        raise AssertionError

    Lease(release).release()  # no running loop: closed, not awaited, no warning


# ---------------------------------------------------------------------------
# Redis limiter: one cap across workers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_workers_share_one_cap():
    redis = _FakeRedis()
    worker_a = RedisConcurrencyLimiter(redis, lease_ttl=60)
    worker_b = RedisConcurrencyLimiter(redis, lease_ttl=60)

    lease_a = await worker_a.acquire("runs:user:alice", 2)
    lease_b = await worker_b.acquire("runs:user:alice", 2)
    assert lease_a is not None and lease_b is not None
    # A per-process count would allow a third (one each) -- the shared one does not.
    assert await worker_a.acquire("runs:user:alice", 2) is None
    assert await worker_b.acquire("runs:user:alice", 2) is None
    assert len(redis.zsets["10xgraph:concurrency:runs:user:alice"]) == 2  # backed out

    lease_a.release()
    await asyncio.sleep(0)  # let the async release run
    assert await worker_b.acquire("runs:user:alice", 2) is not None
    await worker_a.aclose()
    await worker_b.aclose()


@pytest.mark.asyncio
async def test_a_dead_workers_lease_expires():
    redis = _FakeRedis()
    limiter = RedisConcurrencyLimiter(redis, lease_ttl=60)
    clock = [time.time()]
    with patch.object(concurrency.time, "time", lambda: clock[0]):
        lease = await limiter.acquire("k", 1)
        assert lease is not None
        await limiter.aclose()  # the worker dies: no refresh, no release
        assert await limiter.acquire("k", 1) is None
        clock[0] += 61
        assert await limiter.acquire("k", 1) is not None


@pytest.mark.asyncio
async def test_a_held_lease_is_refreshed():
    redis = _FakeRedis()
    limiter = RedisConcurrencyLimiter(redis, lease_ttl=0.06)
    lease = await limiter.acquire("k", 1)
    first_expiry = next(iter(redis.zsets["10xgraph:concurrency:k"].values()))
    await asyncio.sleep(0.05)
    assert next(iter(redis.zsets["10xgraph:concurrency:k"].values())) > first_expiry
    lease.release()
    await asyncio.sleep(0.01)
    assert redis.zsets["10xgraph:concurrency:k"] == {}
    await limiter.aclose()


@pytest.mark.asyncio
async def test_redis_outage_fails_open():
    redis = _FakeRedis()
    redis.fail = True
    limiter = RedisConcurrencyLimiter(redis)
    lease = await limiter.acquire("k", 1)
    assert lease is not None and lease.released  # allowed, nothing to release


@pytest.mark.asyncio
async def test_aclose_closes_the_client():
    redis = _FakeRedis()
    limiter = RedisConcurrencyLimiter(redis)
    await limiter.acquire("k", 5)
    await limiter.aclose()
    assert redis.closed


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def test_limiter_lookup_prefers_the_bound_one(monkeypatch):
    container = InjectQ()
    monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: container))
    assert concurrency.shared_concurrency_limiter() is None
    assert concurrency.concurrency_limiter() is concurrency._process_limiter
    shared = RedisConcurrencyLimiter(_FakeRedis())
    container.bind_instance(ConcurrencyLimiter, shared)
    assert concurrency.concurrency_limiter() is shared


def test_loader_binds_a_redis_limiter_when_redis_is_configured():
    from tenxgraph_api.src.app.loader import _bind_shared_concurrency

    container = InjectQ()
    _bind_shared_concurrency(container, None)
    assert container.try_get(ConcurrencyLimiter) is None
    _bind_shared_concurrency(container, "redis://localhost:6379/0")
    assert isinstance(container.try_get(ConcurrencyLimiter), RedisConcurrencyLimiter)

    mine = MemoryConcurrencyLimiter()
    other = InjectQ()
    other.bind_instance(ConcurrencyLimiter, mine)
    _bind_shared_concurrency(other, "redis://localhost:6379/0")
    assert other.try_get(ConcurrencyLimiter) is mine


@pytest.mark.asyncio
async def test_run_slots_use_the_shared_limiter(monkeypatch):
    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
    from tenxgraph_api.src.app.routers.graph import run_limits

    redis = _FakeRedis()
    shared = RedisConcurrencyLimiter(redis)
    monkeypatch.setattr(run_limits, "concurrency_limiter", lambda: shared)
    await run_limits.acquire_run_slot({"user_id": "alice"}, RunsConfig(max_concurrent_per_user=1))
    assert len(redis.zsets["10xgraph:concurrency:runs:user:alice"]) == 1
    await shared.aclose()


@pytest.mark.asyncio
async def test_websocket_caps_use_the_shared_limiter(monkeypatch):
    from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig
    from tenxgraph_api.src.app.routers.graph import realtime_guard

    redis = _FakeRedis()
    shared = RedisConcurrencyLimiter(redis)
    monkeypatch.setattr(realtime_guard.keying, "verified_user_id", lambda ws: "alice")
    ws_config = WebSocketConfig(max_connections=10, max_connections_per_user=1)
    websocket = SimpleNamespace()

    async with realtime_guard._shared_connection_slots(shared, websocket, ws_config):
        assert set(redis.zsets) == {
            "10xgraph:concurrency:ws:all",
            "10xgraph:concurrency:ws:user:alice",
        }
        with pytest.raises(WebSocketException):
            async with realtime_guard._shared_connection_slots(shared, websocket, ws_config):
                pass
        # The refused attempt gave its global slot back (released on the next loop turn).
        await asyncio.sleep(0)
        assert len(redis.zsets["10xgraph:concurrency:ws:all"]) == 1
    await asyncio.sleep(0)
    assert all(not members for members in redis.zsets.values())
    await shared.aclose()


@pytest.mark.asyncio
async def test_scheduled_releases_are_referenced_until_done():
    done = asyncio.Event()

    async def release():
        await asyncio.sleep(0)
        done.set()

    Lease(release).release()
    assert len(concurrency._pending_releases) == 1
    await asyncio.wait_for(done.wait(), 1)
    await asyncio.sleep(0)
    assert len(concurrency._pending_releases) == 0
