# ruff: noqa: S101, PLR2004, SLF001
"""Rate limiting on the in-memory cache store: bounded memory with LRU eviction."""

import logging
import time
from collections import OrderedDict

import pytest

from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.middleware.rate_limit import check


async def _hit(cache: InMemoryCacheStore, key: str, window: float = 60):
    return await check(cache, key, 5, window)


def _clients(cache: InMemoryCacheStore) -> list[str]:
    return [key.removeprefix("ratelimit:") for key in cache._slots]


@pytest.mark.asyncio
async def test_cap_evicts_exactly_the_least_recently_used_key():
    cache = InMemoryCacheStore(max_slot_keys=3)
    for key in ("a", "b", "c"):
        await _hit(cache, key)

    await _hit(cache, "d")

    assert _clients(cache) == ["b", "c", "d"]


@pytest.mark.asyncio
async def test_recently_used_key_survives_eviction():
    cache = InMemoryCacheStore(max_slot_keys=3)
    for key in ("a", "b", "c"):
        await _hit(cache, key)
    await _hit(cache, "a")

    await _hit(cache, "d")

    assert _clients(cache) == ["c", "a", "d"]


@pytest.mark.asyncio
async def test_live_leases_are_not_evicted_for_rate_limit_keys():
    cache = InMemoryCacheStore(max_slot_keys=2)
    lease = await cache.acquire("concurrency:runs:thread:t1", 1, ttl=60)
    assert lease.granted
    for key in ("a", "b", "c"):
        await _hit(cache, key)

    assert "concurrency:runs:thread:t1" in cache._slots
    assert _clients(cache)[-1] == "c"


@pytest.mark.asyncio
async def test_decisions_keep_sliding_window_values():
    cache = InMemoryCacheStore()

    first = await check(cache, "k", 2, 60)
    second = await check(cache, "k", 2, 60)
    blocked = await check(cache, "k", 2, 60)

    assert (first.allowed, first.remaining, first.reset_after) == (True, 1, 60)
    assert (second.allowed, second.remaining, second.reset_after) == (True, 0, 60)
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert 1 <= blocked.reset_after <= 61


@pytest.mark.asyncio
async def test_sweep_removes_only_expired_windows():
    cache = InMemoryCacheStore()
    for key in ("old1", "old2"):
        await _hit(cache, key, window=0.05)
    await _hit(cache, "fresh")
    time.sleep(0.1)

    await cache._sweep()

    assert _clients(cache) == ["fresh"]


@pytest.mark.asyncio
async def test_sweep_handles_more_than_one_batch():
    cache = InMemoryCacheStore()
    cache.SWEEP_BATCH = 10
    for i in range(35):
        await _hit(cache, f"k{i}", window=0.05)
    time.sleep(0.1)

    await cache._sweep()

    assert len(cache._slots) == 0


@pytest.mark.asyncio
async def test_cap_warning_logged_once_per_minute(caplog):
    cache = InMemoryCacheStore(max_slot_keys=2)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.cache_store"):
        for i in range(50):
            await _hit(cache, f"k{i}")

    warnings = [r for r in caplog.records if "slot cap" in r.getMessage()]
    assert len(warnings) == 1

    cache._last_cap_warning["slot"] = time.monotonic() - 61
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.cache_store"):
        await _hit(cache, "again")
    warnings = [r for r in caplog.records if "slot cap" in r.getMessage()]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_new_key_at_cap_does_not_scan_all_keys():
    class Counting(OrderedDict):
        scans = 0

        def __iter__(self):
            type(self).scans += 1
            return super().__iter__()

        def items(self):
            type(self).scans += 1
            return super().items()

    cache = InMemoryCacheStore(max_slot_keys=5_000)
    cache.SWEEP_EVERY_OPS = 10**9  # keep the background sweep out of the count
    cache.SWEEP_INTERVAL_SECONDS = 10**9
    cache._slots = Counting()
    for i in range(5_000):
        await _hit(cache, f"k{i}")

    scans_before = Counting.scans
    for i in range(20):
        await _hit(cache, f"new{i}")

    assert len(cache._slots) == 5_000
    assert "ratelimit:k0" not in cache._slots
    # One iteration per eviction, and each stops at the first (least recently used) key.
    assert Counting.scans - scans_before <= 20


@pytest.mark.asyncio
async def test_new_key_at_full_table_is_not_slower_than_an_existing_key():
    cache = InMemoryCacheStore(max_slot_keys=50_000)
    for i in range(50_000):
        await _hit(cache, f"k{i}")

    start = time.perf_counter()
    for i in range(500):
        await _hit(cache, f"new{i}")
    elapsed = time.perf_counter() - start

    # A full scan per new key would cost about 2.5 ms each (1.25 s here).
    assert elapsed < 0.5
