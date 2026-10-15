"""Graph internals need the graph:read scope under graph_introspection "scoped"."""

# ruff: noqa: S101

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import APIRouter, Depends

from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

from .conftest import build_app, make_client, user_headers


class _Config:
    def __init__(self, scoped: bool) -> None:
        self.introspection_scoped = scoped

    def auth_config(self) -> str:
        return "custom"


def _client(scoped: bool):
    probe = APIRouter()

    @probe.get("/v1/graph")
    async def graph_details(
        user: dict[str, Any] = Depends(RequirePermission("graph", "read", introspection=True)),
    ):
        return {"ok": True}

    @probe.get("/v1/threads/{thread_id}/state")
    async def ordinary_read(
        thread_id: str,
        user: dict[str, Any] = Depends(RequirePermission("checkpointer", "read")),
    ):
        return {"ok": True}

    app = build_app(routers=[probe], extra_bindings={GraphConfig: _Config(scoped)})
    return make_client(app)


def test_scoped_refuses_an_identity_without_scopes():
    client = _client(scoped=True)
    assert client.get("/v1/graph", headers=user_headers("alice")).status_code == 403


def test_scoped_allows_an_identity_with_graph_read():
    client = _client(scoped=True)
    headers = {**user_headers("alice"), "X-Test-Scopes": "graph:read"}
    assert client.get("/v1/graph", headers=headers).status_code == 200
    headers = {**user_headers("alice"), "X-Test-Scopes": "graph:invoke"}
    assert client.get("/v1/graph", headers=headers).status_code == 403


def test_scoped_leaves_other_routes_permissive():
    client = _client(scoped=True)
    assert client.get("/v1/threads/t1/state", headers=user_headers("alice")).status_code == 200


def test_open_allows_any_authenticated_user():
    client = _client(scoped=False)
    assert client.get("/v1/graph", headers=user_headers("alice")).status_code == 200


def test_the_real_introspection_routes_are_marked():
    from tenxgraph_api.src.app.routers.graph.router import router

    marked = {}
    for route in router.routes:
        for dep in getattr(getattr(route, "dependant", None), "dependencies", []):
            call = dep.call
            if isinstance(call, RequirePermission):
                marked[route.path] = call.introspection
    assert marked["/v1/graph"] is True
    assert marked["/v1/graph/tools"] is True
    assert marked["/v1/graph:StateSchema"] is True
    assert marked["/v1/graph/invoke"] is False
    assert marked["/v1/observability/{thread_id}"] is False  # thread-scoped, ownership-checked


@pytest.mark.parametrize(
    ("mode", "value", "expected"),
    [
        ("production", None, "scoped"),
        ("development", None, "open"),
        ("production", "open", "open"),
        ("development", "scoped", "scoped"),
    ],
)
def test_graph_introspection_default(monkeypatch, tmp_path, mode, value, expected):
    from tenxgraph_api.src.app.core.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: type("S", (), {"MODE": mode})())
    data: dict[str, Any] = {"agent": "g:app"}
    if value is not None:
        data["graph_introspection"] = value
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps(data))
    config = GraphConfig(str(path))
    assert config.graph_introspection == expected
    assert config.introspection_scoped is (expected == "scoped")


def test_graph_introspection_rejects_unknown_values(tmp_path):
    from tenxgraph_api.cli.config_editor import validate_config

    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "g:app", "graph_introspection": "public"}')
    with pytest.raises(ValueError, match="graph_introspection"):
        _ = GraphConfig(str(path)).graph_introspection
    issues = validate_config({"agent": "g:app", "graph_introspection": "public"})
    assert [i["path"] for i in issues if i["level"] == "error"] == ["graph_introspection"]
