# ruff: noqa: S101, PLR2004, SLF001

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from injectq import InjectQ

from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig
from tenxgraph_api.src.app.core.middleware.rate_limit import (
    BaseRateLimitBackend,
    MemoryRateLimitBackend,
    RateLimitDecision,
    RateLimitMiddleware,
    RedisRateLimitBackend,
    build_backend,
)


def _config(**overrides) -> RateLimitConfig:
    data = {
        "enabled": True,
        "backend": "memory",
        "requests": 2,
        "window": 60,
        "by": "ip",
    }
    data.update(overrides)
    return RateLimitConfig.from_dict(data)


@pytest.mark.asyncio
async def test_memory_backend_enforces_limit():
    backend = MemoryRateLimitBackend()

    first = await backend.check("client", limit=2, window=60)
    second = await backend.check("client", limit=2, window=60)
    third = await backend.check("client", limit=2, window=60)

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.remaining == 0


@pytest.mark.asyncio
async def test_memory_backend_unique_key_cap_evicts():
    backend = MemoryRateLimitBackend(max_unique_keys=2)

    await backend.check("a", limit=10, window=60)
    await backend.check("b", limit=10, window=60)
    await backend.check("c", limit=10, window=60)

    assert len(backend._buckets) <= 2
    assert "c" in backend._buckets


@pytest.mark.asyncio
async def test_redis_backend_uses_unique_members_for_same_millisecond(monkeypatch):
    calls = []

    async def fake_script(*, keys, args):
        calls.append((keys, args))
        return [1, 1, 60]

    backend = object.__new__(RedisRateLimitBackend)
    backend._prefix = "10xgraph:test"
    backend._fail_open = True
    backend._script = fake_script

    monkeypatch.setattr("time.time", lambda: 123.456)

    await backend.check("client", limit=2, window=60)
    await backend.check("client", limit=2, window=60)

    first_member = calls[0][1][3]
    second_member = calls[1][1][3]
    assert first_member.startswith("123456:")
    assert second_member.startswith("123456:")
    assert first_member != second_member


def test_rate_limit_config_parses_boolean_strings_and_expands_redis_url(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_REDIS_URL", "redis://localhost:6379/4")

    config = RateLimitConfig.from_dict(
        {
            "enabled": "true",
            "backend": "redis",
            "requests": 5,
            "window": 10,
            "by": "global",
            "trusted_proxy_headers": "false",
            "fail_open": "no",
            "redis": {"url": "${RATE_LIMIT_REDIS_URL}", "prefix": "10xgraph:test"},
        }
    )

    assert config.enabled is True
    assert config.trusted_proxy_headers is False
    assert config.fail_open is False
    assert config.redis_url == "redis://localhost:6379/4"


def test_rate_limit_config_allows_custom_backend_without_path():
    config = RateLimitConfig.from_dict({"backend": "custom"})

    assert config.backend == "custom"


def test_rate_limit_config_rejects_invalid_boolean_string():
    with pytest.raises(ValueError, match="rate_limit.fail_open must be a boolean"):
        RateLimitConfig.from_dict({"fail_open": "sometimes"})


def test_rate_limit_middleware_ignores_forwarded_for_by_default():
    app = FastAPI()
    backend = MemoryRateLimitBackend()
    app.add_middleware(RateLimitMiddleware, config=_config(), backend=backend)

    @app.get("/")
    def root():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-For": "3.3.3.3"}).status_code == 429


def test_rate_limit_middleware_uses_forwarded_for_when_trusted():
    app = FastAPI()
    backend = MemoryRateLimitBackend()
    app.add_middleware(
        RateLimitMiddleware,
        config=_config(trusted_proxy_headers=True),
        backend=backend,
    )

    @app.get("/")
    def root():
        return {"ok": True}

    client = TestClient(app, client=("10.0.0.5", 50000))  # behind a private proxy
    assert client.get("/", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-For": "3.3.3.3"}).status_code == 200


def test_rate_limit_middleware_excludes_paths():
    app = FastAPI()
    backend = MemoryRateLimitBackend()
    app.add_middleware(
        RateLimitMiddleware,
        config=_config(requests=1, exclude_paths=["/health"]),
        backend=backend,
    )

    @app.get("/health")
    def health():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200


@pytest.mark.asyncio
async def test_custom_backend_resolves_from_injectq():
    class MyRateLimitBackend(BaseRateLimitBackend):
        async def check(self, key: str, *, limit: int, window: int) -> RateLimitDecision:
            return RateLimitDecision(allowed=True, remaining=limit - 1, reset_after=window)

        async def close(self) -> None:
            return None

    container = InjectQ()
    custom_backend = MyRateLimitBackend()
    container.bind_instance(BaseRateLimitBackend, custom_backend)

    backend = build_backend(_config(backend="custom"), container=container)

    assert backend is custom_backend
    assert (await backend.check("client", limit=2, window=60)).allowed is True


def test_custom_backend_requires_injectq_binding():
    with pytest.raises(ValueError, match="BaseRateLimitBackend"):
        build_backend(_config(backend="custom"), container=InjectQ())


def test_redis_backend_reuses_injectq_redis_client():
    class FakeRedis:
        def register_script(self, script):
            async def fake_script(*, keys, args):
                return [1, 0, 60]

            return fake_script

    container = InjectQ()
    redis = FakeRedis()
    container.bind_instance("redis", redis)

    backend = build_backend(
        _config(
            backend="redis",
            redis={"prefix": "10xgraph:test"},
        ),
        container=container,
    )

    assert isinstance(backend, RedisRateLimitBackend)
    assert backend._redis is redis
    assert backend._close_redis is False


def test_redis_backend_requires_url_when_no_injected_client():
    with pytest.raises(ValueError, match="redis.url"):
        build_backend(_config(backend="redis"), container=InjectQ())


def test_redis_backend_from_url_builds_and_binds_the_client():
    """A Redis URL with no injected client must build the backend, not crash."""
    from redis.asyncio import Redis

    container = InjectQ()
    backend = build_backend(
        _config(backend="redis", redis={"url": "redis://localhost:6379/0"}), container=container
    )

    assert isinstance(backend, RedisRateLimitBackend)
    assert container.try_get("redis") is backend._redis
    assert container.try_get(Redis) is backend._redis


def test_redis_backend_from_url_requires_optional_extra(monkeypatch):
    monkeypatch.setattr(
        "tenxgraph_api.src.app.core.middleware.rate_limit.redis._REDIS_AVAILABLE",
        False,
    )
    monkeypatch.setattr(
        "tenxgraph_api.src.app.core.middleware.rate_limit.redis.AsyncRedis",
        None,
    )

    with pytest.raises(ImportError, match=r"10xgraph-api\[redis\]"):
        RedisRateLimitBackend.from_url(
            redis_url="redis://localhost:6379/0",
            prefix="10xgraph:test",
        )


def test_rate_limit_middleware_ignores_forwarded_for_from_a_public_peer():
    """No trusted_proxies list must not mean "trust everyone"."""
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        config=_config(trusted_proxy_headers=True),
        backend=MemoryRateLimitBackend(),
    )

    @app.get("/")
    def root():
        return {"ok": True}

    client = TestClient(app, client=("198.51.100.7", 50000))  # reaches the app directly
    assert client.get("/", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-For": "3.3.3.3"}).status_code == 429
