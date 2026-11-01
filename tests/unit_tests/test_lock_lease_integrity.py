"""Lease integrity behind the thread lock (G-91, G-94)."""

import asyncio

import pytest

from tenxgraph_api.src.app.core import leases
from tenxgraph_api.src.app.core.leases import LeaseManager

from .test_concurrency_limiter import RecordingStore


KEY = "concurrency:runs:thread:t1"


class _SlowAcquireStore(RecordingStore):
    """A store that grants the slot, then takes a while to answer, so a caller can be
    cancelled after the slot was taken."""

    async def acquire(self, key, limit, ttl):
        result = await super().acquire(key, limit, ttl)  # the server applied it ...
        await asyncio.sleep(0.2)  # ... but the reply is slow
        return result


class _StrictCloseStore(RecordingStore):
    """Refuses calls once closed, like a real client."""

    async def release(self, key, slot_id):
        await asyncio.sleep(0.01)
        if self.closed:
            raise ConnectionError("client closed")
        await super().release(key, slot_id)


@pytest.mark.asyncio
async def test_cache_store_outage_keeps_one_run_per_thread_in_this_process():
    store = RecordingStore()
    store.fail = True
    manager = LeaseManager(store)
    first = await manager.acquire("runs:thread:t1", 1)
    assert first is not None
    assert await manager.acquire("runs:thread:t1", 1) is None
    first.release()
    assert await manager.acquire("runs:thread:t1", 1) is not None
    await manager.aclose()


@pytest.mark.asyncio
async def test_lost_lease_is_detected_and_restored():
    store = RecordingStore()
    manager = LeaseManager(store, lease_ttl=0.06)
    lease = await manager.acquire("runs:thread:t1", 1)
    assert lease is not None and not lease.lost
    await store.inner.delete(KEY)  # evicted, or expired during a stall
    await asyncio.sleep(0.05)
    assert lease.lost
    assert store.slots(KEY) == 1  # held again, so a second run is still refused
    other = LeaseManager(store, lease_ttl=0.06)
    assert await other.acquire("runs:thread:t1", 1) is None
    lease.release()
    await asyncio.sleep(0.01)
    assert store.slots(KEY) == 0  # the slot taken again is the one released
    await manager.aclose()


@pytest.mark.asyncio
async def test_a_cancelled_waiter_leaves_no_ghost_lease():
    store = _SlowAcquireStore()
    manager = LeaseManager(store, lease_ttl=60)
    waiter = asyncio.create_task(manager.acquire("runs:thread:t1", 1))
    await asyncio.sleep(0.05)
    assert store.slots(KEY) == 1  # granted on the server, the reply still on its way
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0.3)
    assert store.slots(KEY) == 0
    assert manager._held == {}
    await manager.aclose()


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_cancelled_waiters_release():
    store = _SlowAcquireStore()
    manager = LeaseManager(store, lease_ttl=60)
    waiter = asyncio.create_task(manager.acquire("runs:thread:t1", 1))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await manager.aclose()  # the release of the late grant runs before the store closes
    assert store.slots(KEY) == 0
    assert not leases._pending_releases


@pytest.mark.asyncio
async def test_aclose_waits_for_pending_releases_before_closing_the_store():
    store = _StrictCloseStore()
    manager = LeaseManager(store, lease_ttl=60)
    lease = await manager.acquire("runs:thread:t1", 1)
    assert lease is not None
    lease.release()  # scheduled, not yet run
    await manager.aclose()
    await store.aclose()  # what the lifespan does next
    assert store.slots(KEY) == 0
    assert store.closed
    assert not leases._pending_releases
    assert not manager._renewers


@pytest.mark.asyncio
async def test_aclose_stops_renewing_leases_still_held():
    store = RecordingStore()
    manager = LeaseManager(store, lease_ttl=0.06)
    assert await manager.acquire("runs:thread:t1", 1) is not None
    await manager.aclose()
    renewals = len(store.ops("renew"))
    await asyncio.sleep(0.05)
    assert len(store.ops("renew")) == renewals
