"""Shared harness for API integration tests.

These build a real FastAPI app with the real ``RequirePermission`` dependency, a real
``BaseAuth`` backend, a real ``AuthorizationBackend`` and real services -- deliberately
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

from tenxgraph_api.src.app.core.auth.authorization import (
    AuthorizationBackend,
    DefaultAuthorizationBackend,
)
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
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


class OwnershipAuthz(AuthorizationBackend):
    """Authorizes only the owner of a resource.

    Records which ``resource_id`` belongs to which ``user_id``. A request with no
    ``resource_id`` (create/list) is allowed; a request for a resource owned by
    someone else is denied. This is the mechanism a developer wires up to get
    object-level isolation, and it depends on ``RequirePermission`` handing it the
    right ``resource_id`` -- including one carried in the request *body*.
    """

    def __init__(self) -> None:
        self.owners: dict[str, str] = {}
        self.seen_resource_ids: list[str | None] = []

    def own(self, resource_id: str, user_id: str) -> None:
        self.owners[str(resource_id)] = str(user_id)

    async def authorize(
        self,
        user: dict[str, Any],
        resource: str,
        action: str,
        resource_id: str | None = None,
        **context: Any,
    ) -> bool:
        self.seen_resource_ids.append(resource_id)
        if not user or "user_id" not in user:
            return False
        if resource_id is None:
            return True
        owner = self.owners.get(str(resource_id))
        # Unknown resource: allow (nothing to protect yet). Known resource: owner only.
        return owner is None or owner == str(user["user_id"])


class _AuthOnConfig:
    """Minimal GraphConfig stand-in whose ``auth_config()`` is truthy (auth enabled)."""

    def auth_config(self) -> str:
        return "custom"


def build_app(
    *,
    routers: list[Any],
    authz: AuthorizationBackend | None = None,
    checkpointer: BaseCheckpointer | None = None,
    extra_bindings: dict[type, Any] | None = None,
) -> FastAPI:
    """Assemble a test app with real auth/authz wiring and the given routers."""
    app = FastAPI()
    setup_middleware(app)
    # Same handlers as main.py, so a ClientInputError answers 422 as in production.
    init_errors_handler(app)

    container = InjectQ()
    container.bind_instance(GraphConfig, _AuthOnConfig())
    container.bind_instance(BaseAuth, HeaderAuth())
    container.bind_instance(AuthorizationBackend, authz or DefaultAuthorizationBackend())
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
