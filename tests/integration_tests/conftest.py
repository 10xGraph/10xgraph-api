"""Shared harness for API integration tests.

These build a real FastAPI app with the real ``RequirePermission`` dependency, a real
``BaseAuth`` backend and real services -- deliberately
NOT mocking the pieces that enforce isolation. That is the whole point: the earlier
suite mocked the services, so it could never have caught a cross-user (IDOR) defect.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer

from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RunsConfig
from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType
from tenxgraph_api.src.app.core.config.setup_middleware import setup_middleware
from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler


class HeaderAuth(BaseAuth):
    """Test auth backend: the caller's identity is the ``X-Test-User`` header.

    Lets a single TestClient impersonate different users so cross-user access can be
    exercised without minting JWTs.
    """

    def authenticate(
        self,
        request: Request,
        response: Response,
        credential: Any,
    ) -> dict[str, Any] | None:
        user_id = request.headers.get("X-Test-User")
        if not user_id:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="X-Test-User header required")
        info: dict[str, Any] = {"user_id": user_id}
        # Optional comma-separated scopes, so tests can exercise scope enforcement.
        scopes_header = request.headers.get("X-Test-Scopes")
        if scopes_header is not None:
            info["scopes"] = [s.strip() for s in scopes_header.split(",") if s.strip()]
        # Optional comma-separated roles, for the RBAC backend.
        roles_header = request.headers.get("X-Test-Role")
        if roles_header is not None:
            info["roles"] = [r.strip() for r in roles_header.split(",") if r.strip()]
        return info


class _AuthOnConfig:
    """Minimal GraphConfig stand-in whose ``auth_config()`` is truthy (auth enabled)."""

    def auth_config(self) -> str:
        return "custom"


class ClientThreadIdsConfig(_AuthOnConfig):
    """Auth on and ``runs.thread_ids: "client"``: a request may name a new thread id.

    Bind it with ``extra_bindings={GraphConfig: ClientThreadIdsConfig()}``.
    """

    runs = RunsConfig(thread_ids="client")


def build_app(
    *,
    routers: list[Any],
    auth: BaseAuth | None = None,
    checkpointer: BaseCheckpointer | None = None,
    extra_bindings: dict[type, Any] | None = None,
) -> FastAPI:
    """Assemble a test app with real auth wiring (``auth`` defaults to ``HeaderAuth``) and the given routers."""
    app = FastAPI()
    setup_middleware(app)
    # Same handlers as main.py, so a ClientInputError answers 422 as in production.
    init_errors_handler(app)

    container = InjectQ()
    container.bind_instance(GraphConfig, _AuthOnConfig())
    container.bind_instance(BaseAuth, auth or HeaderAuth())
    container.bind_instance(BaseCheckpointer, checkpointer or InMemoryCheckpointer())
    for typ, inst in (extra_bindings or {}).items():
        container.bind_instance(typ, inst)

    setup_fastapi(container, app)
    for router in routers:
        app.include_router(router)
    return app


@pytest.fixture
def memory_media_settings() -> MediaSettings:
    """MediaSettings backed by the in-memory store (no disk writes in tests)."""
    return MediaSettings(MEDIA_STORAGE_TYPE=MediaStorageType.MEMORY)  # type: ignore[call-arg]


def user_headers(user_id: str) -> dict[str, str]:
    return {"X-Test-User": user_id}


def make_client(app: FastAPI) -> TestClient:
    # raise_server_exceptions=False so a 500 is returned as a response we can assert on
    # instead of bubbling out of the client.
    return TestClient(app, raise_server_exceptions=False)
