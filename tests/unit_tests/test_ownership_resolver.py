"""Unit tests for ThreadOwnershipResolver (L1 LRU + optional cache store L2)."""

from __future__ import annotations

import pytest

from tenxgraph_api.src.app.core.auth.ownership_resolver import ThreadOwnershipResolver


class _CountingLookup:
    """Async owner lookup that records how many times it was hit."""

    def __init__(self, owners: dict[str, str]):
        self.owners = owners
        self.calls = 0

    async def __call__(self, thread_id: str):
        self.calls += 1
        return self.owners.get(str(thread_id))


class _FakeRedis:
    """A shared cache store (only the key methods the resolver uses)."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.gets = 0
        self.sets = 0
        self.ttls: dict[str, float | None] = {}

    async def get(self, key):
        self.gets += 1
        return self.store.get(key)

    async def set(self, key, value, ttl=None, mode="always"):
        self.sets += 1
        self.ttls[key] = ttl
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.mark.asyncio
async def test_resolves_and_caches_owner_in_l1():
    lookup = _CountingLookup({"t1": "alice"})
    r = ThreadOwnershipResolver(lookup)

    assert await r.owner_of("t1") == "alice"
    assert await r.owner_of("t1") == "alice"
    assert await r.owner_of("t1") == "alice"
    # Only the first call hit the backing lookup; the rest were L1 hits.
    assert lookup.calls == 1


@pytest.mark.asyncio
async def test_negative_result_is_never_cached():
    lookup = _CountingLookup({})  # no owners
    r = ThreadOwnershipResolver(lookup)

    assert await r.owner_of("ghost") is None
    assert await r.owner_of("ghost") is None
    # Each miss must re-check: a nonexistent thread can become owned at any moment.
    assert lookup.calls == 2


@pytest.mark.asyncio
async def test_redis_l2_is_populated_and_promotes_to_l1():
    lookup = _CountingLookup({"t1": "alice"})
    redis = _FakeRedis()
    r = ThreadOwnershipResolver(lookup, cache=redis)

    assert await r.owner_of("t1") == "alice"  # DB -> fills L2 + L1
    assert redis.sets == 1

    # A fresh resolver sharing the same Redis resolves without touching the DB.
    lookup2 = _CountingLookup({"t1": "alice"})
    r2 = ThreadOwnershipResolver(lookup2, cache=redis)
    assert await r2.owner_of("t1") == "alice"
    assert lookup2.calls == 0  # served from L2
    # And it is now in r2's L1 too (no further Redis reads).
    gets_before = redis.gets
    assert await r2.owner_of("t1") == "alice"
    assert redis.gets == gets_before


@pytest.mark.asyncio
async def test_evict_clears_l1_and_l2():
    lookup = _CountingLookup({"t1": "alice"})
    redis = _FakeRedis()
    r = ThreadOwnershipResolver(lookup, cache=redis)

    await r.owner_of("t1")
    await r.evict("t1")
    assert list(redis.store) == ["owner:tomb:t1"]  # owner cleared, tombstone left

    await r.owner_of("t1")
    assert lookup.calls == 2  # had to re-resolve after eviction


@pytest.mark.asyncio
async def test_l1_lru_is_bounded():
    lookup = _CountingLookup({f"t{i}": f"u{i}" for i in range(10)})
    r = ThreadOwnershipResolver(lookup, max_size=3)

    for i in range(10):
        await r.owner_of(f"t{i}")

    # Only the 3 most-recent survive in L1; older ones must re-resolve.
    calls_before = lookup.calls
    await r.owner_of("t9")  # recent -> L1 hit
    assert lookup.calls == calls_before
    await r.owner_of("t0")  # evicted -> re-resolve
    assert lookup.calls == calls_before + 1


@pytest.mark.asyncio
async def test_redis_errors_degrade_to_lookup():
    class _BrokenRedis:
        async def get(self, key):
            raise RuntimeError("redis down")

        async def set(self, key, value, ttl=None, mode="always"):
            raise RuntimeError("redis down")

        async def delete(self, key):
            raise RuntimeError("redis down")

    lookup = _CountingLookup({"t1": "alice"})
    r = ThreadOwnershipResolver(lookup, cache=_BrokenRedis())
    # Redis failing must not fail the request; falls through to the DB lookup.
    assert await r.owner_of("t1") == "alice"


@pytest.mark.asyncio
async def test_not_implemented_propagates():
    async def _lookup(thread_id):
        raise NotImplementedError

    r = ThreadOwnershipResolver(_lookup)
    with pytest.raises(NotImplementedError):
        await r.owner_of("t1")


# ---------------------------------------------------------------------------
# L3: stale entries expire, so a delete on another worker is honoured eventually
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l1_entry_expires_after_its_ttl(monkeypatch):
    from tenxgraph_api.src.app.core.auth import ownership_resolver

    now = [1000.0]
    monkeypatch.setattr(ownership_resolver.time, "monotonic", lambda: now[0])
    lookup = _CountingLookup({"t1": "alice"})
    r = ThreadOwnershipResolver(lookup, l1_ttl=30)

    assert await r.owner_of("t1") == "alice"
    # Another worker deleted t1 and bob re-created it; this worker only sees the DB.
    lookup.owners["t1"] = "bob"
    now[0] += 29
    assert await r.owner_of("t1") == "alice"  # still inside the TTL
    now[0] += 2
    assert await r.owner_of("t1") == "bob"


@pytest.mark.asyncio
async def test_l2_entries_are_written_with_a_ttl():
    redis = _FakeRedis()
    r = ThreadOwnershipResolver(_CountingLookup({"t1": "alice"}), cache=redis, l2_ttl=600)
    await r.owner_of("t1")
    assert list(redis.ttls.values()) == [600]


@pytest.mark.asyncio
async def test_in_memory_cache_store_is_not_a_second_l1():
    from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore

    r = ThreadOwnershipResolver(_CountingLookup({"t1": "alice"}), cache=InMemoryCacheStore())
    assert r._cache is None
    assert await r.owner_of("t1") == "alice"
