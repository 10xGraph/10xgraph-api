"""Graph internals (schema, tools, state schema) need the graph:read permission."""

# ruff: noqa: S101

from __future__ import annotations

from fastapi import APIRouter, Depends
from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core.auth.permissions import RequirePermission

from .conftest import build_app, make_client, user_headers


def _client():
    probe = APIRouter()

    @probe.get("/v1/graph")
    async def graph_details(
        identity: Identity | None = Depends(RequirePermission("graph", "read")),
    ):
        return {"ok": True}

    app = build_app(routers=[probe])
    return make_client(app)


def test_graph_read_is_allowed_by_default():
    assert _client().get("/v1/graph", headers=user_headers("alice")).status_code == 200


def test_a_token_scoped_to_other_permissions_is_refused():
    client = _client()
    headers = {**user_headers("alice"), "X-Test-Scopes": "graph:read"}
    assert client.get("/v1/graph", headers=headers).status_code == 200
    headers = {**user_headers("alice"), "X-Test-Scopes": "graph:invoke"}
    assert client.get("/v1/graph", headers=headers).status_code == 403


def test_the_real_introspection_routes_need_graph_read():
    from tenxgraph_api.src.app.routers.graph.router import router

    needed = {}
    for route in router.routes:
        for dep in getattr(getattr(route, "dependant", None), "dependencies", []):
            if isinstance(dep.call, RequirePermission):
                needed[route.path] = dep.call.permission
    assert needed["/v1/graph"] == "graph:read"
    assert needed["/v1/graph/tools"] == "graph:read"
    assert needed["/v1/graph:StateSchema"] == "graph:read"
    assert needed["/v1/graph/invoke"] == "graph:invoke"
    assert needed["/v1/graph/stream"] == "graph:stream"
