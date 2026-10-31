"""A rate-limiter backend outage answers 503 RATE_LIMIT_UNAVAILABLE, never 429."""

# ruff: noqa: S101, PLR2004, SLF001

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore, SlotResult
from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig
from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware, limiter


@pytest.fixture(autouse=True)
def _no_outage_in_progress(monkeypatch):
    monkeypatch.setattr(limiter, "_failing", False)


def _config(**overrides) -> RateLimitConfig:
    data = {"enabled": True, "requests": 2, "window": 60, "by": "ip"}
    data.update(overrides)
    return RateLimitConfig.from_dict(data)


class _BrokenStore(InMemoryCacheStore):
    """A cache store whose backend is down, until ``healed``."""

    healed = False

    async def acquire(self, key: str, limit: int, ttl: float) -> SlotResult:
        if not self.healed:
            raise ConnectionError("Error 111 connecting to localhost:6379. Connection refused.")
        return await super().acquire(key, limit, ttl)


def _client(cache: BaseCacheStore, **overrides) -> TestClient:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, config=_config(**overrides), cache=cache)

    @app.get("/")
    def root():
        return {"ok": True}

    return TestClient(app)


def test_backend_error_with_fail_open_false_answers_503():
    resp = _client(_BrokenStore(), fail_open=False).get("/")
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"
    error = resp.json()["error"]
    assert error["code"] == "RATE_LIMIT_UNAVAILABLE"
    assert error["message"] == "Rate limiter unavailable: its backend could not be reached."
    assert error["retry_after_seconds"] == 5


def test_backend_error_with_fail_open_true_allows_the_request():
    resp = _client(_BrokenStore(), fail_open=True).get("/")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_real_limit_still_answers_429():
    client = _client(InMemoryCacheStore())
    assert client.get("/").status_code == 200
    assert client.get("/").status_code == 200
    resp = client.get("/")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert "Retry-After" in resp.headers


def test_plain_denial_from_a_custom_store_is_still_429():
    class Full(InMemoryCacheStore):
        async def acquire(self, key: str, limit: int, ttl: float) -> SlotResult:
            return SlotResult(
                granted=False, count=limit, oldest_expiry=await self.now() + 2.5, slot_id=None
            )

    resp = _client(Full()).get("/")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "3"


def test_app_wiring_with_fail_open_false_and_a_failing_cache_store_answers_503(tmp_path):
    """The startup check off, the bound cache store down: 503 RATE_LIMIT_UNAVAILABLE, not 429."""
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
    from tenxgraph_api.src.app.core.config.setup_middleware import setup_middleware

    path = tmp_path / "10xgraph.json"
    path.write_text(
        '{"agent": "g:app", "cache_store": {"rate_limit": {"requests": 2, "fail_open": false}}}'
    )
    graph_config = GraphConfig(str(path))
    container = InjectQ()
    app = FastAPI()
    setup_middleware(app, graph_config=graph_config, container=container)
    container.bind_instance(BaseCacheStore, _BrokenStore())  # bound at startup, after setup

    @app.get("/")
    def root():
        return {"ok": True}

    resp = TestClient(app).get("/", headers={"host": "localhost"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "RATE_LIMIT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_outage_logs_one_traceback_then_one_line_per_error(caplog):
    store = _BrokenStore()
    with caplog.at_level(logging.INFO, logger="tenxgraph_api.rate_limit"):
        for _ in range(3):
            decision = await limiter.check(store, "client", 2, 60, fail_open=False)
            assert decision.unavailable is True
        records = [r for r in caplog.records if r.name == "tenxgraph_api.rate_limit"]
        assert [r.exc_info is not None for r in records] == [True, False, False]
        assert "Connection refused" in records[1].getMessage()

        store.healed = True
        assert (await limiter.check(store, "client", 2, 60, fail_open=False)).allowed is True
    assert "recovered" in caplog.records[-1].getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_open", [True, False])
async def test_denial_stays_429_when_the_clock_read_after_it_fails(fail_open):
    """acquire denied, then now() failed: still the limit hit, never allow or 503."""

    class DeniesThenClockFails(InMemoryCacheStore):
        async def acquire(self, key: str, limit: int, ttl: float) -> SlotResult:
            return SlotResult(granted=False, count=limit, oldest_expiry=1e12, slot_id=None)

        async def now(self) -> float:
            raise ConnectionError("Connection reset by peer")

    decision = await limiter.check(DeniesThenClockFails(), "client", 2, 60.5, fail_open=fail_open)
    assert decision.allowed is False
    assert decision.unavailable is False
    assert decision.reset_after == 61  # ceil(window)
