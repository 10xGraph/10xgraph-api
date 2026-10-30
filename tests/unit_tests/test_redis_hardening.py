"""Redis client limits (G-92), Redis clock (G-131) and revocation fixes (G-117, G-167)."""

# ruff: noqa: S101

import time
from typing import Any

import pytest

from tenxgraph_api.src.app.core import redis_client
from tenxgraph_api.src.app.core.auth import revocation
from tenxgraph_api.src.app.core.auth.revocation import (
    DEFAULT_USER_REVOCATION_TTL,
    InMemoryTokenRevocation,
    RedisTokenRevocation,
)
from tenxgraph_api.src.app.core.concurrency import RedisConcurrencyLimiter
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
    built = []
    monkeypatch.setattr(
        redis_client, "create_redis", lambda url, **kw: built.append(url) or object()
    )
    RedisConcurrencyLimiter.from_url("redis://a")
    RedisTokenRevocation("redis://b")
    from tenxgraph_api.src.app.core.middleware.rate_limit.redis import RedisRateLimitBackend

    class _Client:
        def register_script(self, _):
            return None

    monkeypatch.setattr(
        redis_client, "create_redis", lambda url, **kw: built.append(url) or _Client()
    )
    RedisRateLimitBackend.from_url("redis://c", "p")
    from tenxgraph_api.src.app.loader import _build_ownership_redis

    _build_ownership_redis("redis://d")
    assert built == ["redis://a", "redis://b", "redis://c", "redis://d"]


# ---------------------------------------------------------------------------
# G-131: lease times come from the Redis clock
# ---------------------------------------------------------------------------


class _SkewedRedis:
    """Redis whose clock is an hour ahead of this process."""

    SKEW = 3600.0

    def __init__(self):
        self.scores: list[float] = []

    async def time(self):
        now = time.time() + self.SKEW
        return int(now), int((now % 1) * 1_000_000)

    def pipeline(self, transaction=True):
        scores = self.scores

        class _Pipe:
            def zremrangebyscore(self, key, low, high):
                self.high = high

            def zadd(self, key, mapping):
                scores.extend(mapping.values())

            def zcard(self, key):
                pass

            def expire(self, key, seconds):
                pass

            async def execute(self):
                return [None, 1, 1, True]

        return _Pipe()

    async def zrem(self, key, member):
        pass


@pytest.mark.asyncio
async def test_lease_expiry_uses_the_redis_clock():
    client = _SkewedRedis()
    limiter = RedisConcurrencyLimiter(client, lease_ttl=60.0)
    lease = await limiter.acquire("k", 1)
    assert lease is not None
    # With the pod's own clock the score would be about now + 60; the Redis clock is +3600.
    assert client.scores[0] > time.time() + 3000
    lease.release()
    await limiter.aclose()


# ---------------------------------------------------------------------------
# G-117 / G-167: revocation
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, Any] = {}
        self.ttl: dict[str, Any] = {}
        self.mget_calls = 0

    async def time(self):
        now = time.time()
        return int(now), int((now % 1) * 1_000_000)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl[key] = ex

    async def get(self, key):
        return self.data.get(key)

    async def mget(self, keys):
        self.mget_calls += 1
        raise AssertionError("MGET over two keys fails with CROSSSLOT on Redis Cluster")

    async def eval(self, script, numkeys, key, cutoff, ttl):
        new = float(cutoff) if cutoff else time.time()
        current = self.data.get(key)
        if current is not None and float(current) >= new:
            return 0
        self.data[key] = repr(new).encode()
        self.ttl[key] = int(ttl) or None
        return 1


@pytest.mark.asyncio
async def test_check_does_not_use_mget():
    client = _FakeRedis()
    backend = RedisTokenRevocation(client=client, prefix="p")
    await backend.revoke_user("a", before=100.0)
    assert await backend.is_revoked({"user_id": "a", "jti": "x", "iat": 50}) is True
    assert client.mget_calls == 0


@pytest.mark.asyncio
async def test_an_older_cutoff_does_not_replace_a_newer_one():
    client = _FakeRedis()
    backend = RedisTokenRevocation(client=client, prefix="p")
    await backend.revoke_user("a", before=2000.0)
    await backend.revoke_user("a", before=1000.0)
    assert await backend.is_revoked({"user_id": "a", "iat": 1500}) is True


def test_memory_older_cutoff_does_not_replace_a_newer_one():
    backend = InMemoryTokenRevocation()
    backend.revoke_user("a", before=2000.0)
    backend.revoke_user("a", before=1000.0)
    assert backend._users["a"] == 2000.0


@pytest.mark.asyncio
async def test_user_entries_expire_by_default():
    client = _FakeRedis()
    backend = RedisTokenRevocation(client=client, prefix="p")
    await backend.revoke_user("a")
    assert client.ttl["p:user:a"] == DEFAULT_USER_REVOCATION_TTL


@pytest.mark.asyncio
async def test_token_without_iat_is_dated_from_exp_when_the_lifetime_is_known():
    now = time.time()
    backend = InMemoryTokenRevocation(token_lifetime=3600)
    backend.revoke_user("a", before=now)
    old = {"user_id": "a", "exp": now + 100}  # minted about 3500 s ago
    new = {"user_id": "a", "exp": now + 3600 + 100}  # minted after the revoke
    assert await backend.is_revoked(old) is True
    assert await backend.is_revoked(new) is False
    # Without a lifetime an undated token cannot be told apart and stays revoked.
    plain = InMemoryTokenRevocation()
    plain.revoke_user("a", before=now)
    assert await plain.is_revoked(new) is True


@pytest.mark.asyncio
async def test_exported_helpers_revoke_through_the_backend():
    backend = InMemoryTokenRevocation()
    await revocation.revoke_token("j1", backend=backend)
    await revocation.revoke_user("u1", before=time.time(), backend=backend)
    assert await backend.is_revoked({"jti": "j1"}) is True
    assert await backend.is_revoked({"user_id": "u1", "iat": 1}) is True

    redis = RedisTokenRevocation(client=_FakeRedis(), prefix="p")
    await revocation.revoke_token("j2", backend=redis)
    assert await redis.is_revoked({"jti": "j2"}) is True


@pytest.mark.asyncio
async def test_helpers_need_a_backend(monkeypatch):
    monkeypatch.setattr(revocation, "_bound_backend", lambda: None)
    with pytest.raises(RuntimeError, match="token_revocation"):
        await revocation.revoke_user("u")
