"""Permission enforcement: each thread route needs its ``threads:<action>`` permission."""

from __future__ import annotations

import pytest
from tenxgraph.core.authz import Identity
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils.thread_info import ThreadInfo

from tenxgraph_api.src.app.core.auth.rbac import RbacConfig
from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router

from .conftest import HeaderAuth, build_app, make_client


HTTP_FORBIDDEN = 403
HTTP_OK = 200

THREAD_ACTIONS = ("read", "write", "delete")

# (method, path, json body, permission the route requires)
ROUTES = [
    ("GET", "/v1/threads", None, "threads:read"),
    ("GET", "/v1/threads/t1", None, "threads:read"),
    ("GET", "/v1/threads/t1/state", None, "threads:read"),
    ("GET", "/v1/threads/t1/messages", None, "threads:read"),
    ("GET", "/v1/threads/t1/messages/m1", None, "threads:read"),
    ("PUT", "/v1/threads/t1/state", {"state": {}}, "threads:write"),
    ("PUT", "/v1/threads/t1/messages/m1", {"message": {}}, "threads:write"),
    ("DELETE", "/v1/threads/t1/state", {}, "threads:delete"),
    ("DELETE", "/v1/threads/t1/messages/m1", {}, "threads:delete"),
    ("DELETE", "/v1/threads/t1", {}, "threads:delete"),
]


def _headers(user_id: str, scopes: str | None = None) -> dict[str, str]:
    headers = {"X-Test-User": user_id}
    if scopes is not None:
        headers["X-Test-Scopes"] = scopes
    return headers


def _client(auth: HeaderAuth | None = None):
    # alice owns t1, so an allowed call reaches the thread instead of a 404.
    checkpointer = InMemoryCheckpointer()
    owner = {"thread_id": "t1", "user_id": "alice", "user": Identity(user_id="alice")}
    checkpointer.put_thread(owner, ThreadInfo(thread_id="t1"))
    app = build_app(routers=[checkpointer_router], auth=auth, checkpointer=checkpointer)
    return make_client(app)


def _call(client, method: str, path: str, body, headers):
    if body is None:
        return client.request(method, path, headers=headers)
    return client.request(method, path, json=body, headers=headers)


@pytest.mark.parametrize(("method", "path", "body", "needed"), ROUTES)
def test_token_without_the_permission_is_forbidden(method, path, body, needed):
    # A scope narrows the default grant: every threads permission except the needed one.
    scopes = ",".join(f"threads:{a}" for a in THREAD_ACTIONS if f"threads:{a}" != needed)
    r = _call(_client(), method, path, body, _headers("alice", scopes))
    assert r.status_code == HTTP_FORBIDDEN
    assert needed in r.text


@pytest.mark.parametrize(("method", "path", "body", "needed"), ROUTES)
def test_token_with_only_the_permission_is_not_forbidden(method, path, body, needed):
    r = _call(_client(), method, path, body, _headers("alice", needed))
    assert r.status_code != HTTP_FORBIDDEN


def test_token_with_a_resource_wildcard_scope_is_allowed():
    r = _call(_client(), "DELETE", "/v1/threads/t1", {}, _headers("alice", "threads:*"))
    assert r.status_code == HTTP_OK


def test_permissions_of_another_resource_do_not_help():
    r = _call(_client(), "GET", "/v1/threads/t1/state", None, _headers("alice", "store:read"))
    assert r.status_code == HTTP_FORBIDDEN


def test_token_without_scopes_gets_the_default_permissions():
    client = _client()
    assert client.get("/v1/threads/t1/state", headers=_headers("alice")).status_code == HTTP_OK
    r = _call(client, "DELETE", "/v1/threads/t1", {}, _headers("alice"))
    assert r.status_code == HTTP_OK


def test_missing_credentials_are_401_not_403():
    assert _client().get("/v1/threads/t1/state").status_code == 401


def test_rbac_roles_decide_permissions_end_to_end():
    """A ``viewer`` role holds threads:read (GET works) but not threads:delete (403);
    an ``admin`` role holds everything."""
    auth = HeaderAuth()
    auth.rbac = RbacConfig(
        roles={
            "viewer": {"permissions": ["threads:read"], "reach": "own"},
            "admin": {"permissions": ["*"], "reach": "own"},
        }
    )
    client = _client(auth)

    def roled(user_id: str, role: str) -> dict[str, str]:
        return {"X-Test-User": user_id, "X-Test-Role": role}

    assert client.get("/v1/threads/t1/state", headers=roled("alice", "viewer")).status_code == (
        HTTP_OK
    )
    denied = client.request("DELETE", "/v1/threads/t1", json={}, headers=roled("alice", "viewer"))
    assert denied.status_code == HTTP_FORBIDDEN
    assert "threads:delete" in denied.text
    allowed = client.request("DELETE", "/v1/threads/t1", json={}, headers=roled("alice", "admin"))
    assert allowed.status_code == HTTP_OK


def test_user_without_a_known_role_has_no_permissions():
    auth = HeaderAuth()
    auth.rbac = RbacConfig(roles={"viewer": {"permissions": ["threads:read"], "reach": "own"}})
    client = _client(auth)
    r = client.get(
        "/v1/threads/t1/state", headers={"X-Test-User": "alice", "X-Test-Role": "stranger"}
    )
    assert r.status_code == HTTP_FORBIDDEN


def test_custom_authorize_can_refuse_a_permission():
    class _NoDeletes(HeaderAuth):
        async def authorize(self, identity, permission, resource=None):
            return permission != "threads:delete" and await super().authorize(
                identity, permission, resource
            )

    client = _client(_NoDeletes())
    assert client.get("/v1/threads/t1/state", headers=_headers("alice")).status_code == HTTP_OK
    r = _call(client, "DELETE", "/v1/threads/t1", {}, _headers("alice"))
    assert r.status_code == HTTP_FORBIDDEN
