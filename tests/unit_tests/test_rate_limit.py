# ruff: noqa: S101, PLR2004, SLF001

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore, SlotResult
from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig
from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware, check


def _config(**overrides) -> RateLimitConfig:
    data = {
        "enabled": True,
        "requests": 2,
        "window": 60,
        "by": "ip",
    }
    data.update(overrides)
    return RateLimitConfig.from_dict(data)


@pytest.mark.asyncio
async def test_check_enforces_limit():
    cache = InMemoryCacheStore()

    first = await check(cache, "client", 2, 60)
    second = await check(cache, "client", 2, 60)
    third = await check(cache, "client", 2, 60)

    assert (first.allowed, first.remaining, first.reset_after) == (True, 1, 60)
    assert (second.allowed, second.remaining) == (True, 0)
    assert third.allowed is False
    assert third.remaining == 0
    assert 1 <= third.reset_after <= 60


@pytest.mark.asyncio
async def test_check_takes_a_slot_that_is_never_released():
    cache = InMemoryCacheStore()
    await check(cache, "client", 2, 60)
    result = await cache.acquire("ratelimit:client", 10, ttl=60)
    assert result.count == 2  # ours plus the request's, still live


@pytest.mark.asyncio
async def test_denied_request_waits_for_the_oldest_slot():
    class Full(InMemoryCacheStore):
        async def now(self) -> float:
            return 1000.0

        async def acquire(self, key, limit, ttl):
            return SlotResult(granted=False, count=limit, oldest_expiry=1012.2, slot_id=None)

    decision = await check(Full(), "client", 2, 60)
    assert (decision.allowed, decision.remaining, decision.reset_after) == (False, 0, 13)


@pytest.mark.asyncio
async def test_window_expiry_frees_the_client():
    cache = InMemoryCacheStore()
    assert (await check(cache, "client", 1, 0.2)).allowed is True
    assert (await check(cache, "client", 1, 0.2)).allowed is False
    import asyncio

    await asyncio.sleep(0.3)
    assert (await check(cache, "client", 1, 0.2)).allowed is True


def test_rate_limit_config_parses_boolean_strings():
    config = RateLimitConfig.from_dict(
        {
            "enabled": "true",
            "requests": 5,
            "window": 10,
            "by": "global",
            "trusted_proxy_headers": "false",
            "fail_open": "no",
        }
    )

    assert config.enabled is True
    assert config.trusted_proxy_headers is False
    assert config.fail_open is False


@pytest.mark.parametrize(
    "removed",
    [
        {"backend": "memory"},
        {"backend": "redis"},
        {"backend": "custom"},
        {"redis": {"url": "redis://localhost:6379/0"}},
        {"redis": "redis://localhost:6379/0"},
    ],
)
def test_removed_rate_limit_keys_name_the_cache_store(removed):
    with pytest.raises(ValueError, match="cache_store") as exc:
        RateLimitConfig.from_dict(removed)
    assert "`redis`" in str(exc.value)


def test_rate_limit_fail_open_defaults_to_true():
    assert RateLimitConfig.from_dict({}).fail_open is True


def test_rate_limit_config_rejects_invalid_boolean_string():
    with pytest.raises(ValueError, match="rate_limit.fail_open must be a boolean"):
        RateLimitConfig.from_dict({"fail_open": "sometimes"})


def test_rate_limit_middleware_ignores_forwarded_for_by_default():
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, config=_config(), cache=InMemoryCacheStore())

    @app.get("/")
    def root():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-For": "3.3.3.3"}).status_code == 429


def test_rate_limit_middleware_uses_forwarded_for_when_trusted():
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        config=_config(trusted_proxy_headers=True),
        cache=InMemoryCacheStore(),
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
    app.add_middleware(
        RateLimitMiddleware,
        config=_config(requests=1, exclude_paths=["/health"]),
        cache=InMemoryCacheStore(),
    )

    @app.get("/health")
    def health():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200


def test_middleware_uses_the_cache_store_bound_after_it_was_added():
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore

    container = InjectQ()
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, config=_config(requests=1), container=container)
    cache = InMemoryCacheStore()
    container.bind_instance(BaseCacheStore, cache)

    @app.get("/")
    def root():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/").status_code == 429
    assert cache._slots  # the counters live in the bound store


def test_rate_limit_middleware_ignores_forwarded_for_from_a_public_peer():
    """No trusted_proxies list must not mean "trust everyone"."""
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        config=_config(trusted_proxy_headers=True),
        cache=InMemoryCacheStore(),
    )

    @app.get("/")
    def root():
        return {"ok": True}

    client = TestClient(app, client=("198.51.100.7", 50000))  # reaches the app directly
    assert client.get("/", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
    assert client.get("/", headers={"X-Forwarded-For": "3.3.3.3"}).status_code == 429
