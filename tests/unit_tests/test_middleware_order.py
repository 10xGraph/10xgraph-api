"""Early rejections still carry CORS, request id and security headers."""

# ruff: noqa: S101

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from tenxgraph_api.src.app.core.config import setup_middleware as sm
from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig
from tenxgraph_api.src.app.core.config.settings import Settings


ORIGIN = "https://app.example.com"


def _app(allowed_host: str = "testserver", max_size: int = 10 * 1024 * 1024) -> FastAPI:
    settings = Settings(
        ORIGINS=ORIGIN,
        ALLOWED_HOST=allowed_host,
        MAX_REQUEST_SIZE=max_size,
        _env_file=None,
    )
    graph_config = SimpleNamespace(
        rate_limit=RateLimitConfig.from_dict({"backend": "memory", "requests": 1, "window": 60}),
        observability=None,
    )
    app = FastAPI()

    @app.get("/")
    async def root():
        return {"ok": True}

    @app.post("/echo")
    async def echo(body: dict):
        return body

    with patch.object(sm, "get_settings", lambda: settings), patch.object(sm, "init_sentry"):
        sm.setup_middleware(app, graph_config=graph_config)  # type: ignore[arg-type]
    return app


def _assert_decorated(response):
    assert response.headers.get("access-control-allow-origin") == ORIGIN
    assert response.headers.get("x-request-id")
    assert response.headers.get("x-content-type-options") == "nosniff"


def test_cors_is_outermost():
    stack = [m.cls for m in _app().user_middleware]  # outermost first
    assert stack[0] is CORSMiddleware
    names = [cls.__name__ for cls in stack]
    assert names == [
        "CORSMiddleware",
        "RequestIDMiddleware",
        "SecurityHeadersMiddleware",
        "HealthCheckAwareTrustedHostMiddleware",
        "RateLimitMiddleware",
        "RequestSizeLimitMiddleware",
        "SelectiveGZipMiddleware",
    ]


def test_rate_limited_response_is_readable_by_the_browser():
    client = TestClient(_app())
    assert client.get("/", headers={"Origin": ORIGIN}).status_code == 200
    limited = client.get("/", headers={"Origin": ORIGIN})
    assert limited.status_code == 429
    _assert_decorated(limited)
    assert limited.json()["metadata"]["request_id"] == limited.headers["x-request-id"]


def test_rejected_host_response_carries_cors():
    client = TestClient(_app(allowed_host="api.example.com"))
    resp = client.get("/", headers={"Origin": ORIGIN})
    assert resp.status_code == 400
    _assert_decorated(resp)


def test_oversized_body_response_carries_cors():
    client = TestClient(_app(max_size=10))
    resp = client.post("/echo", json={"text": "x" * 100}, headers={"Origin": ORIGIN})
    assert resp.status_code == 413
    _assert_decorated(resp)


@pytest.mark.parametrize("path", ["/"])
def test_preflight_is_answered_before_the_rate_limiter(path):
    client = TestClient(_app())
    headers = {"Origin": ORIGIN, "Access-Control-Request-Method": "GET"}
    for _ in range(3):  # CORS answers preflights itself; they never reach the limiter
        assert client.options(path, headers=headers).status_code == 200
    assert client.get(path, headers={"Origin": ORIGIN}).status_code == 200
