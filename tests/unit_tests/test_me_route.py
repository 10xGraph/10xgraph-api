"""``GET /v1/me``: the caller's identity, or ``{"auth": "off"}`` when auth is off."""

from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi

from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.rbac import RbacConfig
from tenxgraph_api.src.app.routers.me import router


class _HeaderAuth(BaseAuth):
    def authenticate(self, request: Request, response: Response, credential: Any) -> dict:
        user_id = request.headers.get("X-Test-User")
        if not user_id:
            raise HTTPException(status_code=401, detail="X-Test-User header required")
        return {"user_id": user_id, "roles": ["viewer"], "org_id": "org-a"}


def _client(auth: BaseAuth | None) -> TestClient:
    app = FastAPI()
    container = InjectQ()
    container.bind_instance(BaseAuth, auth, allow_none=True)
    setup_fastapi(container, app)
    app.include_router(router)
    return TestClient(app)


def test_me_returns_the_identity_with_sorted_permissions():
    auth = _HeaderAuth()
    auth.rbac = RbacConfig(
        tenant_claims=["org_id"],
        roles={"viewer": {"permissions": ["threads:read", "graph:read"], "reach": "tenant"}},
    )
    resp = _client(auth).get("/v1/me", headers={"X-Test-User": "alice"})
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "user_id": "alice",
        "tenant": {"org_id": "org-a"},
        "roles": ["viewer"],
        "permissions": ["graph:read", "threads:read"],
        "reach": "tenant",
    }


def test_me_without_credentials_is_401():
    assert _client(_HeaderAuth()).get("/v1/me").status_code == 401


def test_me_with_auth_off_says_so():
    resp = _client(None).get("/v1/me")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"auth": "off"}
