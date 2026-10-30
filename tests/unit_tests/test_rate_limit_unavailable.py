"""A rate-limiter backend outage answers 503 RATE_LIMIT_UNAVAILABLE, never 429."""

# ruff: noqa: S101, PLR2004, SLF001

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig
from tenxgraph_api.src.app.core.middleware.rate_limit import (
    BaseRateLimitBackend,
    MemoryRateLimitBackend,
    RateLimitDecision,
    RateLimitMiddleware,
    RedisRateLimitBackend,
)


def _config(**overrides) -> RateLimitConfig:
    data = {"enabled": True, "backend": "memory", "requests": 2, "window": 60, "by": "ip"}
    data.update(overrides)
    return RateLimitConfig.from_dict(data)


def _broken_redis_backend(*, fail_open: bool) -> RedisRateLimitBackend:
    async def failing_script(*, keys, args):
        raise ConnectionError("Error 111 connecting to localhost:6379. Connection refused.")

    backend = object.__new__(RedisRateLimitBackend)
    backend._prefix = "10xgraph:test"
    backend._fail_open = fail_open
    backend._script = failing_script
    return backend


def _client(backend: BaseRateLimitBackend, **overrides) -> TestClient:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, config=_config(**overrides), backend=backend)

    @app.get("/")
    def root():
        return {"ok": True}

    return TestClient(app)


def test_backend_error_with_fail_open_false_answers_503():
    resp = _client(_broken_redis_backend(fail_open=False)).get("/")
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"
    error = resp.json()["error"]
    assert error["code"] == "RATE_LIMIT_UNAVAILABLE"
    assert error["message"] == "Rate limiter unavailable: its backend could not be reached."
    assert error["retry_after_seconds"] == 5


def test_backend_error_with_fail_open_true_allows_the_request():
    resp = _client(_broken_redis_backend(fail_open=True)).get("/")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_real_limit_still_answers_429():
    client = _client(MemoryRateLimitBackend())
    assert client.get("/").status_code == 200
    assert client.get("/").status_code == 200
    resp = client.get("/")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert "Retry-After" in resp.headers


def test_custom_backend_can_signal_unavailable():
    class Down(BaseRateLimitBackend):
        async def check(self, key: str, *, limit: int, window: int) -> RateLimitDecision:
            return RateLimitDecision.backend_unavailable(retry_after=7)

        async def close(self) -> None:
            return None

    resp = _client(Down()).get("/")
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "7"


def test_plain_denial_from_a_custom_backend_is_still_429():
    class Deny(BaseRateLimitBackend):
        async def check(self, key: str, *, limit: int, window: int) -> RateLimitDecision:
            return RateLimitDecision(allowed=False, remaining=0, reset_after=3)

        async def close(self) -> None:
            return None

    resp = _client(Deny()).get("/")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "3"


@pytest.mark.asyncio
async def test_outage_logs_one_traceback_then_one_line_per_error(caplog):
    backend = _broken_redis_backend(fail_open=False)
    with caplog.at_level(logging.INFO, logger="tenxgraph_api.rate_limit"):
        for _ in range(3):
            decision = await backend.check("client", limit=2, window=60)
            assert decision.unavailable is True
        records = [r for r in caplog.records if r.name == "tenxgraph_api.rate_limit"]
        assert [r.exc_info is not None for r in records] == [True, False, False]
        assert "Connection refused" in records[1].getMessage()

        async def working_script(*, keys, args):
            return [1, 1, 60]

        backend._script = working_script
        assert (await backend.check("client", limit=2, window=60)).allowed is True
    assert "recovered" in caplog.records[-1].getMessage()
