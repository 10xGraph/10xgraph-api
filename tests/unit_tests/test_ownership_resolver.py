"""Unit tests for ThreadOwnershipResolver (L1 LRU + optional Redis L2)."""

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
    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.gets = 0
        self.sets = 0
        self.ttls: dict[str, int | None] = {}

    async def get(self, key):
        self.gets += 1
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.sets += 1
        self.ttls[key] = ex
        self.store[key] = value.encode() if isinstance(value, str) else value

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
    r = ThreadOwnershipResolver(lookup, redis=redis)

    assert await r.owner_of("t1") == "alice"  # DB -> fills L2 + L1
    assert redis.sets == 1

    # A fresh resolver sharing the same Redis resolves without touching the DB.
    lookup2 = _CountingLookup({"t1": "alice"})
    r2 = ThreadOwnershipResolver(lookup2, redis=redis)
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
    r = ThreadOwnershipResolver(lookup, redis=redis)

    await r.owner_of("t1")
    await r.evict("t1")
    assert list(redis.store) == ["af:authz:owner:deleted:t1"]  # owner cleared, tombstone left

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

        async def set(self, key, value, ex=None):
            raise RuntimeError("redis down")

        async def delete(self, key):
            raise RuntimeError("redis down")

    lookup = _CountingLookup({"t1": "alice"})
    r = ThreadOwnershipResolver(lookup, redis=_BrokenRedis())
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
    r = ThreadOwnershipResolver(_CountingLookup({"t1": "alice"}), redis=redis, l2_ttl=600)
    await r.owner_of("t1")
    assert list(redis.ttls.values()) == [600]


# ---------------------------------------------------------------------------
# delete tombstones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_leaves_a_tombstone_for_one_l1_ttl():
    clock = [1000.0]
    r = ThreadOwnershipResolver(_CountingLookup({}), l1_ttl=30.0)
    import tenxgraph_api.src.app.core.auth.ownership_resolver as mod

    original = mod.time.monotonic
    mod.time.monotonic = lambda: clock[0]
    try:
        assert await r.recently_deleted("t1") is False
        await r.evict("t1")
        assert await r.recently_deleted("t1") is True
        clock[0] += 29.0
        assert await r.recently_deleted("t1") is True
        clock[0] += 2.0  # past the L1 TTL: every worker's stale entry has expired
        assert await r.recently_deleted("t1") is False
        assert "t1" not in r._tombstones
    finally:
        mod.time.monotonic = original


@pytest.mark.asyncio
async def test_tombstone_reaches_other_workers_through_redis():
    redis = _FakeRedis()
    deleting = ThreadOwnershipResolver(_CountingLookup({}), redis=redis, l1_ttl=30.0)
    other = ThreadOwnershipResolver(_CountingLookup({}), redis=redis, l1_ttl=30.0)
    await deleting.evict("t1")
    assert await other.recently_deleted("t1") is True
    assert redis.ttls["af:authz:owner:deleted:t1"] == 30


@pytest.mark.asyncio
async def test_tombstones_are_bounded():
    r = ThreadOwnershipResolver(_CountingLookup({}), max_size=3)
    for i in range(10):
        await r.evict(f"t{i}")
    assert list(r._tombstones) == ["t7", "t8", "t9"]


@pytest.mark.asyncio
async def test_tombstone_read_errors_degrade_to_not_deleted():
    class _Broken(_FakeRedis):
        async def get(self, key):
            raise ConnectionError("down")

    r = ThreadOwnershipResolver(_CountingLookup({}), redis=_Broken())
    assert await r.recently_deleted("t1") is False


@pytest.mark.asyncio
async def test_a_just_deleted_thread_cannot_be_recreated_by_anyone():
    from tenxgraph_api.src.app.core.auth.authorization import OwnershipAuthorizationBackend

    resolver = ThreadOwnershipResolver(_CountingLookup({}), l1_ttl=30.0)
    backend = OwnershipAuthorizationBackend(checkpointer=object(), resolver=resolver)
    await backend.evict("t1")
    for user in ({"user_id": "alice"}, {"user_id": "bob"}):
        assert await backend.authorize(user, "graph", "invoke", resource_id="t1") is False
        assert await backend.authorize(user, "checkpointer", "write", resource_id="t1") is False
    assert await backend.authorize({"user_id": "bob"}, "graph", "invoke", resource_id="t2")


@pytest.mark.asyncio
async def test_the_deleting_owner_may_recreate_the_id_but_nobody_else():
    r = ThreadOwnershipResolver(_CountingLookup({}), l1_ttl=30.0)
    await r.evict("t1", owner="alice")
    assert await r.recently_deleted("t1", "alice") is False
    assert await r.recently_deleted("t1", "bob") is True
    assert await r.recently_deleted("t1") is True  # unknown caller


@pytest.mark.asyncio
async def test_deleting_owner_falls_back_to_the_cached_owner():
    r = ThreadOwnershipResolver(_CountingLookup({"t1": "alice"}), l1_ttl=30.0)
    await r.owner_of("t1")
    await r.evict("t1")
    assert await r.recently_deleted("t1", "alice") is False
    assert await r.recently_deleted("t1", "bob") is True


@pytest.mark.asyncio
async def test_shared_tombstone_carries_the_deleting_owner():
    redis = _FakeRedis()
    deleting = ThreadOwnershipResolver(_CountingLookup({}), redis=redis)
    other = ThreadOwnershipResolver(_CountingLookup({}), redis=redis)
    await deleting.evict("t1", owner="alice")
    assert await other.recently_deleted("t1", "alice") is False
    assert await other.recently_deleted("t1", "bob") is True
    await deleting.evict("t2")  # owner unknown: blocks everyone
    assert await other.recently_deleted("t2", "alice") is True
