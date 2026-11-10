"""Audit events: access (allowed, denied, not_found) and auth failures on HTTP and WebSocket."""

# ruff: noqa: S101

import asyncio
import importlib
import io
import json
import logging
from typing import Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations import setup_fastapi
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect
from tenxgraph.core.authz import Identity
from tenxgraph.core.exceptions import ThreadNotFoundError

from tenxgraph_api import AuditEvent, BaseAuditSink
from tenxgraph_api.src.app.core.audit import AuditConfig, configure_audit, set_sinks
from tenxgraph_api.src.app.core.audit.sinks import AUDIT_LOGGER_NAME, LogAuditSink
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.permissions import (
    WS_BEARER_SUBPROTOCOL,
    RequirePermission,
    _failure_reason,
    authorize_resource,
)
from tenxgraph_api.src.app.core.auth.revocation import (
    TokenRevocationConfig,
    TokenRevocationUnavailableError,
    revoke_token,
)
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.config.setup_middleware import RequestIDMiddleware
from tenxgraph_api.src.app.core.exceptions import UserAccountError
from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler
from tenxgraph_api.src.app.core.request_context import get_request_context
from tenxgraph_api.src.app.core.threads import require_thread


ROUTER = "tenxgraph_api.src.app.routers.graph.router"
WS_POLICY_VIOLATION = 1008
CLAIMS: dict[str, dict[str, Any]] = {
    "alice": {"user_id": "alice", "org": "acme"},
    "revoked": {"user_id": "bob", "jti": "dead"},
    "reserved": {"user_id": "__system__:memory"},
    "bad-scope": {"user_id": "carol", "scope": 5},
}


class _Auth(BaseAuth):
    def authenticate(self, request, response, credential):  # type: ignore[override]
        if credential is None:
            return None
        if credential.credentials == "invalid":
            raise HTTPException(status_code=401, detail="bad token")
        return CLAIMS.get(credential.credentials)


class _Deny(_Auth):
    async def authorize(self, identity, permission, resource=None):  # type: ignore[override]
        return False


class _NoThreads:
    """A checkpointer where no thread is visible."""

    async def aget_thread(self, config):
        return None

    async def aget_thread_owner(self, thread_id):
        return None


class _Recorder(BaseAuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Broken(BaseAuditSink):
    async def emit(self, event: AuditEvent) -> None:
        raise RuntimeError("sink down")


@pytest.fixture
def sink():
    recorder = _Recorder()
    set_sinks([recorder])
    yield recorder
    set_sinks([])


def _build(auth: BaseAuth | None) -> TestClient:
    container = InjectQ()
    container.bind_instance(BaseAuth, auth, allow_none=True)
    container.bind_instance(BaseCacheStore, InMemoryCacheStore())
    container.bind_instance(TokenRevocationConfig, TokenRevocationConfig())
    app = FastAPI()
    setup_fastapi(container, app)
    init_errors_handler(app)
    app.add_middleware(RequestIDMiddleware)

    @app.get("/threads/{thread_id}")
    async def read(
        thread_id: str, who: Identity | None = Depends(RequirePermission("threads", "read"))
    ):
        ctx = get_request_context()
        return {"ctx_user": ctx.user_id if ctx else None}

    @app.get("/missing/{thread_id}")
    async def missing(
        thread_id: str, who: Identity | None = Depends(RequirePermission("threads", "read"))
    ):
        await require_thread(_NoThreads(), who, thread_id)  # type: ignore[arg-type]

    @app.delete("/storage/{thread_id}")
    async def storage(
        thread_id: str, who: Identity | None = Depends(RequirePermission("threads", "delete"))
    ):
        raise ThreadNotFoundError(thread_id)  # a 404 raised by storage, not the lookup

    @app.delete("/files/{file_id}")
    async def file(
        file_id: str, who: Identity | None = Depends(RequirePermission("files", "delete"))
    ):
        raise HTTPException(status_code=404, detail="File not found")  # outside the reach

    @app.post("/invoke")
    async def invoke(
        body: dict, who: Identity | None = Depends(RequirePermission("graph", "invoke"))
    ):
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(
        websocket: WebSocket, who: Identity | None = Depends(RequirePermission("graph", "stream"))
    ):
        await websocket.accept(subprotocol=WS_BEARER_SUBPROTOCOL if who else None)
        frame = await websocket.receive_json()
        try:
            await authorize_resource(who, "graph:stream", frame["thread_id"])
            await require_thread(_NoThreads(), who, frame["thread_id"], run=True)  # type: ignore[arg-type]
            await websocket.send_json({"ok": True})
        except HTTPException as exc:
            await websocket.send_json({"error": exc.status_code})
        except ThreadNotFoundError:
            await websocket.send_json({"error": 404})
        await websocket.close()

    client = TestClient(app)
    client.container = container  # type: ignore[attr-defined]
    return client


def _get(client: TestClient, path: str, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(path, headers=headers)


def _ws(client: TestClient, token: str | None, thread_id: str = "t1") -> Any:
    subprotocols = [WS_BEARER_SUBPROTOCOL, token] if token else None
    try:
        with client.websocket_connect("/ws", subprotocols=subprotocols) as conn:
            conn.send_json({"thread_id": thread_id})
            return conn.receive_json()
    except WebSocketDisconnect as exc:
        return exc.code


def _summary(events: list[AuditEvent]) -> list[tuple]:
    return [(e.event, e.result, e.action, e.resource, e.reason, e.transport) for e in events]


# -- access on HTTP -----------------------------------------------------------


def test_allowed_http_carries_identity_and_request_context(sink):
    client = _build(_Auth())
    response = _get(client, "/threads/t1", "alice")
    assert response.status_code == 200
    assert response.json() == {"ctx_user": "alice"}  # set_request_user ran
    [event] = sink.events
    assert _summary([event]) == [("access", "allowed", "threads:read", "thread/t1", None, "http")]
    assert event.user_id == "alice"
    assert event.roles == []
    assert event.tenant == {}
    assert event.client_ip == "testclient"
    assert event.request_id == response.headers["X-Request-ID"]


def test_denied_http(sink):
    assert _get(_build(_Deny()), "/threads/t1", "alice").status_code == 403
    assert _summary(sink.events) == [
        ("access", "denied", "threads:read", "thread/t1", None, "http")
    ]


def test_not_found_from_thread_lookup_is_one_event(sink):
    assert _get(_build(_Auth()), "/missing/t9", "alice").status_code == 404
    assert _summary(sink.events) == [
        ("access", "allowed", "threads:read", "thread/t9", None, "http"),
        ("access", "not_found", "threads:read", "thread/t9", None, "http"),
    ]
    assert sink.events[1].user_id == "alice"


def test_not_found_raised_by_storage_is_recorded_by_the_404_handler(sink):
    client = _build(_Auth())
    response = client.delete("/storage/t9", headers={"Authorization": "Bearer alice"})
    assert response.status_code == 404
    assert _summary(sink.events)[-1] == (
        "access",
        "not_found",
        "threads:delete",
        "thread/t9",
        None,
        "http",
    )
    assert sink.events[-1].user_id == "alice"


def test_http_404_after_allowed_check_is_one_not_found(sink):
    client = _build(_Auth())
    response = client.delete("/files/f1", headers={"Authorization": "Bearer alice"})
    assert response.status_code == 404
    assert _summary(sink.events) == [
        ("access", "allowed", "files:delete", "file/f1", None, "http"),
        ("access", "not_found", "files:delete", "file/f1", None, "http"),
    ]
    assert sink.events[1].user_id == "alice"


def test_404_without_a_permission_check_emits_nothing(sink):
    assert _get(_build(_Auth()), "/no/such/route", "alice").status_code == 404
    assert sink.events == []


def test_auth_off_records_access_with_null_user(sink):
    assert _get(_build(None), "/threads/t1").status_code == 200
    [event] = sink.events
    assert (event.result, event.user_id, event.resource) == ("allowed", None, "thread/t1")


# -- auth failures --------------------------------------------------------------


def _revoke(client: TestClient) -> None:
    store = client.container.get(BaseCacheStore)  # type: ignore[attr-defined]
    asyncio.run(revoke_token(store, "dead"))


@pytest.mark.parametrize(
    ("token", "reason", "user_id"),
    [
        (None, "missing_token", None),
        ("invalid", "invalid_token", None),
        ("unknown", "invalid_token", None),
        ("revoked", "revoked", "bob"),
        ("reserved", "reserved_id", None),
        ("bad-scope", "claims_rejected", "carol"),
    ],
)
def test_auth_failures_on_http_and_ws(sink, token, reason, user_id):
    client = _build(_Auth())
    _revoke(client)
    sink.events.clear()  # the revocation itself is a change event
    assert _get(client, "/threads/t1", token).status_code == 401
    assert _ws(client, token) == WS_POLICY_VIOLATION
    assert [(e.event, e.result, e.reason, e.user_id) for e in sink.events] == [
        ("auth", "failed", reason, user_id)
    ] * 2
    assert [e.transport for e in sink.events] == ["http", "ws"]
    assert all(e.action is None and e.roles is None for e in sink.events)


def test_expired_token_reason():
    expired = UserAccountError(message="expired", error_code="EXPIRED_TOKEN", status_code=401)
    credential = type("C", (), {"credentials": "x"})()
    assert _failure_reason(credential, expired) == "expired"  # type: ignore[arg-type]
    assert _failure_reason(None, expired) == "missing_token"
    broken = UserAccountError(
        message="x", error_code="JWT_SETTINGS_NOT_CONFIGURED", status_code=500
    )
    assert _failure_reason(credential, broken) == "auth_error"  # type: ignore[arg-type]


# -- WebSocket ------------------------------------------------------------------


def test_ws_handshake_allowed_then_frame_thread_checks(sink):
    assert _ws(_build(_Auth()), "alice", "t7") == {"error": 404}
    assert _summary(sink.events) == [
        ("access", "allowed", "graph:stream", None, None, "ws"),  # handshake, no thread yet
        ("access", "allowed", "graph:stream", "thread/t7", None, "ws"),  # the run frame
        ("access", "not_found", "graph:stream", "thread/t7", None, "ws"),
    ]


def test_ws_handshake_denied(sink):
    assert _ws(_build(_Deny()), "alice") == WS_POLICY_VIOLATION
    assert _summary(sink.events) == [("access", "denied", "graph:stream", None, None, "ws")]


# -- switches and failure modes ---------------------------------------------------


def test_include_reads_false_keeps_denials_and_not_found():
    recorder = _Recorder()
    set_sinks([recorder], include_reads=False)
    try:
        assert _get(_build(_Auth()), "/threads/t1", "alice").status_code == 200
        assert _get(_build(_Auth()), "/missing/t2", "alice").status_code == 404
        assert _get(_build(_Deny()), "/threads/t1", "alice").status_code == 403
        assert _get(_build(_Auth()), "/threads/t1", None).status_code == 401
    finally:
        set_sinks([])
    assert [(e.event, e.result) for e in recorder.events] == [
        ("access", "not_found"),
        ("access", "denied"),
        ("auth", "failed"),
    ]


def test_disabled_emits_nothing(sink):
    configure_audit(AuditConfig(), auth_enabled=True)  # enabled: false (the default)
    client = _build(_Deny())
    _get(client, "/threads/t1", "alice")
    _get(client, "/threads/t1", None)
    _ws(client, "alice")
    assert sink.events == []


def test_sink_error_is_logged_and_the_request_continues(sink):
    set_sinks([_Broken(), sink])
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    app_logger = logging.getLogger("tenxgraph_api")
    app_logger.addHandler(handler)
    try:
        assert _get(_build(_Auth()), "/threads/t1", "alice").status_code == 200
    finally:
        app_logger.removeHandler(handler)
    assert "Audit sink _Broken failed: RuntimeError: sink down" in stream.getvalue()
    assert len(sink.events) == 1  # the next sink still got the event


# -- config -----------------------------------------------------------------------


def test_config_defaults_and_validation(tmp_path):
    config = AuditConfig()
    assert (config.enabled, config.sinks, config.include_reads) == (False, ["log"], True)
    AuditConfig(sinks=["log", "publisher", "my_pkg.audit:Sink"])
    for bad in ({"enabled": True, "level": 1}, {"sinks": []}, {"sinks": ["file"]}):
        with pytest.raises(ValidationError):
            AuditConfig.model_validate(bad)
    with pytest.raises(ValidationError):
        AuditConfig.model_validate({"enabled": "yes"})

    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps({"agent": "x:y", "audit": {"enabled": True, "typo": 1}}))
    with pytest.raises(ValidationError):
        _ = GraphConfig(str(path)).audit
    path.write_text(json.dumps({"agent": "x:y"}))
    assert GraphConfig(str(path)).audit.enabled is False


def test_configure_audit_warns_and_skips_publisher_without_publisher(caplog):
    app_logger = logging.getLogger("tenxgraph_api")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    app_logger.addHandler(handler)
    try:
        configure_audit(AuditConfig(enabled=True, sinks=["log", "publisher"]), auth_enabled=False)
        from tenxgraph_api.src.app.core.audit import audit_enabled

        assert audit_enabled()
    finally:
        app_logger.removeHandler(handler)
        set_sinks([])
    out = stream.getvalue()
    assert "auth is off" in out
    assert '"publisher" but no events publisher is configured' in out


# -- the log sink -------------------------------------------------------------------


def test_log_sink_writes_one_json_line_without_secrets():
    set_sinks([LogAuditSink()])
    handler = logging.getLogger(AUDIT_LOGGER_NAME).handlers[0]
    stream = io.StringIO()
    old = handler.setStream(stream)  # type: ignore[attr-defined]
    try:
        client = _build(_Auth())
        headers = {"Authorization": "Bearer alice"}
        prompt = "my secret prompt"
        assert (
            client.post("/invoke", json={"messages": [prompt]}, headers=headers).status_code == 200
        )
        _get(client, "/threads/t1", "invalid")
    finally:
        handler.setStream(old)  # type: ignore[attr-defined]
        set_sinks([])
    text = stream.getvalue()
    assert "Bearer" not in text and 'invalid"' not in text and prompt not in text
    lines = [json.loads(line) for line in text.splitlines()]
    assert [line["event"] for line in lines] == ["access", "auth"]
    assert set(lines[0]) == set(AuditEvent.model_fields)
    assert lines[0]["v"] == 1 and lines[0]["ts"].endswith("Z")
    assert lines[0]["action"] == "graph:invoke" and lines[0]["user_id"] == "alice"
    assert lines[1]["reason"] == "invalid_token" and lines[1]["user_id"] is None
    assert not logging.getLogger(AUDIT_LOGGER_NAME).propagate


# -- WebSocket re-checks during a session ---------------------------------------------


@pytest.mark.parametrize(
    ("valid", "revoked", "reason"),
    [
        (False, False, "invalid_token"),
        (True, True, "revoked"),
        (True, None, "revocation_unavailable"),
    ],
)
def test_ws_session_recheck_records_auth_failure(sink, monkeypatch, valid, revoked, reason):
    router = importlib.import_module(ROUTER)

    async def token_revoked(identity):
        if revoked is None:
            raise TokenRevocationUnavailableError("down")
        return revoked

    monkeypatch.setattr(router, "ws_identity_still_valid", lambda ws, user: valid)
    monkeypatch.setattr(router, "token_revoked", token_revoked)
    assert asyncio.run(router._ws_credential_error(None, Identity(user_id="alice")))
    assert [(e.event, e.reason, e.user_id) for e in sink.events] == [("auth", reason, "alice")]


def test_ws_session_recheck_passes_quietly(sink, monkeypatch):
    router = importlib.import_module(ROUTER)

    async def token_revoked(identity):
        return False

    monkeypatch.setattr(router, "ws_identity_still_valid", lambda ws, user: True)
    monkeypatch.setattr(router, "token_revoked", token_revoked)
    assert not asyncio.run(router._ws_credential_error(None, Identity(user_id="alice")))
    assert not asyncio.run(router._ws_credential_error(None, None))  # auth off
    assert sink.events == []
