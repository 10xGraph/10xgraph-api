"""``websocket.enabled`` in 10xgraph.json decides whether the WebSocket routes exist."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.routing import APIWebSocketRoute

from tenxgraph_api.src.app.core.auth.route_guard import _iter_routes, find_unprotected_routes
from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig
from tenxgraph_api.src.app.routers import setup_router


WS_PATHS = {"/v1/graph/ws", "/v1/graph/live"}


def _ws_paths(app: FastAPI) -> set[str]:
    # _iter_routes also walks FastAPI 0.139+'s lazy included-router wrappers.
    return {
        path for path, route, _ in _iter_routes(app.routes) if isinstance(route, APIWebSocketRoute)
    }


def _http_paths(app: FastAPI) -> set[str]:
    return {path for path, _, _ in _iter_routes(app.routes)} - WS_PATHS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, None),
        ({"enabled": True}, True),
        ({"enabled": False}, False),
        ({"enabled": "off"}, False),
    ],
)
def test_config_parses_enabled(raw, expected) -> None:
    assert WebSocketConfig.from_dict(raw).enabled is expected


def test_config_rejects_non_bool_enabled() -> None:
    with pytest.raises(ValueError, match="websocket.enabled"):
        WebSocketConfig.from_dict({"enabled": "sometimes"})


def test_disabled_leaves_out_both_websocket_routes_only(monkeypatch) -> None:
    monkeypatch.setattr(setup_router, "websocket_library_available", lambda: False)
    on, off = FastAPI(), FastAPI()
    setup_router.init_routes(on, evals=False, websocket=None)
    setup_router.init_routes(off, evals=False, websocket=False)

    assert _ws_paths(on) == WS_PATHS
    assert _ws_paths(off) == set()
    # Every HTTP route survives, still guarded; only the two WebSocket routes go.
    assert _http_paths(off) == _http_paths(on)
    assert len(_http_paths(off)) > 5
    assert find_unprotected_routes(off) == find_unprotected_routes(on)


def test_enabled_without_a_websocket_library_refuses_to_boot(monkeypatch) -> None:
    monkeypatch.setattr(setup_router, "websocket_library_available", lambda: False)
    with pytest.raises(RuntimeError, match=r"10xgraph-api\[websocket\]"):
        setup_router.init_routes(FastAPI(), evals=False, websocket=True)


def test_unset_without_a_websocket_library_warns_and_keeps_routes(monkeypatch, caplog) -> None:
    monkeypatch.setattr(setup_router, "websocket_library_available", lambda: False)
    app = FastAPI()
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api"):
        setup_router.init_routes(app, evals=False, websocket=None)

    assert _ws_paths(app) == WS_PATHS
    assert "10xgraph-api[websocket]" in caplog.text


def test_enabled_with_a_websocket_library_mounts_quietly(monkeypatch, caplog) -> None:
    monkeypatch.setattr(setup_router, "websocket_library_available", lambda: True)
    app = FastAPI()
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api"):
        setup_router.init_routes(app, evals=False, websocket=True)

    assert _ws_paths(app) == WS_PATHS
    assert "websocket" not in caplog.text.lower()
