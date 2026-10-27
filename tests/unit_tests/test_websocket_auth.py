"""Regression tests for auth on WebSocket routes.

These drive ``RequirePermission`` through FastAPI's real dependency-injection path on a
``@app.websocket(...)`` route (not by calling the endpoint function directly). That is the
only way to catch the class of bug where a dependency declares ``request: Request`` and
FastAPI cannot inject it on a WebSocket connection -- the previous failure mode raised
``TypeError: __call__() missing 1 required positional argument: 'request'`` at connect time,
which endpoint-level unit tests (that pass ``user=...`` directly) could never surface.
"""

from typing import Any

from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations import setup_fastapi

from tenxgraph_api.src.app.core.auth.auth_backend import BaseAuth
from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend
from tenxgraph_api.src.app.core.auth.permissions import (
    RequirePermission,
    _extract_credential,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


class _FakeAuth(BaseAuth):
    """Authenticate by treating the bearer token as the user_id."""

    def authenticate(self, request, response, credential):  # type: ignore[override]
        if credential is None:
            return {}
        return {"user_id": credential.credentials}


class _AllowAuthz(AuthorizationBackend):
    def __init__(self):
        pass

    async def authorize(self, user, resource, action, resource_id=None):  # type: ignore[override]
        return True


def _build_client(auth_configured: bool) -> TestClient:
    container = InjectQ()

    config = type(
        "_Cfg",
        (),
        {"auth_config": staticmethod(lambda: "custom" if auth_configured else None)},
    )()
    container.bind_instance(GraphConfig, config, allow_none=True)
    container.bind_instance(BaseAuth, _FakeAuth(), allow_none=True)
    container.bind_instance(AuthorizationBackend, _AllowAuthz(), allow_none=True)

    app = FastAPI()
    setup_fastapi(container, app)

    @app.websocket("/ws")
    async def ws(
        websocket: WebSocket,
        user: dict[str, Any] = Depends(RequirePermission("graph", "stream")),
    ):
        await websocket.accept()
        await websocket.send_json(user)
        await websocket.close()

    return TestClient(app)


class TestWebSocketAuthResolves:
    def test_token_query_param_authenticates_on_websocket(self):
        """The ?token= fallback must resolve the dependency on a WS route (was a TypeError)."""
        client = _build_client(auth_configured=True)
        with client.websocket_connect("/ws?token=alice") as conn:
            assert conn.receive_json()["user_id"] == "alice"

    def test_authorization_header_authenticates_on_websocket(self):
        client = _build_client(auth_configured=True)
        with client.websocket_connect("/ws", headers={"Authorization": "Bearer bob"}) as conn:
            assert conn.receive_json()["user_id"] == "bob"

    def test_auth_not_configured_yields_empty_user_on_websocket(self):
        client = _build_client(auth_configured=False)
        with client.websocket_connect("/ws") as conn:
            assert conn.receive_json() == {}


class TestExtractCredential:
    def test_bearer_header_parsed(self):
        conn = type(
            "_C",
            (),
            {
                "headers": {"Authorization": "Bearer xyz"},
                "query_params": {},
                "scope": {"type": "http"},
            },
        )()
        cred = _extract_credential(conn)
        assert cred is not None
        assert cred.credentials == "xyz"
        assert cred.scheme == "Bearer"

    def test_query_token_fallback_when_no_header(self):
        conn = type(
            "_C",
            (),
            {"headers": {}, "query_params": {"token": "qtok"}, "scope": {"type": "websocket"}},
        )()
        cred = _extract_credential(conn)
        assert cred is not None
        assert cred.credentials == "qtok"

    def test_header_takes_priority_over_query(self):
        conn = type(
            "_C",
            (),
            {
                "headers": {"Authorization": "Bearer hdr"},
                "query_params": {"token": "q"},
                "scope": {"type": "websocket"},
            },
        )()
        assert _extract_credential(conn).credentials == "hdr"

    def test_non_bearer_scheme_ignored(self):
        conn = type(
            "_C",
            (),
            {
                "headers": {"Authorization": "Basic abc"},
                "query_params": {},
                "scope": {"type": "http"},
            },
        )()
        assert _extract_credential(conn) is None

    def test_no_credentials_returns_none(self):
        conn = type("_C", (), {"headers": {}, "query_params": {}, "scope": {"type": "http"}})()
        assert _extract_credential(conn) is None

    def test_subprotocol_token_accepted_on_websocket(self):
        conn = type(
            "_C",
            (),
            {
                "headers": {"sec-websocket-protocol": "10xgraph-bearer, sptok"},
                "query_params": {},
                "scope": {"type": "websocket"},
            },
        )()
        assert _extract_credential(conn).credentials == "sptok"

    def test_subprotocol_token_ignored_on_http(self):
        conn = type(
            "_C",
            (),
            {
                "headers": {"sec-websocket-protocol": "10xgraph-bearer, sptok"},
                "query_params": {},
                "scope": {"type": "http"},
            },
        )()
        assert _extract_credential(conn) is None

    def test_query_token_ignored_on_http(self):
        conn = type(
            "_C",
            (),
            {"headers": {}, "query_params": {"token": "qtok"}, "scope": {"type": "http"}},
        )()
        assert _extract_credential(conn) is None
