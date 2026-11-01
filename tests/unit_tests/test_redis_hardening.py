"""Redis client limits (G-92), Redis clock (G-131) and revocation fixes (G-117, G-167)."""

import time
from typing import Any

import pytest

from tenxgraph_api.src.app.core import redis_client
from tenxgraph_api.src.app.core.auth import revocation
from tenxgraph_api.src.app.core.auth.revocation import DEFAULT_USER_REVOCATION_TTL
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore, redis_store
from tenxgraph_api.src.app.core.cache_store.redis_store import RedisCacheStore
from tenxgraph_api.src.app.core.config.settings import get_settings


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    for name in (
        "REDIS_SOCKET_TIMEOUT",
        "REDIS_CONNECT_TIMEOUT",
        "REDIS_MAX_CONNECTIONS",
        "REDIS_POOL_TIMEOUT",
        "REDIS_HEALTH_CHECK_INTERVAL",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# G-92: timeouts and a bounded pool on every client
# ---------------------------------------------------------------------------


def test_default_client_has_timeouts_and_a_bounded_pool():
    client = redis_client.create_redis("redis://localhost:6379/0")
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 5.0
    assert kwargs["socket_connect_timeout"] == 5.0
    assert kwargs["health_check_interval"] == 30.0
    assert client.connection_pool.max_connections == 50


def test_client_limits_are_configurable(monkeypatch):
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT", "1.5")
    monkeypatch.setenv("REDIS_CONNECT_TIMEOUT", "2")
    monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "7")
    get_settings.cache_clear()
    client = redis_client.create_redis("redis://localhost:6379/0")
    assert client.connection_pool.connection_kwargs["socket_timeout"] == 1.5
    assert client.connection_pool.connection_kwargs["socket_connect_timeout"] == 2.0
    assert client.connection_pool.max_connections == 7


def test_zero_turns_a_limit_off(monkeypatch):
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT", "0")
    monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "0")
    get_settings.cache_clear()
    client = redis_client.create_redis("redis://localhost:6379/0")
    assert "socket_timeout" not in client.connection_pool.connection_kwargs or (
        client.connection_pool.connection_kwargs["socket_timeout"] is None
    )


def test_every_api_redis_client_uses_the_shared_builder(monkeypatch):
    """The cache store is the API's only Redis client (locks, caps, idempotency, ownership,
    revocation and rate limits all go through it)."""
    built = []
    monkeypatch.setattr(
        redis_client, "create_redis", lambda url, **kw: built.append(url) or object()
    )
    monkeypatch.setattr(redis_store, "create_redis", redis_client.create_redis)
    RedisCacheStore.from_url("redis://a")
    assert built == ["redis://a"]


# ---------------------------------------------------------------------------
# G-131: lease times come from the Redis clock
# ---------------------------------------------------------------------------


def test_lease_expiry_uses_the_redis_clock():
    """Lease scores are computed inside the scripts from ``TIME``, never the pod's clock."""
    for script in (redis_store._ACQUIRE_LUA, redis_store._RENEW_LUA):
        assert "redis.call('TIME')" in script
        assert "now + tonumber(ARGV[2])" in script


# ---------------------------------------------------------------------------
# G-117 / G-167: revocation
# ---------------------------------------------------------------------------


class _RecordingStore(InMemoryCacheStore):
    """In-memory cache store that records TTLs and refuses multi-key reads."""

    def __init__(self):
        super().__init__()
        self.ttl: dict[str, Any] = {}
        self.modes: dict[str, str] = {}

    async def set(self, key, value, ttl, mode="always"):
        written = await super().set(key, value, ttl, mode)
        if written:
            self.ttl[key] = ttl
        self.modes[key] = mode
        return written

    async def mget(self, keys):
        raise AssertionError("MGET over two keys fails with CROSSSLOT on Redis Cluster")


@pytest.mark.asyncio
async def test_check_reads_two_keys_one_by_one():
    store = _RecordingStore()
    await revocation.revoke_user(store, "a", before=100.0)
    assert await revocation.is_revoked(store, {"user_id": "a", "jti": "x", "iat": 50}) is True


@pytest.mark.asyncio
async def test_an_older_cutoff_does_not_replace_a_newer_one():
    store = _RecordingStore()
    await revocation.revoke_user(store, "a", before=2000.0)
    await revocation.revoke_user(store, "a", before=1000.0)
    assert store.modes["revoked:user:a"] == "if_greater"
    assert await store.get("revoked:user:a") == b"2000.000000"
    assert await revocation.is_revoked(store, {"user_id": "a", "iat": 1500}) is True


@pytest.mark.asyncio
async def test_user_entries_expire_by_default():
    store = _RecordingStore()
    await revocation.revoke_user(store, "a")
    assert store.ttl["revoked:user:a"] == DEFAULT_USER_REVOCATION_TTL


@pytest.mark.asyncio
async def test_token_without_iat_is_dated_from_exp_when_the_lifetime_is_known():
    now = time.time()
    store = InMemoryCacheStore()
    await revocation.revoke_user(store, "a", before=now)
    old = {"user_id": "a", "exp": now + 100}  # minted about 3500 s ago
    new = {"user_id": "a", "exp": now + 3600 + 100}  # minted after the revoke
    assert await revocation.is_revoked(store, old, token_lifetime=3600) is True
    assert await revocation.is_revoked(store, new, token_lifetime=3600) is False
    # Without a lifetime an undated token cannot be told apart and stays revoked.
    assert await revocation.is_revoked(store, new) is True


@pytest.mark.asyncio
async def test_token_entry_expires_with_the_token():
    store = _RecordingStore()
    await revocation.revoke_token(store, "j1", expires_at=time.time() + 120)
    assert 1 <= store.ttl["revoked:jti:j1"] <= 120
    await revocation.revoke_token(store, "j2")
    assert store.ttl["revoked:jti:j2"] is None
    assert await revocation.is_revoked(store, {"jti": "j1"}) is True
    assert await revocation.is_revoked(store, {"jti": "j2"}) is True
