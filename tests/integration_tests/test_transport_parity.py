"""Transport parity: one token gets the same decision on every transport.

Real app wiring (``build_app``), real ``JwtAuth`` with a role table, tokens minted with PyJWT.
HTTP invoke, HTTP stream, AG-UI, ``/v1/graph/ws`` and ``/v1/graph/live`` all run through
``RequirePermission``. A request that passes auth is "allowed" and then stops at body
validation (HTTP 422, an empty body) or at the first frame (WebSocket): the guard is what is
under test, not the graph. ``GraphService`` is a stub.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import FastAPI, WebSocketDisconnect
from injectq import InjectQ
from tenxgraph.core.authz import Identity
from tenxgraph.storage.store import BaseStore

from tenxgraph_api.src.app.core.auth.jwt_auth import JwtAuth
from tenxgraph_api.src.app.core.auth.rbac import RbacConfig
from tenxgraph_api.src.app.core.auth.revocation import TokenRevocationConfig, revoke_token
from tenxgraph_api.src.app.core.auth.route_guard import assert_all_routes_protected
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import AgUiConfig, GraphConfig, WebSocketConfig
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.routers.ag_ui import router as ag_ui_router
from tenxgraph_api.src.app.routers.checkpointer.router import router as threads_router
from tenxgraph_api.src.app.routers.graph.router import router as graph_router
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.routers.me import router as me_router
from tenxgraph_api.src.app.routers.store.router import router as store_router

from . import conftest
from .conftest import build_app, make_client


SECRET = "transport-parity-test-secret-key-0123456789"
HTTP_OK = 200
HTTP_UNPROCESSABLE = 422
POLICY_VIOLATION = 1008

ROLES = RbacConfig(
    roles={
        "invoker": {"permissions": ["graph:invoke"], "reach": "own"},
        "streamer": {"permissions": ["graph:stream"], "reach": "own"},
        "reader": {"permissions": ["threads:read", "store:read"], "reach": "own"},
        "admin": {"permissions": ["*"], "reach": "all"},
    }
)

# transport -> permission it needs
TRANSPORTS = {
    "invoke": "graph:invoke",
    "stream": "graph:stream",
    "ag_ui": "graph:stream",
    "ws": "graph:stream",
    "live": "graph:stream",
}


class _Config:
    """GraphConfig stand-in: auth on, no rate limit, no connection caps."""

    rate_limit = None
    websocket = WebSocketConfig(max_connections=None, max_connections_per_user=None)
    ag_ui = AgUiConfig(enabled=True)

    def auth_config(self) -> str:
        return "jwt"


class _StubService:
    """Stands in for GraphService; never reached by a denied request."""

    is_live_agent = False


class _Auth(JwtAuth):
    rbac = ROLES


class _DenyStream(_Auth):
    """Custom ``authorize``: nobody may ``graph:stream``, whatever their roles say."""

    async def authorize(
        self, identity: Identity, permission: str, resource: str | None = None
    ) -> bool:
        return permission != "graph:stream" and identity.can(permission)


@pytest.fixture(autouse=True)
def jwt_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def mint(secret: str = SECRET, expires_in: int = 3600, **claims: Any) -> str:
    now = int(time.time())
    payload = {"user_id": "alice", "roles": ["admin"], "iat": now, "exp": now + expires_in}
    payload.update(claims)
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def app_container(monkeypatch):
    """Make the container ``build_app`` creates the global one.

    The WebSocket routes bind ``InjectQ.get_instance()`` for the handshake (they get no HTTP
    middleware), so a test app must be that instance.
    """
    seen: list[InjectQ] = []
    real_setup = conftest.setup_fastapi

    def setup(container, app):
        seen.append(container)
        real_setup(container, app)

    monkeypatch.setattr(conftest, "setup_fastapi", setup)
    monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: seen[-1]))


def _app(auth=None, routers=None, cache: BaseCacheStore | None = None) -> FastAPI:
    bindings: dict[type, Any] = {
        GraphConfig: _Config(),
        GraphService: _StubService(),
        BaseStore: AsyncMock(spec=BaseStore),
    }
    if cache is not None:
        bindings[BaseCacheStore] = cache
        bindings[TokenRevocationConfig] = TokenRevocationConfig()
    return build_app(
        routers=routers or [graph_router, ag_ui_router, threads_router, store_router, me_router],
        auth=auth or _Auth(),
        extra_bindings=bindings,
    )


def call(client, transport: str, token: str | None) -> str:
    """The decision on one transport: ``allow``, ``401``, ``403`` or ``1008``."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if transport in ("ws", "live"):
        path = "/v1/graph/ws" if transport == "ws" else "/v1/graph/live"
        try:
            with client.websocket_connect(path, headers=headers):
                return "allow"
        except WebSocketDisconnect as exc:
            assert exc.code == POLICY_VIOLATION
            return "1008"
    path = {
        "invoke": "/v1/graph/invoke",
        "stream": "/v1/graph/stream",
        "ag_ui": "/v1/ag-ui",
    }[transport]
    status = client.post(path, json={}, headers=headers).status_code
    return "allow" if status == HTTP_UNPROCESSABLE else str(status)


def expected(transport: str, http_decision: str) -> str:
    """WebSocket transports answer any refusal with close 1008."""
    if http_decision == "allow" or transport not in ("ws", "live"):
        return http_decision
    return "1008"


@pytest.fixture
def client(app_container):
    return make_client(_app())


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_role_with_the_permission_is_allowed(client, transport):
    role = "invoker" if TRANSPORTS[transport] == "graph:invoke" else "streamer"
    assert call(client, transport, mint(roles=[role])) == "allow"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_role_without_the_permission_is_403(client, transport):
    role = "streamer" if TRANSPORTS[transport] == "graph:invoke" else "invoker"
    assert call(client, transport, mint(roles=[role])) == expected(transport, "403")


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_unknown_role_has_no_permission(client, transport):
    assert call(client, transport, mint(roles=["nobody"])) == expected(transport, "403")


BAD_TOKENS = {
    "no_token": lambda: None,
    "bad_signature": lambda: mint(secret="another-secret-key-of-32-bytes-or-more!!"),
    "expired": lambda: mint(expires_in=-60),
    "garbage": lambda: "not.a.jwt",
    "malformed_roles": lambda: mint(roles=123),
    "reserved_user_id": lambda: mint(user_id="__system__:memory"),
}


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("case", BAD_TOKENS)
def test_unauthenticated_is_401(client, transport, case):
    assert call(client, transport, BAD_TOKENS[case]()) == expected(transport, "401")


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_revoked_token_is_401(app_container, transport):
    cache = InMemoryCacheStore()
    client = make_client(_app(cache=cache))
    token = mint(jti="revoked-1")
    assert call(client, transport, token) == "allow"
    asyncio.run(revoke_token(cache, "revoked-1"))
    assert call(client, transport, token) == expected(transport, "401")
    assert call(client, transport, mint(jti="other")) == "allow"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_custom_authorize_denies_one_permission_everywhere(app_container, transport):
    client = make_client(_app(auth=_DenyStream()))
    decision = call(client, transport, mint(roles=["admin"]))
    if TRANSPORTS[transport] == "graph:stream":
        assert decision == expected(transport, "403")
    else:
        assert decision == "allow"


def test_threads_route_401_vs_403(client):
    assert client.get("/v1/threads").status_code == 401
    bad = mint(secret="another-secret-key-of-32-bytes-or-more!!")
    assert client.get("/v1/threads", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    lacks = {"Authorization": f"Bearer {mint(roles=['invoker'])}"}
    assert client.get("/v1/threads", headers=lacks).status_code == 403
    has = {"Authorization": f"Bearer {mint(roles=['reader'])}"}
    assert client.get("/v1/threads", headers=has).status_code == HTTP_OK


def test_store_route_401_vs_403(client):
    body = {"query": "x"}
    assert client.post("/v1/store/search", json=body).status_code == 401
    lacks = {"Authorization": f"Bearer {mint(roles=['streamer'])}"}
    assert client.post("/v1/store/search", json=body, headers=lacks).status_code == 403
    has = {"Authorization": f"Bearer {mint(roles=['reader'])}"}
    # Past the guard: the request reaches body validation, not 401/403.
    assert client.post("/v1/store/search", json={}, headers=has).status_code == HTTP_UNPROCESSABLE


def test_route_guard_refuses_an_unguarded_route(app_container):
    app = _app()
    assert_all_routes_protected(app)

    @app.get("/v1/open")
    async def open_route():
        return {}

    with pytest.raises(RuntimeError, match="/v1/open"):
        assert_all_routes_protected(app)


def test_me_returns_the_resolved_identity(client):
    token = mint(roles=["reader"], user_id="bob")
    resp = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTP_OK
    data = resp.json()["data"]
    assert data["user_id"] == "bob"
    assert data["roles"] == ["reader"]
    assert data["permissions"] == ["store:read", "threads:read"]
    assert data["reach"] == "own"


def test_me_reports_reach_all_and_needs_a_token(client):
    admin = mint(roles=["admin"])
    data = client.get("/v1/me", headers={"Authorization": f"Bearer {admin}"}).json()["data"]
    assert data["reach"] == "all"
    assert "graph:invoke" in data["permissions"]
    assert client.get("/v1/me").status_code == 401
