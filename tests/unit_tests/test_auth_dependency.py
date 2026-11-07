"""RequirePermission / RequireIdentity: one decision on HTTP and WebSocket."""

import asyncio
from typing import Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations import setup_fastapi
from starlette.websockets import WebSocketDisconnect
from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.permissions import (
    WS_BEARER_SUBPROTOCOL,
    RequireIdentity,
    RequirePermission,
)
from tenxgraph_api.src.app.core.auth.rbac import RbacConfig
from tenxgraph_api.src.app.core.auth.reserved_identity import identity_is_reserved
from tenxgraph_api.src.app.core.auth.revocation import (
    TokenRevocationConfig,
    revoke_token,
    token_revoked,
)
from tenxgraph_api.src.app.core.auth.route_guard import find_unprotected_routes
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore
from tenxgraph_api.src.app.core.validation.request_config import ClientInputError


WS_POLICY_VIOLATION = 1008

CLAIMS: dict[str, dict[str, Any]] = {
    "alice": {"user_id": "alice"},
    "no-id": {"email": "x@example.com"},
    "int-id": {"user_id": 7},
    "bad-scope": {"user_id": "alice", "scope": 5},
    "reserved": {"user_id": "__system__:memory"},
    "revoked": {"user_id": "bob", "jti": "dead"},
    "read-only": {"user_id": "carol", "perms": ["graph:read"]},
    "scoped": {"user_id": "dave", "scope": "graph:read"},
}


class _Auth(BaseAuth):
    reusable_within_request = True

    def authenticate(self, request, response, credential):  # type: ignore[override]
        if credential is None:
            return None
        if credential.credentials == "invalid":
            raise HTTPException(status_code=401, detail="bad token")
        if credential.credentials == "not-a-dict":
            return ["user_id"]  # type: ignore[return-value]
        return CLAIMS.get(credential.credentials)


class _Deny(_Auth):
    async def authorize(self, identity, permission, resource=None):  # type: ignore[override]
        return False


class _OnlyOwnThread(_Auth):
    """Custom rule that uses the resource id."""

    async def authorize(self, identity, permission, resource=None):  # type: ignore[override]
        return resource is None or resource.startswith(identity.user_id)


def _build(auth: BaseAuth | None) -> TestClient:
    container = InjectQ()
    container.bind_instance(BaseAuth, auth, allow_none=True)
    container.bind_instance(BaseCacheStore, InMemoryCacheStore())
    container.bind_instance(TokenRevocationConfig, TokenRevocationConfig())
    app = FastAPI()
    setup_fastapi(container, app)

    @app.post("/invoke")
    async def invoke(who: Identity | None = Depends(RequirePermission("graph", "invoke"))):
        return {"user_id": who.user_id if who else None}

    @app.get("/threads/{thread_id}")
    async def thread(
        thread_id: str, who: Identity | None = Depends(RequirePermission("threads", "read"))
    ):
        return {"user_id": who.user_id if who else None}

    @app.get("/read")
    async def read(who: Identity | None = Depends(RequirePermission("graph", "read"))):
        return {"user_id": who.user_id if who else None}

    @app.get("/me")
    async def me(who: Identity | None = Depends(RequireIdentity())):
        return {"user_id": who.user_id if who else None}

    @app.websocket("/ws")
    async def ws(
        websocket: WebSocket, who: Identity | None = Depends(RequirePermission("graph", "stream"))
    ):
        await websocket.accept(subprotocol=WS_BEARER_SUBPROTOCOL if who else None)
        await websocket.send_json({"user_id": who.user_id if who else None})
        await websocket.close()

    client = TestClient(app)
    client.container = container  # type: ignore[attr-defined]
    return client


def _http(client: TestClient, token: str | None, path: str = "/invoke"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    method = "post" if path == "/invoke" else "get"
    return getattr(client, method)(path, headers=headers)


def _ws(client: TestClient, token: str | None) -> int | str:
    """The WebSocket outcome: the user id when accepted, else the close code."""
    subprotocols = [WS_BEARER_SUBPROTOCOL, token] if token else None
    try:
        with client.websocket_connect("/ws", subprotocols=subprotocols) as conn:
            return conn.receive_json()["user_id"]
    except WebSocketDisconnect as exc:
        return exc.code


def _revoke(client: TestClient, jti: str) -> None:
    store = client.container.get(BaseCacheStore)  # type: ignore[attr-defined]
    asyncio.run(revoke_token(store, jti))


# -- auth off ---------------------------------------------------------------


def test_auth_off_returns_none_and_checks_nothing():
    client = _build(None)
    assert _http(client, None).json() == {"user_id": None}
    assert _http(client, "anything", "/me").json() == {"user_id": None}
    assert _ws(client, None) is None


# -- 401, same on HTTP and WebSocket ----------------------------------------


@pytest.mark.parametrize(
    "token",
    [None, "invalid", "unknown", "not-a-dict", "no-id", "bad-scope", "reserved", "revoked"],
)
def test_refused_credentials_are_401_and_1008(token):
    client = _build(_Auth())
    _revoke(client, "dead")
    assert _http(client, token).status_code == 401
    assert _http(client, token, "/me").status_code == 401
    assert _ws(client, token) == WS_POLICY_VIOLATION


def test_valid_token_passes_on_both_transports():
    client = _build(_Auth())
    assert _http(client, "alice").json() == {"user_id": "alice"}
    assert _http(client, "alice", "/me").json() == {"user_id": "alice"}
    assert _ws(client, "alice") == "alice"


def test_revoked_token_is_refused_but_others_pass():
    client = _build(_Auth())
    assert _http(client, "revoked").status_code == 200
    _revoke(client, "dead")
    assert _http(client, "revoked").status_code == 401
    assert _http(client, "alice").status_code == 200


def test_integer_user_id_is_refused_not_coerced():
    # The auth class owns normalising ids (JwtAuth does); resolve() never guesses.
    assert _http(_build(_Auth()), "int-id").status_code == 401


# -- 403 --------------------------------------------------------------------


def test_missing_permission_is_403_on_both_transports():
    auth = _Auth()
    auth.rbac = RbacConfig(permissions_claim="perms")
    client = _build(auth)
    assert _http(client, "read-only", "/read").status_code == 200
    assert _http(client, "read-only", "/invoke").status_code == 403
    assert _ws(client, "read-only") == WS_POLICY_VIOLATION


def test_identity_route_needs_no_permission():
    auth = _Auth()
    auth.rbac = RbacConfig(permissions_claim="perms")
    assert _http(_build(auth), "read-only", "/me").status_code == 200


def test_token_scope_narrows_the_granted_permissions():
    client = _build(_Auth())  # default set grants graph:invoke
    assert _http(client, "alice").status_code == 200
    assert _http(client, "scoped", "/read").status_code == 200
    assert _http(client, "scoped", "/invoke").status_code == 403
    assert _ws(client, "scoped") == WS_POLICY_VIOLATION


# -- custom authorize -------------------------------------------------------


def test_custom_authorize_can_deny():
    client = _build(_Deny())
    assert _http(client, "alice").status_code == 403
    assert _http(client, "alice", "/me").status_code == 200  # no permission asked
    assert _ws(client, "alice") == WS_POLICY_VIOLATION


def test_custom_authorize_gets_the_resource_id():
    client = _build(_OnlyOwnThread())
    assert _http(client, "alice", "/threads/alice-1").status_code == 200
    assert _http(client, "alice", "/threads/mallory-1").status_code == 403


def test_bad_resource_id_is_refused_before_authorize():
    # ClientInputError is rendered as 422 by the app's error handlers (not in this tiny app).
    with pytest.raises(ClientInputError):
        _http(_build(_Auth()), "alice", "/threads/a$b")


# -- construction and route guard -------------------------------------------


@pytest.mark.parametrize("pair", [("graph", "invok"), ("grap", "invoke"), ("checkpointer", "read")])
def test_unknown_permission_raises_at_construction(pair):
    with pytest.raises(ValueError, match="Unknown permission"):
        RequirePermission(*pair)


def test_route_guard_accepts_both_dependencies_and_refuses_an_unguarded_route():
    app = _build(None).app
    assert find_unprotected_routes(app) == []

    @app.get("/open")
    async def open_route():
        return {}

    assert find_unprotected_routes(app) == ["GET /open"]


# -- reserved and revocation checks never pass an unknown type (AUTH-S0-10) ---


def test_reserved_check_takes_identity_or_dict_and_refuses_anything_else():
    assert identity_is_reserved(Identity(user_id="__system__:x"))
    assert identity_is_reserved({"user_id": "__system__"})
    assert not identity_is_reserved(Identity(user_id="alice"))
    for bad in (None, "__system__", ["__system__"]):
        with pytest.raises(TypeError):
            identity_is_reserved(bad)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_revocation_check_reads_an_identity_and_refuses_anything_else():
    store = InMemoryCacheStore()
    await revoke_token(store, "dead")
    on = TokenRevocationConfig()
    assert await token_revoked(Identity(user_id="bob", claims={"jti": "dead"}), store, on)
    assert not await token_revoked(Identity(user_id="bob", claims={"jti": "ok"}), store, on)
    for bad in (None, "bob", ["bob"]):
        with pytest.raises(TypeError):
            await token_revoked(bad, store, on)  # type: ignore[arg-type]
