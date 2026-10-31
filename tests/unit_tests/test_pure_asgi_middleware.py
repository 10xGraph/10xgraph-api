"""The hot-path middleware is pure ASGI."""

# ruff: noqa: S101

import asyncio

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig
from tenxgraph_api.src.app.core.config.setup_middleware import RequestIDMiddleware
from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware
from tenxgraph_api.src.app.core.middleware.security_headers import SecurityHeadersMiddleware


def _rate_limit_config(**overrides) -> RateLimitConfig:
    data = {"enabled": True, "requests": 5, "window": 60}
    data.update(overrides)
    return RateLimitConfig.from_dict(data)


@pytest.mark.parametrize(
    "cls", [RequestIDMiddleware, SecurityHeadersMiddleware, RateLimitMiddleware]
)
def test_middleware_is_not_base_http_middleware(cls):
    assert not issubclass(cls, BaseHTTPMiddleware)


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/plain")
    async def plain(request: Request):
        return {"request_id": request.state.request_id}

    @app.get("/stream")
    async def stream():
        async def body():
            yield b"first\n"
            await asyncio.sleep(0)
            yield b"second\n"

        return StreamingResponse(body(), media_type="text/plain")

    app.add_middleware(RateLimitMiddleware, config=_rate_limit_config(), cache=InMemoryCacheStore())
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)
    return app


def test_all_three_still_decorate_the_response():
    response = TestClient(_app()).get("/plain")
    assert response.status_code == 200
    # Request state is filled before the route runs and matches the header.
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["X-Timestamp"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "4"


def test_streaming_body_passes_through_chunk_by_chunk():
    with TestClient(_app()).stream("GET", "/stream") as response:
        chunks = list(response.iter_bytes())
        assert response.headers["X-Request-ID"]
        assert response.headers["X-RateLimit-Limit"] == "5"
    assert b"".join(chunks) == b"first\nsecond\n"


def test_rate_limited_response_still_carries_request_id_and_security_headers():
    app = FastAPI()

    @app.get("/")
    async def root():
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware,
        config=_rate_limit_config(requests=1),
        cache=InMemoryCacheStore(),
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)
    client = TestClient(app)
    client.get("/")
    limited = client.get("/")
    assert limited.status_code == 429
    assert limited.json()["metadata"]["request_id"] == limited.headers["X-Request-ID"]
    assert limited.headers["Retry-After"]
    assert limited.headers["X-Frame-Options"] == "DENY"


def test_websocket_scopes_pass_straight_through():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    app.add_middleware(
        RateLimitMiddleware, config=_rate_limit_config(requests=1), cache=InMemoryCacheStore()
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)
    client = TestClient(app)
    for _ in range(3):  # not counted by the HTTP limiter
        with client.websocket_connect("/ws") as conn:
            assert conn.receive_text() == "hi"
