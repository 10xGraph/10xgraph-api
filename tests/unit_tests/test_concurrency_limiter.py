"""Leases on the cache store: shared run / WebSocket caps, fail open, wiring and selection."""

import asyncio
import logging
import sys
import textwrap
from types import SimpleNamespace

import pytest
from fastapi import WebSocketException
from injectq import InjectQ

from tenxgraph_api.src.app.core import leases
from tenxgraph_api.src.app.core.cache_store import (
    BaseCacheStore,
    InMemoryCacheStore,
    RedisCacheStore,
)
from tenxgraph_api.src.app.core.cache_store import loader as cache_loader
from tenxgraph_api.src.app.core.leases import Lease, LeaseManager


@pytest.fixture(autouse=True)
def _app_logs_reach_caplog(monkeypatch):
    """The app's log setup (run by any test that builds the app) stops ``tenxgraph_api``
    from propagating; caplog listens on the root logger."""
    monkeypatch.setattr(logging.getLogger("tenxgraph_api"), "propagate", True)


class RecordingStore(BaseCacheStore):
    """An ``InMemoryCacheStore`` that records every call and can be switched to failing.

    One instance stands for one shared server: give it to two ``LeaseManager`` objects to
    model two workers.
    """

    def __init__(self) -> None:
        self.inner = InMemoryCacheStore()
        self.calls: list[tuple] = []
        self.fail = False
        self.closed = False

    def _record(self, *call) -> None:
        self.calls.append(call)
        if self.fail:
            raise ConnectionError("cache store down")

    def ops(self, name: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == name]

    async def aping(self) -> None:
        self._record("aping")

    async def now(self) -> float:
        self._record("now")
        return await self.inner.now()

    async def acquire(self, key, limit, ttl):
        self._record("acquire", key)
        return await self.inner.acquire(key, limit, ttl)

    async def renew(self, key, slot_id, ttl):
        self._record("renew", key)
        return await self.inner.renew(key, slot_id, ttl)

    async def release(self, key, slot_id):
        self._record("release", key)
        await self.inner.release(key, slot_id)

    async def get(self, key):
        self._record("get", key)
        return await self.inner.get(key)

    async def set(self, key, value, ttl, mode="always"):
        self._record("set", key, ttl)
        return await self.inner.set(key, value, ttl, mode)

    async def delete(self, key):
        self._record("delete", key)
        await self.inner.delete(key)

    def slots(self, key: str) -> int:
        """Live slots held under the store key ``key``."""
        slot_set = self.inner._slots.get(key)
        return len(slot_set.expiries) if slot_set is not None else 0

    async def aclose(self) -> None:
        self.closed = True


async def _settle():
    """Let scheduled releases run."""
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# leases in one process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_manager_counts_per_key():
    manager = LeaseManager(InMemoryCacheStore())
    first = await manager.acquire("k", 2)
    assert await manager.acquire("k", 2) is not None
    assert await manager.acquire("k", 2) is None
    assert await manager.acquire("other", 2) is not None
    first.release()
    first.release()  # idempotent
    assert manager.held("k") == 1
    # The slot given back is free at once for the next caller in this process.
    assert await manager.acquire("k", 2) is not None
    assert await manager.acquire("k", 2) is None
    await manager.aclose()


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


@pytest.mark.asyncio
async def test_scheduled_releases_are_referenced_until_done():
    done = asyncio.Event()

    async def release():
        await asyncio.sleep(0)
        done.set()

    Lease(release).release()
    assert len(leases._pending_releases) == 1
    await asyncio.wait_for(done.wait(), 1)
    await asyncio.sleep(0)
    assert len(leases._pending_releases) == 0


# ---------------------------------------------------------------------------
# one cap across workers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_workers_share_one_cap():
    store = RecordingStore()
    worker_a = LeaseManager(store, lease_ttl=60)
    worker_b = LeaseManager(store, lease_ttl=60)

    lease_a = await worker_a.acquire("runs:user:alice", 2)
    lease_b = await worker_b.acquire("runs:user:alice", 2)
    assert lease_a is not None and lease_b is not None
    # A per-process count would allow a third (one each) -- the shared one does not.
    assert await worker_a.acquire("runs:user:alice", 2) is None
    assert await worker_b.acquire("runs:user:alice", 2) is None
    assert store.slots("concurrency:runs:user:alice") == 2  # a denial adds nothing

    lease_a.release()
    await _settle()
    assert await worker_b.acquire("runs:user:alice", 2) is not None
    await worker_a.aclose()
    await worker_b.aclose()


@pytest.mark.asyncio
async def test_a_dead_workers_lease_expires():
    store = InMemoryCacheStore()
    dead = LeaseManager(store, lease_ttl=0.2)
    alive = LeaseManager(store, lease_ttl=0.2)
    assert await dead.acquire("k", 1) is not None
    for task in list(dead._renewers):  # the worker dies: no renewal, no release
        task.cancel()
    assert await alive.acquire("k", 1) is None
    await asyncio.sleep(0.25)
    assert await alive.acquire("k", 1) is not None
    await alive.aclose()


@pytest.mark.asyncio
async def test_a_held_lease_is_renewed_and_released():
    store = RecordingStore()
    manager = LeaseManager(store, lease_ttl=0.06)
    lease = await manager.acquire("k", 1)
    await asyncio.sleep(0.15)  # well past the ttl
    assert store.ops("renew")
    assert store.slots("concurrency:k") == 1
    assert not lease.lost
    lease.release()
    await _settle()
    assert store.slots("concurrency:k") == 0
    renewals = len(store.ops("renew"))
    await asyncio.sleep(0.05)
    assert len(store.ops("renew")) == renewals  # renewal stopped with the release
    await manager.aclose()


# ---------------------------------------------------------------------------
# fail open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_store_outage_fails_open(caplog):
    store = RecordingStore()
    store.fail = True
    manager = LeaseManager(store)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.concurrency"):
        lease = await manager.acquire("k", 1)
    assert lease is not None  # allowed, counted in this process only
    assert "Shared concurrency limit unavailable" in caplog.text
    assert await manager.acquire("k", 1) is None  # still one holder at a time per process
    lease.release()
    assert await manager.acquire("k", 1) is not None
    await manager.aclose()


@pytest.mark.asyncio
async def test_a_fallback_lease_is_released_in_the_fallback():
    store = RecordingStore()
    manager = LeaseManager(store)
    store.fail = True
    lease = await manager.acquire("k", 1)
    store.fail = False  # the store is back while the lease is held
    # This process still holds the slot, so the shared store must not grant a second one.
    assert await manager.acquire("k", 1) is None
    await _settle()
    assert store.slots("concurrency:k") == 0  # the shared slot taken meanwhile was given back
    lease.release()
    await _settle()
    assert manager.fallback._slots == {}
    assert await manager.acquire("k", 1) is not None
    await manager.aclose()


@pytest.mark.asyncio
async def test_a_shared_lease_counts_against_the_fallback():
    store = RecordingStore()
    manager = LeaseManager(store)
    lease = await manager.acquire("runs:thread:t1", 1)
    store.fail = True  # the store goes down while the run holds the lock
    assert await manager.acquire("runs:thread:t1", 1) is None  # no second run in this process
    store.fail = False
    lease.release()
    await manager.aclose()


@pytest.mark.asyncio
async def test_store_failing_mid_run_keeps_the_lease(caplog):
    store = RecordingStore()
    manager = LeaseManager(store, lease_ttl=0.06)
    lease = await manager.acquire("runs:thread:t1", 1)
    store.fail = True
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.concurrency"):
        await asyncio.sleep(0.05)
    assert "Lease refresh failed" in caplog.text
    assert not lease.released  # the run goes on
    lease.release()  # the release fails quietly; the slot expires on its own
    await _settle()
    await manager.aclose()


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def test_lease_manager_lookup_prefers_the_bound_one(monkeypatch):
    container = InjectQ()
    monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: container))
    assert leases.lease_manager() is leases.process_lease_manager()
    store = RecordingStore()
    cache_loader.bind_cache_store(container, store)
    bound = leases.lease_manager()
    assert bound is container.try_get(LeaseManager)
    assert bound.cache is store
    assert container.try_get(BaseCacheStore) is store


@pytest.mark.asyncio
async def test_run_slots_use_the_lease_manager(monkeypatch):
    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
    from tenxgraph_api.src.app.routers.graph import run_limits

    store = RecordingStore()
    manager = LeaseManager(store)
    monkeypatch.setattr(run_limits, "lease_manager", lambda: manager)
    await run_limits.acquire_run_slot({"user_id": "alice"}, RunsConfig(max_concurrent_per_user=1))
    assert store.slots("concurrency:runs:user:alice") == 1
    await manager.aclose()


@pytest.mark.asyncio
async def test_websocket_caps_use_the_lease_manager(monkeypatch):
    from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig
    from tenxgraph_api.src.app.routers.graph import realtime_guard

    store = RecordingStore()
    manager = LeaseManager(store)
    monkeypatch.setattr(realtime_guard.keying, "verified_user_id", lambda ws: "alice")
    ws_config = WebSocketConfig(max_connections=10, max_connections_per_user=1)
    websocket = SimpleNamespace()

    async with realtime_guard._connection_slots(manager, websocket, ws_config):
        assert set(store.inner._slots) == {"concurrency:ws:all", "concurrency:ws:user:alice"}
        with pytest.raises(WebSocketException):
            async with realtime_guard._connection_slots(manager, websocket, ws_config):
                pass
        # The refused attempt gave its global slot back.
        await _settle()
        assert store.slots("concurrency:ws:all") == 1
    await _settle()
    assert store.inner._slots == {}
    await manager.aclose()


@pytest.mark.asyncio
async def test_websocket_caps_hold_per_process_without_a_shared_store(monkeypatch):
    from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig
    from tenxgraph_api.src.app.routers.graph import realtime_guard

    manager = LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(realtime_guard.keying, "verified_user_id", lambda ws: None)
    ws_config = WebSocketConfig(max_connections=1, max_connections_per_user=1)
    async with realtime_guard._connection_slots(manager, SimpleNamespace(), ws_config):
        assert manager.held("ws:all") == 1
        with pytest.raises(WebSocketException):
            async with realtime_guard._connection_slots(manager, SimpleNamespace(), ws_config):
                pass
    assert manager._held == {}
    await manager.aclose()


# ---------------------------------------------------------------------------
# selection (Section 5.0)
# ---------------------------------------------------------------------------


class _Config:
    def __init__(self, cache_store=None, redis_url=None):
        self.cache_store = cache_store
        self.redis_url = redis_url


@pytest.fixture
def cache_module(tmp_path, monkeypatch):
    """A project module with a ``cache_store`` instance and a factory."""
    (tmp_path / "my_cache.py").write_text(
        textwrap.dedent(
            """
            from tests.unit_tests.test_concurrency_limiter import RecordingStore

            instance = RecordingStore()

            def factory():
                return RecordingStore()

            not_a_store = object()
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "my_cache"
    sys.modules.pop("my_cache", None)


def test_selection_order(cache_module):
    redis_url = "redis://:hunter2@config-host:6380/0"
    env = SimpleNamespace(REDIS_URL="redis://:hunter2@env-host:6379/1")
    store, source = cache_loader.build_cache_store(
        _Config(cache_store=f"{cache_module}:instance", redis_url=redis_url), env
    )
    assert isinstance(store, RecordingStore)
    assert source == f"from cache_store {cache_module}:instance"

    store, source = cache_loader.build_cache_store(_Config(redis_url=redis_url), env)
    assert isinstance(store, RedisCacheStore) and source == "from redis in 10xgraph.json"
    assert store.describe() == "config-host:6380"

    store, source = cache_loader.build_cache_store(_Config(), env)
    assert isinstance(store, RedisCacheStore) and source == "from REDIS_URL"
    assert store.describe() == "env-host:6379"

    store, source = cache_loader.build_cache_store(_Config(), SimpleNamespace(REDIS_URL=None))
    assert isinstance(store, InMemoryCacheStore)
    assert source == "no cache_store, redis or REDIS_URL set"
    assert cache_loader.build_cache_store(None, SimpleNamespace(REDIS_URL=None))[1] == source


def test_cache_store_factory_and_bad_paths(cache_module):
    env = SimpleNamespace(REDIS_URL=None)
    store, _ = cache_loader.build_cache_store(_Config(cache_store=f"{cache_module}:factory"), env)
    assert isinstance(store, RecordingStore)
    with pytest.raises(Exception, match="not a BaseCacheStore"):
        cache_loader.build_cache_store(_Config(cache_store=f"{cache_module}:not_a_store"), env)
    with pytest.raises(Exception, match="missing"):
        cache_loader.build_cache_store(_Config(cache_store=f"{cache_module}:missing"), env)


def test_graph_config_cache_store_key(tmp_path):
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "a:b", "cache_store": "my_cache:instance"}')
    assert GraphConfig(str(path)).cache_store == "my_cache:instance"
    path.write_text('{"agent": "a:b"}')
    assert GraphConfig(str(path)).cache_store is None
    path.write_text('{"agent": "a:b", "cache_store": "redis"}')
    with pytest.raises(ValueError, match="module:attr"):
        _ = GraphConfig(str(path)).cache_store


def test_boot_logs_one_info_line_without_credentials(caplog):
    container = InjectQ()
    env = SimpleNamespace(REDIS_URL="redis://user:hunter2@cache.internal:6379/0")
    with caplog.at_level(logging.INFO, logger="tenxgraph_api.cache_store"):
        store = cache_loader.load_and_bind_cache_store(container, _Config(), env)
    lines = [r.getMessage() for r in caplog.records if r.name == "tenxgraph_api.cache_store"]
    assert lines == ["Cache store: RedisCacheStore (from REDIS_URL, cache.internal:6379)"]
    assert "hunter2" not in caplog.text and "user" not in lines[0]
    assert container.try_get(BaseCacheStore) is store

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="tenxgraph_api.cache_store"):
        cache_loader.load_and_bind_cache_store(InjectQ(), _Config(), SimpleNamespace())
    assert caplog.records[-1].getMessage() == (
        "Cache store: InMemoryCacheStore (no cache_store, redis or REDIS_URL set)"
    )


@pytest.mark.asyncio
async def test_custom_cache_store_backs_locks_caps_and_idempotency(cache_module, monkeypatch):
    """A ``cache_store: "module:attr"`` store is what every lease and record goes through."""
    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
    from tenxgraph_api.src.app.routers.graph import idempotency
    from tenxgraph_api.src.app.routers.graph.run_guard import acquire_run_guard

    container = InjectQ()
    monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: container))
    store = cache_loader.load_and_bind_cache_store(
        container, _Config(cache_store=f"{cache_module}:factory"), SimpleNamespace()
    )
    assert isinstance(store, RecordingStore)

    guard = await acquire_run_guard(
        user={"user_id": "alice"}, runs=RunsConfig(max_concurrent_per_user=2), thread_id="t1"
    )
    run, _ = await idempotency.check({"user_id": "alice"}, "k1", "invoke", {})
    await run.complete("{}")
    run.release()
    guard.release()
    await _settle()

    acquired = {call[1] for call in store.ops("acquire")}
    assert acquired == {
        "concurrency:runs:thread:t1",
        "concurrency:runs:user:alice",
        "concurrency:runs:idem:alice:k1",
    }
    assert store.ops("set") == [("set", "idempotency:alice:k1", 86400)]
    assert {call[1] for call in store.ops("release")} == acquired
    await container.get(LeaseManager).aclose()


@pytest.mark.asyncio
async def test_custom_cache_store_backs_ownership_revocation_and_rate_limits(cache_module):
    """The same custom store serves the ownership cache, token revocation and rate limits."""
    from unittest.mock import AsyncMock, MagicMock

    from tenxgraph_api.src.app import loader
    from tenxgraph_api.src.app.core.auth import revocation
    from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
    from tenxgraph_api.src.app.core.middleware.rate_limit import limiter

    container = InjectQ()
    store = cache_loader.load_and_bind_cache_store(
        container, _Config(cache_store=f"{cache_module}:factory"), SimpleNamespace()
    )
    assert isinstance(store, RecordingStore)

    checkpointer = MagicMock()
    checkpointer.aget_thread_owner = AsyncMock(return_value="alice")
    loader.load_and_bind_authorization(container, "ownership")
    authz = container.get(AuthorizationBackend)
    authz._checkpointer = checkpointer
    assert await authz.authorize({"user_id": "alice"}, "graph", "invoke", resource_id="t1")
    await authz.evict("t1", "alice")

    config = MagicMock(spec=GraphConfig)
    config.token_revocation = revocation.TokenRevocationConfig()
    loader._enable_token_revocation(container, config, True)
    settings = container.get(revocation.TokenRevocationConfig)
    await revocation.revoke_user(store, "mallory", before=100.0)
    bound = limiter.bound_cache_store(container)
    assert bound is store
    assert await revocation.token_revoked({"user_id": "mallory", "iat": 50}, bound, settings)

    assert (await limiter.check(bound, "ip:1.2.3.4", 5, 60)).allowed

    keys = {call[1] for call in store.calls if len(call) > 1}
    assert {
        "owner:id:t1",
        "owner:tomb:t1",
        "revoked:user:mallory",
        "ratelimit:ip:1.2.3.4",
    } <= keys
    assert ("delete", "owner:id:t1") in store.calls
