# ruff: noqa: S101, PLR2004, SLF001

import logging
import time
from collections import OrderedDict

import pytest

from tenxgraph_api.src.app.core.middleware.rate_limit import MemoryRateLimitBackend


async def _hit(backend: MemoryRateLimitBackend, key: str, window: int = 60):
    return await backend.check(key, limit=5, window=window)


@pytest.mark.asyncio
async def test_cap_evicts_exactly_the_least_recently_used_key():
    backend = MemoryRateLimitBackend(max_unique_keys=3)
    for key in ("a", "b", "c"):
        await _hit(backend, key)

    await _hit(backend, "d")

    assert list(backend._buckets) == ["b", "c", "d"]


@pytest.mark.asyncio
async def test_recently_used_key_survives_eviction():
    backend = MemoryRateLimitBackend(max_unique_keys=3)
    for key in ("a", "b", "c"):
        await _hit(backend, key)
    await _hit(backend, "a")

    await _hit(backend, "d")

    assert "a" in backend._buckets
    assert "b" not in backend._buckets
    assert list(backend._buckets) == ["c", "a", "d"]


@pytest.mark.asyncio
async def test_decisions_keep_sliding_window_values():
    backend = MemoryRateLimitBackend()

    first = await backend.check("k", limit=2, window=60)
    second = await backend.check("k", limit=2, window=60)
    blocked = await backend.check("k", limit=2, window=60)

    assert (first.allowed, first.remaining, first.reset_after) == (True, 1, 60)
    assert (second.allowed, second.remaining, second.reset_after) == (True, 0, 60)
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert 1 <= blocked.reset_after <= 61


@pytest.mark.asyncio
async def test_sweep_removes_only_stale_keys_from_the_front():
    backend = MemoryRateLimitBackend()
    for key in ("old1", "old2", "fresh"):
        await _hit(backend, key)
    now = time.monotonic()
    backend._buckets["old1"].last_seen = now - 1000
    backend._buckets["old2"].last_seen = now - 1000

    await backend._sweep(60)

    assert list(backend._buckets) == ["fresh"]


@pytest.mark.asyncio
async def test_sweep_stops_at_first_fresh_key_without_scanning_the_rest():
    backend = MemoryRateLimitBackend()
    for key in ("old", "fresh", "later"):
        await _hit(backend, key)
    backend._buckets["old"].last_seen = time.monotonic() - 1000
    # A stale entry behind a fresh one is out of order and must not be reached.
    backend._buckets["later"].last_seen = time.monotonic() - 1000

    await backend._sweep(60)

    assert list(backend._buckets) == ["fresh", "later"]


@pytest.mark.asyncio
async def test_sweep_handles_more_than_one_batch():
    backend = MemoryRateLimitBackend()
    backend._SWEEP_BATCH = 10
    for i in range(35):
        await _hit(backend, f"k{i}")
    for entry in backend._buckets.values():
        entry.last_seen = time.monotonic() - 1000

    await backend._sweep(60)

    assert len(backend._buckets) == 0


@pytest.mark.asyncio
async def test_cap_warning_logged_once_per_minute(caplog):
    backend = MemoryRateLimitBackend(max_unique_keys=2)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.rate_limit"):
        for i in range(50):
            await _hit(backend, f"k{i}")

    warnings = [r for r in caplog.records if "bucket cap" in r.getMessage()]
    assert len(warnings) == 1

    backend._last_cap_warning = time.monotonic() - 61
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.rate_limit"):
        await _hit(backend, "again")
    warnings = [r for r in caplog.records if "bucket cap" in r.getMessage()]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_new_key_at_cap_does_not_scan_all_keys(monkeypatch):
    class Counting(OrderedDict):
        scans = 0

        def __iter__(self):
            type(self).scans += 1
            return super().__iter__()

        def values(self):
            type(self).scans += 1
            return super().values()

    backend = MemoryRateLimitBackend(max_unique_keys=5_000)
    backend._buckets = Counting()
    for i in range(5_000):
        await _hit(backend, f"k{i}")

    scans_before = Counting.scans
    for i in range(20):
        await _hit(backend, f"new{i}")

    assert len(backend._buckets) == 5_000
    assert "k0" not in backend._buckets
    assert Counting.scans == scans_before


@pytest.mark.asyncio
async def test_new_key_at_full_table_is_not_slower_than_an_existing_key():
    backend = MemoryRateLimitBackend(max_unique_keys=50_000)
    for i in range(50_000):
        await _hit(backend, f"k{i}")

    start = time.perf_counter()
    for i in range(500):
        await _hit(backend, f"new{i}")
    elapsed = time.perf_counter() - start

    # The old full scan cost about 2.5 ms per new key (1.25 s here).
    assert elapsed < 0.5
