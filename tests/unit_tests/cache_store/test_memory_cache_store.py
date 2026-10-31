"""InMemoryCacheStore: caps, eviction, lease protection and the sweep."""

import asyncio
import logging

import pytest

from tenxgraph_api.src.app.core.cache_store import memory_store
from tenxgraph_api.src.app.core.cache_store.memory_store import InMemoryCacheStore


@pytest.fixture(autouse=True)
def _app_logs_reach_caplog(monkeypatch):
    """The app's log setup (run by any test that builds the app) stops ``tenxgraph_api``
    from propagating; caplog listens on the root logger."""
    monkeypatch.setattr(logging.getLogger("tenxgraph_api"), "propagate", True)


@pytest.mark.asyncio
async def test_slot_cap_evicts_least_recently_used_unprotected_key(caplog):
    store = InMemoryCacheStore(max_slot_keys=3)
    for name in ("a", "b", "c"):
        assert (await store.acquire(f"ratelimit:{name}", 5, 30)).granted
    await store.acquire("ratelimit:a", 5, 30)  # a is now the most recently used
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.cache_store"):
        await store.acquire("ratelimit:d", 5, 30)
    assert set(store._slots) == {"ratelimit:a", "ratelimit:c", "ratelimit:d"}
    assert "slot cap (3) reached" in caplog.text


@pytest.mark.asyncio
async def test_live_leases_are_never_evicted(caplog):
    store = InMemoryCacheStore(max_slot_keys=2)
    lock = await store.acquire("concurrency:runs:thread:1", 1, 30)
    cap = await store.acquire("concurrency:runs:user:u", 10, 30)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.cache_store"):
        await store.acquire("ratelimit:x", 5, 30)
    # Both leases survive; the table grows past the cap instead.
    assert len(store._slots) == 3
    assert "only live leases are left" in caplog.text
    assert not (await store.acquire("concurrency:runs:thread:1", 1, 30)).granted
    assert await store.renew("concurrency:runs:thread:1", lock.slot_id, 30)
    assert await store.renew("concurrency:runs:user:u", cap.slot_id, 30)
    # The next new key evicts the unprotected rate-limit entry, not a lease.
    await store.acquire("ratelimit:y", 5, 30)
    assert "ratelimit:x" not in store._slots
    assert "concurrency:runs:thread:1" in store._slots


@pytest.mark.asyncio
async def test_expired_lease_is_evicted_first():
    store = InMemoryCacheStore(max_slot_keys=2)
    await store.acquire("concurrency:old", 1, 0.05)
    await store.acquire("ratelimit:r", 5, 30)
    await asyncio.sleep(0.1)
    await store.acquire("ratelimit:new", 5, 30)
    assert set(store._slots) == {"ratelimit:r", "ratelimit:new"}


@pytest.mark.asyncio
async def test_value_cap_evicts_lru_but_keeps_revocations(caplog):
    store = InMemoryCacheStore(max_values=3)
    await store.set("revoked:jti:t1", b"1", 30)
    await store.set("idempotency:a", b"{}", 30)
    await store.set("idempotency:b", b"{}", 30)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.cache_store"):
        await store.set("idempotency:c", b"{}", 30)
    assert await store.get("revoked:jti:t1") == b"1"
    assert await store.get("idempotency:a") is None
    assert await store.get("idempotency:c") == b"{}"
    assert "key cap (3) reached" in caplog.text


@pytest.mark.asyncio
async def test_value_cap_grows_when_only_protected_keys_are_left(caplog):
    store = InMemoryCacheStore(max_values=2)
    await store.set("revoked:jti:1", b"1", 30)
    await store.set("revoked:jti:2", b"1", 30)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.cache_store"):
        await store.set("revoked:jti:3", b"1", 30)
    assert len(store._values) == 3
    assert "only protected keys are left" in caplog.text


@pytest.mark.asyncio
async def test_overwriting_a_key_at_the_cap_evicts_nothing():
    store = InMemoryCacheStore(max_values=2)
    await store.set("idempotency:a", b"1", 30)
    await store.set("idempotency:b", b"1", 30)
    await store.set("idempotency:a", b"2", 30)
    assert await store.get("idempotency:b") == b"1"


@pytest.mark.asyncio
async def test_cap_warning_is_rate_limited(caplog):
    store = InMemoryCacheStore(max_values=1)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.cache_store"):
        for n in range(5):
            await store.set(f"idempotency:{n}", b"1", 30)
    assert caplog.text.count("key cap (1) reached") == 1


@pytest.mark.asyncio
async def test_denied_acquire_on_a_missing_key_creates_nothing():
    store = InMemoryCacheStore()
    result = await store.acquire("ratelimit:none", 0, 30)
    assert not result.granted and result.count == 0 and result.oldest_expiry is None
    assert "ratelimit:none" not in store._slots


@pytest.mark.asyncio
async def test_release_of_last_slot_drops_the_key():
    store = InMemoryCacheStore()
    slot = await store.acquire("concurrency:k", 1, 30)
    await store.release("concurrency:k", slot.slot_id)
    assert "concurrency:k" not in store._slots


@pytest.mark.asyncio
async def test_sweep_removes_expired_entries(monkeypatch):
    monkeypatch.setattr(InMemoryCacheStore, "SWEEP_BATCH", 2)
    store = InMemoryCacheStore()
    for n in range(5):
        await store.acquire(f"ratelimit:{n}", 5, 0.05)
        await store.set(f"idempotency:{n}", b"1", 0.05)
    live = await store.acquire("concurrency:live", 1, 30)
    await store.set("idempotency:live", b"1", None)
    await asyncio.sleep(0.1)
    await store._sweep()
    assert set(store._slots) == {"concurrency:live"}
    assert set(store._values) == {"idempotency:live"}
    assert live.granted


@pytest.mark.asyncio
async def test_sweep_is_scheduled_by_operation_count(monkeypatch):
    monkeypatch.setattr(InMemoryCacheStore, "SWEEP_EVERY_OPS", 3)
    store = InMemoryCacheStore()
    await store.set("idempotency:x", b"1", 0.01)
    await asyncio.sleep(0.05)
    for _ in range(3):
        await store.get("idempotency:other")
    task = store._sweep_task
    assert task is not None
    await task
    assert "idempotency:x" not in store._values
    await store.aclose()
    assert store._sweep_task is None


@pytest.mark.asyncio
async def test_aclose_cancels_a_running_sweep():
    store = InMemoryCacheStore()
    gate = asyncio.Event()

    async def slow_sweep():
        await gate.wait()

    store._sweep_task = asyncio.get_running_loop().create_task(slow_sweep())
    await asyncio.sleep(0)
    await store.aclose()
    assert store._sweep_task is None


@pytest.mark.asyncio
async def test_if_greater_overwrites_a_stored_non_number():
    store = InMemoryCacheStore()
    await store.set("revoked:user:u", b"junk", 30)
    assert await store.set("revoked:user:u", b"5", 30, "if_greater")
    assert await store.get("revoked:user:u") == b"5"


@pytest.mark.asyncio
async def test_if_greater_writes_normalised_text():
    store = InMemoryCacheStore()
    assert await store.set("revoked:user:u", b" 7.5 ", None, "if_greater")
    assert await store.get("revoked:user:u") == b"7.5"


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan")])
async def test_bad_ttl_is_rejected(ttl):
    store = InMemoryCacheStore()
    with pytest.raises(ValueError):
        await store.acquire("ratelimit:k", 1, ttl)
    with pytest.raises(ValueError):
        await store.renew("ratelimit:k", "id", ttl)
    with pytest.raises(ValueError):
        await store.set("k", b"1", ttl)


@pytest.mark.asyncio
async def test_unknown_set_mode_is_rejected():
    store = InMemoryCacheStore()
    with pytest.raises(ValueError):
        await store.set("k", b"1", None, "sometimes")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_delete_removes_a_slot_set_too():
    store = InMemoryCacheStore()
    await store.acquire("concurrency:k", 1, 30)
    await store.delete("concurrency:k")
    assert (await store.acquire("concurrency:k", 1, 30)).granted


@pytest.mark.asyncio
async def test_many_renewals_keep_the_heap_small():
    store = InMemoryCacheStore()
    slot = await store.acquire("concurrency:k", 1, 30)
    for _ in range(200):
        assert await store.renew("concurrency:k", slot.slot_id, 30)
    assert len(store._slots["concurrency:k"].heap) < 40


def test_defaults_match_the_old_caps():
    assert memory_store.DEFAULT_MAX_SLOT_KEYS == 50_000
    assert memory_store.DEFAULT_MAX_VALUES == 10_000  # the old in-memory idempotency cap
    assert InMemoryCacheStore().describe() is None


@pytest.mark.asyncio
async def test_wall_clock_jump_does_not_expire_live_slots_or_keys(monkeypatch):
    """A host suspend moves wall time but not the monotonic clock the lease renewer runs on."""
    store = InMemoryCacheStore()
    held = await store.acquire("concurrency:runs:thread:1", 1, 30)
    assert held.granted
    await store.set("idempotency:k", b"v", 30)

    real_time = memory_store.time.time
    monkeypatch.setattr(memory_store.time, "time", lambda: real_time() + 3600)

    # The lock is still held: a second run on the same thread is refused.
    assert not (await store.acquire("concurrency:runs:thread:1", 1, 30)).granted
    assert await store.renew("concurrency:runs:thread:1", held.slot_id, 30)
    assert await store.get("idempotency:k") == b"v"
    await store._sweep()
    assert "concurrency:runs:thread:1" in store._slots
    assert "idempotency:k" in store._values

    # Reported times follow the store clock: epoch seconds, about now() + ttl.
    denied = await store.acquire("concurrency:runs:thread:1", 1, 30)
    assert abs(denied.oldest_expiry - (await store.now() + 30)) < 1.5
