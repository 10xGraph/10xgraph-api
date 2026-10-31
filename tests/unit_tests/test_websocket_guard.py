"""Tests for the WebSocket connection guard and secure token transport.

Covers what middleware cannot, because rate-limit / request-size middleware are HTTP-only:
  - the global rate limit applied at the WS handshake (shared backend/bucket with REST),
  - the concurrent-connection cap (websocket.max_connections),
  - bearer token via the Sec-WebSocket-Protocol sentinel (kept out of URLs/logs).

These drive the guard through FastAPI's real DI on a @app.websocket route.
"""

import asyncio
from typing import Any

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations import setup_fastapi
from starlette.websockets import WebSocketDisconnect

from tenxgraph_api.src.app.core.auth.auth_backend import BaseAuth
from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend
from tenxgraph_api.src.app.core.auth.permissions import (
    LEGACY_WS_BEARER_SUBPROTOCOL,
    WS_BEARER_SUBPROTOCOL,
    RequirePermission,
    ws_bearer_subprotocol,
)
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore, SlotResult
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, WebSocketConfig
from tenxgraph_api.src.app.core.leases import LeaseManager
from tenxgraph_api.src.app.routers.graph import realtime_guard
from tenxgraph_api.src.app.routers.graph.realtime_guard import realtime_connection_guard


class _FakeAuth(BaseAuth):
    def authenticate(self, request, response, credential):  # type: ignore[override]
        return {} if credential is None else {"user_id": credential.credentials}


class _AllowAuthz(AuthorizationBackend):
    def __init__(self):
        pass

    async def authorize(self, user, resource, action, resource_id=None):  # type: ignore[override]
        return True


class _FakeRateBackend(InMemoryCacheStore):
    """A cache store whose rate-limit windows always grant (or always deny)."""

    def __init__(self, allowed: bool):
        super().__init__()
        self._allowed = allowed
        self.calls = 0

    async def acquire(self, key, limit, ttl):
        if not key.startswith("ratelimit:"):
            return await super().acquire(key, limit, ttl)
        self.calls += 1
        if self._allowed:
            return SlotResult(granted=True, count=1, oldest_expiry=None, slot_id="s")
        return SlotResult(
            granted=False, count=limit, oldest_expiry=await self.now() + 5, slot_id=None
        )


class _StubConfig:
    def __init__(self, rate_limit, max_connections, auth_configured=True):
        self._rl = rate_limit
        self._ws = WebSocketConfig(max_connections=max_connections)
        self._auth_configured = auth_configured

    def auth_config(self):
        return "custom" if self._auth_configured else None

    @property
    def rate_limit(self):
        return self._rl

    @property
    def websocket(self):
        return self._ws


def _build_app(config, rate_backend=None, *, auth=False):
    container = InjectQ()
    container.bind_instance(GraphConfig, config, allow_none=True)
    container.bind_instance(BaseAuth, _FakeAuth(), allow_none=True)
    container.bind_instance(AuthorizationBackend, _AllowAuthz(), allow_none=True)

    if rate_backend is not None:
        container.bind_instance(BaseCacheStore, rate_backend)

    app = FastAPI()
    setup_fastapi(container, app)

    deps = [Depends(realtime_connection_guard)]
    if auth:
        deps.append(Depends(RequirePermission("graph", "stream")))

    @app.websocket("/ws")
    async def ws(
        websocket: WebSocket,
        _guard: None = deps[0],
        # Must stay a lambda: FastAPI resolves the dependency via
        # inspect.signature(call, eval_str=True), which fails on the `dict` builtin.
        user: dict[str, Any] = (deps[1] if auth else Depends(lambda: {})),  # noqa: PIE807
    ):
        await websocket.accept(subprotocol=ws_bearer_subprotocol(websocket))
        active = realtime_guard.lease_manager().held("ws:all")
        await websocket.send_json({"active": active, "user": user})
        await websocket.close()

    return app


@pytest.fixture(autouse=True)
def leases(monkeypatch):
    """A fresh in-process lease manager behind the connection cap."""
    manager = LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(realtime_guard, "lease_manager", lambda: manager)
    return manager


class TestConcurrencyCap:
    def test_under_cap_connects_and_tracks_active(self):
        app = _build_app(_StubConfig(None, 2))
        client = TestClient(app)
        with client.websocket_connect("/ws") as conn:
            msg = conn.receive_json()
            assert msg["active"] == 1

    def test_slot_released_after_disconnect(self):
        app = _build_app(_StubConfig(None, 1))
        client = TestClient(app)
        # Two sequential connections both succeed because the first releases its slot.
        for _ in range(2):
            with client.websocket_connect("/ws") as conn:
                assert conn.receive_json()["active"] == 1
        assert realtime_guard.lease_manager().held("ws:all") == 0

    def test_over_cap_rejected(self, leases):
        app = _build_app(_StubConfig(None, 1))
        # Simulate one already-active connection (held by another worker, or another socket).
        asyncio.run(leases.cache.acquire("concurrency:ws:all", 1, 60))
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws") as conn:
                conn.receive_json()
        assert exc.value.code == realtime_guard.WS_TRY_AGAIN_LATER


class TestHandshakeRateLimit:
    def test_rate_limited_handshake_rejected(self):
        backend = _FakeRateBackend(allowed=False)
        config = _StubConfig(_RL(), None)
        app = _build_app(config, rate_backend=backend)
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws") as conn:
                conn.receive_json()
        assert exc.value.code == realtime_guard.WS_TRY_AGAIN_LATER
        assert backend.calls == 1

    def test_allowed_handshake_passes(self):
        backend = _FakeRateBackend(allowed=True)
        config = _StubConfig(_RL(), None)
        app = _build_app(config, rate_backend=backend)
        client = TestClient(app)
        with client.websocket_connect("/ws") as conn:
            assert conn.receive_json()["active"] == 1
        assert backend.calls == 1


class _RL:
    """Minimal RateLimitConfig stand-in for keying + check()."""

    by = "global"
    requests = 100
    window = 60
    trusted_proxy_headers = False
    fail_open = True


class TestSubprotocolToken:
    def test_token_via_subprotocol_authenticates_and_is_echoed(self):
        app = _build_app(_StubConfig(None, None), auth=True)
        client = TestClient(app)
        with client.websocket_connect("/ws", subprotocols=[WS_BEARER_SUBPROTOCOL, "alice"]) as conn:
            msg = conn.receive_json()
            assert msg["user"]["user_id"] == "alice"
            # Server must echo the sentinel subprotocol or browsers fail the handshake.
            assert conn.accepted_subprotocol == WS_BEARER_SUBPROTOCOL

    def test_legacy_subprotocol_still_authenticates_and_is_echoed(self):
        app = _build_app(_StubConfig(None, None), auth=True)
        client = TestClient(app)
        with client.websocket_connect(
            "/ws", subprotocols=[LEGACY_WS_BEARER_SUBPROTOCOL, "alice"]
        ) as conn:
            msg = conn.receive_json()
            assert msg["user"]["user_id"] == "alice"
            assert conn.accepted_subprotocol == LEGACY_WS_BEARER_SUBPROTOCOL


class TestWebSocketConfig:
    def test_absent_gets_a_finite_default(self):
        # Unlimited must be an explicit choice (0 or null); see test_websocket_limits.py.
        assert WebSocketConfig.from_dict({}).max_connections == 1000

    def test_zero_is_unlimited(self):
        assert WebSocketConfig.from_dict({"max_connections": 0}).max_connections is None

    def test_positive_value(self):
        assert WebSocketConfig.from_dict({"max_connections": 25}).max_connections == 25

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            WebSocketConfig.from_dict({"max_connections": -1})
