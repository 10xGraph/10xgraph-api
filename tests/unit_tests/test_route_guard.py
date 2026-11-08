"""Tests for the boot-time route-protection invariant."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI

from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.auth.route_guard import (
    assert_all_routes_protected,
    find_unprotected_routes,
)


def _protected_app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/threads/{thread_id}/state")
    async def _state(
        thread_id: str,
        user: dict[str, Any] = Depends(RequirePermission("threads", "read")),
    ):
        return {}

    @app.get("/ping")
    async def _ping():
        return {"data": "pong"}

    return app


def _leaky_app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/threads/{thread_id}/state")
    async def _state(
        thread_id: str,
        user: dict[str, Any] = Depends(RequirePermission("threads", "read")),
    ):
        return {}

    @app.get("/v1/secret")  # <-- forgot RequirePermission
    async def _secret():
        return {"top": "secret"}

    return app


def test_protected_app_passes():
    app = _protected_app()
    assert find_unprotected_routes(app) == []
    assert_all_routes_protected(app)  # does not raise


def test_leaky_route_is_detected_and_blocks_boot():
    app = _leaky_app()
    unprotected = find_unprotected_routes(app)
    assert any("/v1/secret" in u for u in unprotected)
    with pytest.raises(RuntimeError) as exc:
        assert_all_routes_protected(app)
    assert "/v1/secret" in str(exc.value)


def test_public_allowlist_exempts_path():
    app = _leaky_app()
    # Explicitly declaring /v1/secret public makes the invariant pass.
    assert_all_routes_protected(app, public_paths=frozenset({"/ping", "/v1/secret"}))


def test_real_app_has_no_unprotected_routes():
    import tenxgraph_api.src.app.main as main_module

    assert find_unprotected_routes(main_module.app) == []


def _open_router() -> APIRouter:
    router = APIRouter(prefix="/v1/admin")

    @router.get("/dump")  # <-- forgot RequirePermission
    async def _dump():
        return {}

    return router


def test_leaky_route_in_an_included_router_is_detected():
    # FastAPI 0.139+ keeps included routers lazy, so app.routes holds no APIRoute for them.
    app = FastAPI()
    app.include_router(_open_router())
    assert find_unprotected_routes(app) == ["GET /v1/admin/dump"]


def test_nested_include_prefixes_are_combined():
    outer = APIRouter(prefix="/v2")
    outer.include_router(_open_router())
    app = FastAPI()
    app.include_router(outer, prefix="/api")
    assert find_unprotected_routes(app) == ["GET /api/v2/v1/admin/dump"]


def test_guard_given_at_include_time_protects_the_router():
    app = FastAPI()
    app.include_router(_open_router(), dependencies=[Depends(RequirePermission("graph", "read"))])
    assert find_unprotected_routes(app) == []


def test_real_app_routes_are_actually_checked():
    import tenxgraph_api.src.app.main as main_module
    from tenxgraph_api.src.app.core.auth.route_guard import _iter_routes

    paths = {path for path, _route, _guarded in _iter_routes(main_module.app.routes)}
    assert "/v1/graph/invoke" in paths
    assert "/v1/threads/{thread_id}/state" in paths
