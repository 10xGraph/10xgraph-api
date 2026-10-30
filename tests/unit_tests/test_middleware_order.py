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
    assert issubclass(stack[0], CORSMiddleware)
    names = [cls.__name__ for cls in stack]
    assert names == [
        "AllowlistCORSMiddleware",
        "RequestIDMiddleware",
        "SecurityHeadersMiddleware",
        "HealthCheckAwareTrustedHostMiddleware",
        "RateLimitMiddleware",
        "RequestSizeLimitMiddleware",
        "SelectiveGZipMiddleware",
        "UnhandledErrorMiddleware",
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


PREFLIGHT = {"Access-Control-Request-Method": "GET"}
PRIVATE_NETWORK = {"Access-Control-Request-Private-Network": "true"}


def test_private_network_preflight_from_allowed_origin_is_granted():
    # The hosted playground calling a local dev server: browsers add this header.
    headers = {"Origin": ORIGIN, **PREFLIGHT, **PRIVATE_NETWORK}
    resp = TestClient(_app()).options("/", headers=headers)
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ORIGIN
    assert resp.headers.get("access-control-allow-private-network") == "true"


def test_private_network_preflight_from_other_origin_is_refused():
    headers = {"Origin": "https://evil.example.com", **PREFLIGHT, **PRIVATE_NETWORK}
    resp = TestClient(_app()).options("/", headers=headers)
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers
    assert "access-control-allow-private-network" not in resp.headers


def test_preflight_without_private_network_header_is_unchanged():
    resp = TestClient(_app()).options("/", headers={"Origin": ORIGIN, **PREFLIGHT})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ORIGIN
    assert "access-control-allow-private-network" not in resp.headers


def test_unhandled_exception_is_a_json_500_with_request_id_and_headers():
    app = _app()

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom", headers={"Origin": ORIGIN})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    _assert_decorated(response)
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert body["metadata"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize(
    ("make", "status", "retry"),
    [
        (lambda: __import__("redis").exceptions.ConnectionError("down"), 503, True),
        (
            lambda: type("RateLimitError", (Exception,), {})("slow down"),
            429,
            True,
        ),
        (lambda: RuntimeError("bug"), 500, False),
    ],
)
def test_retryable_failures_are_not_plain_500s(make, status, retry):
    app = _app()

    @app.get("/boom")
    async def boom():
        raise make()

    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == status
    assert ("retry-after" in response.headers) is retry


def test_a_wrapped_driver_error_is_still_a_503():
    app = _app()

    @app.get("/boom")
    async def boom():
        try:
            raise __import__("redis").exceptions.TimeoutError("slow")
        except Exception as exc:
            raise RuntimeError("checkpoint failed") from exc

    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 503
    assert response.headers["retry-after"]


def test_http_exception_headers_reach_the_client():
    from fastapi import HTTPException

    from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler

    app = _app()
    init_errors_handler(app)

    @app.get("/busy")
    async def busy():
        raise HTTPException(409, "busy", headers={"Retry-After": "3"})

    response = TestClient(app).get("/busy")
    assert response.status_code == 409
    assert response.headers["retry-after"] == "3"
