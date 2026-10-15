"""The thread-id guard reuses FastAPI's parsed body; it does not parse it again."""

# ruff: noqa: S101

from __future__ import annotations

import json
from typing import Any

import pytest
import starlette.requests
from fastapi import APIRouter, Depends

from tenxgraph_api.src.app.core.auth.permissions import RequirePermission

from .conftest import build_app, make_client, user_headers


@pytest.fixture
def counted_loads(monkeypatch):
    calls: list[int] = []
    real = json.loads

    def counting(raw, *args, **kwargs):
        calls.append(len(raw))
        return real(raw, *args, **kwargs)

    # Starlette's Request.json() is what both FastAPI and the guard call. This patches the
    # shared json module, so callers must read the count before parsing anything themselves.
    monkeypatch.setattr(starlette.requests.json, "loads", counting)
    return calls


def test_invoke_body_is_parsed_exactly_once(counted_loads):
    probe = APIRouter()

    @probe.post("/v1/graph/invoke")
    async def invoke(
        payload: dict,
        user: dict[str, Any] = Depends(RequirePermission("graph", "invoke")),
    ):
        return {"thread": payload["config"]["thread_id"]}

    client = make_client(build_app(routers=[probe]))
    body = {"messages": [], "config": {"thread_id": "t1"}}
    resp = client.post("/v1/graph/invoke", json=body, headers=user_headers("alice"))
    server_side = len(counted_loads)  # before the client parses the response
    assert resp.status_code == 200
    assert resp.json() == {"thread": "t1"}
    assert server_side == 1


def test_root_thread_id_body_is_parsed_exactly_once(counted_loads):
    """Same for routes that carry the thread at the body root (stop, fix)."""
    probe = APIRouter()

    @probe.post("/v1/graph/stop")
    async def stop(
        payload: dict,
        user: dict[str, Any] = Depends(RequirePermission("graph", "stop")),
    ):
        return {"ok": True}

    client = make_client(build_app(routers=[probe]))
    resp = client.post("/v1/graph/stop", json={"thread_id": "t1"}, headers=user_headers("a"))
    server_side = len(counted_loads)
    assert resp.status_code == 200
    assert server_side == 1
