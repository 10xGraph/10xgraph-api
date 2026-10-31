"""No caller may authenticate as a reserved owner of shared data (``__system__`` prefix)."""

# ruff: noqa: S101

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from tenxgraph_api.src.app.core.auth.auth_backend import verify_current_user
from tenxgraph_api.src.app.core.auth.authorization import (
    AuthorizationBackend,
    DefaultAuthorizationBackend,
)
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.identity_cache import recall_identity, remember_identity
from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.auth.reserved_identity import (
    SYSTEM_USER_PREFIX,
    identity_is_reserved,
    is_reserved_user_id,
)
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RateLimitConfig
from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware, keying
from tenxgraph_api.src.app.routers.graph.realtime_guard import ws_identity_still_valid


HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
WS_POLICY_VIOLATION = 1008


class _TokenAuth(BaseAuth):
    """The bearer token is the user id, returned as-is (a custom backend with no checks)."""

    def __init__(self) -> None:
        self.calls = 0

    def authenticate(self, request, response, credential):
        self.calls += 1
        return {"user_id": credential.credentials if credential else ""}


class _ReusableTokenAuth(_TokenAuth):
    reusable_within_request = True

    def authenticate(self, request, response, credential):
        return super().authenticate(request, response, credential)


class _AuthOn:
    introspection_scoped = False

    def auth_config(self):
        return {"method": "custom", "path": "x:y"}


def _container(auth: BaseAuth) -> InjectQ:
    container = InjectQ()
    container.bind_instance(GraphConfig, _AuthOn())
    container.bind_instance(BaseAuth, auth)
    container.bind_instance(AuthorizationBackend, DefaultAuthorizationBackend())
    return container


def _app(auth: BaseAuth, *, rate_limit_by_user: bool = False) -> tuple[FastAPI, InjectQ]:
    container = _container(auth)
    app = FastAPI()
    if rate_limit_by_user:
        config = RateLimitConfig.from_dict(
            {"enabled": True, "requests": 100, "window": 60, "by": "user"}
        )
        app.add_middleware(RateLimitMiddleware, config=config, cache=InMemoryCacheStore())
    setup_fastapi(container, app)

    @app.get("/v1/threads")
    async def threads(user: dict[str, Any] = Depends(RequirePermission("checkpointer", "read"))):
        return {"user_id": user["user_id"]}

    # /v1/graph/ws and /v1/graph/live both authenticate their handshake with this dependency.
    @app.websocket("/v1/graph/live")
    async def live(
        websocket: WebSocket,
        user: dict[str, Any] = Depends(RequirePermission("graph", "stream")),
    ):
        await websocket.accept()
        await websocket.send_json(user)
        await websocket.close()

    return app, container


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


def test_prefix_is_the_documented_name():
    assert SYSTEM_USER_PREFIX == "__system__"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("__system__", True),
        ("  __system__ ", True),
        ("__system__:support_agent", True),
        ("__system__:knowledge_base", True),
        ("__system__x", True),
        ("x__system__", False),
        ("system", False),
        ("alice", False),
        ("anonymous", False),
        (12345, False),  # ints are stored as strings, never start with the prefix
        (True, False),
        (None, False),
        ("", False),
        (["__system__"], False),
    ],
)
def test_is_reserved_user_id(value, expected):
    assert is_reserved_user_id(value) is expected


def test_identity_is_reserved_needs_a_dict():
    assert identity_is_reserved({"user_id": "__system__"})
    assert not identity_is_reserved({"user_id": "alice"})
    assert not identity_is_reserved(None)
    assert not identity_is_reserved("__system__")


# --- HTTP ---------------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["__system__", "  __system__"])
def test_http_refuses_reserved_identity(token):
    app, _ = _app(_TokenAuth())
    resp = TestClient(app).get("/v1/threads", headers=_bearer(token))
    assert resp.status_code == HTTP_UNAUTHORIZED
    assert "reserved" in resp.text
    assert "__system__" not in resp.text


def test_http_still_accepts_ordinary_users_and_anonymous():
    app, _ = _app(_TokenAuth())
    client = TestClient(app)
    for name in ("alice", "anonymous", "system"):
        resp = client.get("/v1/threads", headers=_bearer(name))
        assert resp.status_code == HTTP_OK
        assert resp.json() == {"user_id": name}


def test_verify_current_user_refuses_reserved_identity():
    from fastapi import HTTPException

    cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials="__system__")
    with pytest.raises(HTTPException) as exc:
        verify_current_user(_request("__system__"), None, cred, _AuthOn(), _TokenAuth())  # type: ignore[arg-type]
    assert exc.value.status_code == HTTP_UNAUTHORIZED
    assert "__system__" not in str(exc.value.detail)

    alice = HTTPAuthorizationCredentials(scheme="Bearer", credentials="alice")
    ok = verify_current_user(_request("alice"), None, alice, _AuthOn(), _TokenAuth())  # type: ignore[arg-type]
    assert ok == {"user_id": "alice"}


def test_rejection_is_logged_at_warning_without_the_name(caplog):
    import logging

    app, _ = _app(_TokenAuth())
    with caplog.at_level(logging.WARNING):
        TestClient(app).get("/v1/threads", headers=_bearer("__system__"))
    rejected = [r for r in caplog.records if "reserved" in r.getMessage()]
    assert rejected
    assert all(r.levelno == logging.WARNING for r in rejected)
    assert all("__system__" not in r.getMessage() for r in rejected)


# --- WebSocket (/v1/graph/ws and /v1/graph/live handshake) --------------------------------


def test_websocket_handshake_refuses_reserved_identity():
    app, _ = _app(_TokenAuth())
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/v1/graph/live", headers=_bearer("__system__")):
            pass
    assert exc.value.code == WS_POLICY_VIOLATION
    assert "__system__" not in (exc.value.reason or "")
    assert "reserved" in (exc.value.reason or "")


def test_websocket_query_token_fallback_is_refused_too():
    app, _ = _app(_TokenAuth())
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect("/v1/graph/live?token=__system__"):
            pass
    assert exc.value.code == WS_POLICY_VIOLATION


def test_websocket_accepts_ordinary_user():
    app, _ = _app(_TokenAuth())
    with TestClient(app).websocket_connect("/v1/graph/live", headers=_bearer("alice")) as conn:
        assert conn.receive_json()["user_id"] == "alice"


def test_live_recheck_fails_for_a_reserved_identity():
    """The periodic live/ws re-verification treats a reserved id as no longer valid."""
    container = _container(_TokenAuth())
    with patch("injectq.InjectQ.get_instance", return_value=container):
        assert ws_identity_still_valid(_request("alice"), {"user_id": "alice"})
        assert not ws_identity_still_valid(_request("__system__"), {"user_id": "__system__"})


# --- identity cache and rate-limit keying -------------------------------------------------


def test_cache_never_stores_or_returns_a_reserved_identity():
    backend = _ReusableTokenAuth()
    req = _request("tok")
    remember_identity(req, backend, "tok", {"user_id": "__system__"})
    assert recall_identity(req, backend, "tok") is None

    # an entry that somehow got in (older state, another writer) is still not served
    req.scope["state"]["tenxgraph_verified_identity"] = (
        id(backend),
        "tok",
        {"user_id": "__system__"},
    )
    assert recall_identity(req, backend, "tok") is None


def test_rate_limit_key_does_not_use_a_reserved_user():
    container = _container(_ReusableTokenAuth())
    with patch("injectq.InjectQ.get_instance", return_value=container):
        assert keying.verified_user_id(_request("alice")) == "alice"
        assert keying.verified_user_id(_request("__system__")) is None
        assert keying.verified_user_id(_request("  __system__")) is None


def test_reusable_backend_with_rate_limit_by_user_is_still_refused():
    auth = _ReusableTokenAuth()
    app, container = _app(auth, rate_limit_by_user=True)
    with patch("injectq.InjectQ.get_instance", return_value=container):
        client = TestClient(app)
        resp = client.get("/v1/threads", headers=_bearer("__system__"))
        ok = client.get("/v1/threads", headers=_bearer("alice"))
    assert resp.status_code == HTTP_UNAUTHORIZED
    assert "__system__" not in resp.text
    assert ok.status_code == HTTP_OK
