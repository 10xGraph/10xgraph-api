"""API-layer scope enforcement: each endpoint requires its `<resource>:<action>` scope."""

from __future__ import annotations

from tenxgraph.storage.checkpointer import InMemoryCheckpointer

from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router

from .conftest import build_app, make_client


HTTP_OK = 200
HTTP_FORBIDDEN = 403
HTTP_UNPROCESSABLE = 422


def _headers(user_id: str, scopes: str | None) -> dict[str, str]:
    h = {"X-Test-User": user_id}
    if scopes is not None:
        h["X-Test-Scopes"] = scopes
    return h


def _client():
    app = build_app(routers=[checkpointer_router], checkpointer=InMemoryCheckpointer())
    return make_client(app)


def test_read_scope_allows_read():
    client = _client()
    r = client.get("/v1/threads/t1/state", headers=_headers("alice", "checkpointer:read"))
    assert r.status_code == HTTP_OK


def test_missing_read_scope_is_forbidden():
    client = _client()
    # Has write but not read -> reading is denied.
    r = client.get("/v1/threads/t1/state", headers=_headers("alice", "checkpointer:write"))
    assert r.status_code == HTTP_FORBIDDEN
    assert "checkpointer:read" in r.text


def test_delete_scope_required_for_delete():
    client = _client()
    # read+write but not delete -> deleting a thread is denied.
    r = client.request(
        "DELETE",
        "/v1/threads/t1",
        json={},
        headers=_headers("alice", "checkpointer:read,checkpointer:write"),
    )
    assert r.status_code == HTTP_FORBIDDEN
    assert "checkpointer:delete" in r.text


def test_delete_allowed_with_delete_scope():
    client = _client()
    r = client.request(
        "DELETE", "/v1/threads/t1", json={}, headers=_headers("alice", "checkpointer:delete")
    )
    assert r.status_code == HTTP_OK


def test_no_scopes_declared_is_permissive():
    # Backward compatible: an identity that declares no scopes is not scope-restricted.
    client = _client()
    r = client.get("/v1/threads/t1/state", headers=_headers("alice", None))
    assert r.status_code == HTTP_OK


def test_rbac_backend_maps_roles_to_scopes_end_to_end():
    """With the RBAC backend, roles decide scopes: a 'viewer' role grants
    checkpointer:read (GET works) but not checkpointer:delete (DELETE -> 403)."""
    import anyio
    from tenxgraph.storage.checkpointer import InMemoryCheckpointer as _Cp
    from tenxgraph.utils.thread_info import ThreadInfo

    from tenxgraph_api.src.app.core.auth.authorization import RoleBasedAuthorizationBackend

    checkpointer = _Cp()
    authz = RoleBasedAuthorizationBackend(
        {"viewer": ["checkpointer:read"], "admin": ["*"]},
        checkpointer=checkpointer,
    )
    for thread_id, owner in (("t1", "alice"), ("t2", "bob")):
        anyio.run(
            checkpointer.aput_thread,
            {"thread_id": thread_id, "user_id": owner},
            ThreadInfo(thread_id=thread_id, user_id=owner),
        )
    app = build_app(routers=[checkpointer_router], authz=authz, checkpointer=checkpointer)
    client = make_client(app)

    def roled(user_id: str, role: str) -> dict[str, str]:
        # HeaderAuth only sets user_id; supply the role via the scopes header is not
        # possible, so drive roles through a small header the test auth understands.
        return {"X-Test-User": user_id, "X-Test-Role": role}

    # We need the auth backend to surface the role. Extend via X-Test-Role below.
    r_read = client.get("/v1/threads/t1/state", headers=roled("alice", "viewer"))
    assert r_read.status_code == HTTP_OK
    r_del = client.request("DELETE", "/v1/threads/t1", json={}, headers=roled("alice", "viewer"))
    assert r_del.status_code == HTTP_FORBIDDEN
    r_admin = client.request("DELETE", "/v1/threads/t2", json={}, headers=roled("bob", "admin"))
    assert r_admin.status_code == HTTP_OK


def test_authz_block_drives_core_isolation_via_api():
    """End-to-end: the API stamps scope="owner"; the core checkpointer then isolates by
    owner. Uses a backend that allows every action (authorize=True) but declares owner
    isolation, so ONLY the stamped block -- not authorize() -- blocks the non-owner."""

    import anyio
    from tenxgraph.core.authz import build_authz
    from tenxgraph.core.state import AgentState

    from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend

    class _OwnerScopeBackend(AuthorizationBackend):
        async def authorize(self, user, resource, action, resource_id=None, **ctx):
            return True  # allow every action; isolation is enforced by the stamped block

        def isolation_scope(self) -> str:
            return "owner"

    cp = InMemoryCheckpointer()
    # Seed a state owned by alice (owner recorded because the config carries an owner policy).
    anyio.run(
        cp.aput_state,
        {"thread_id": "t1", "user_id": "alice", "authz": build_authz("alice", scope="owner")},
        AgentState(),
    )

    app = build_app(routers=[checkpointer_router], authz=_OwnerScopeBackend(), checkpointer=cp)
    client = make_client(app)

    # Owner reads their state; the API stamps scope=owner and the checkpointer returns it.
    owner = client.get("/v1/threads/t1/state", headers=_headers("alice", "checkpointer:read"))
    assert owner.status_code == HTTP_OK
    assert owner.json()["data"]["state"] is not None

    # Non-owner: same stamp, but the checkpointer isolates -> empty (not the other user's data).
    other = client.get("/v1/threads/t1/state", headers=_headers("mallory", "checkpointer:read"))
    assert other.status_code == HTTP_OK
    assert other.json()["data"]["state"] is None
