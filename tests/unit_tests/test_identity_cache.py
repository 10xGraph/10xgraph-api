"""One credential verification per request with rate_limit.by = "user"."""

# ruff: noqa: S101

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from starlette.requests import Request

from tenxgraph_api.src.app.core.auth.authorization import (
    AuthorizationBackend,
    DefaultAuthorizationBackend,
)
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.identity_cache import recall_identity, remember_identity
from tenxgraph_api.src.app.core.auth.jwt_auth import JwtAuth
from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RateLimitConfig
from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware


class _CountingAuthBase(BaseAuth):
    def __init__(self) -> None:
        self.calls = 0

    def authenticate(self, request, response, credential):
        self.calls += 1
        token = credential.credentials if credential else ""
        if not token.startswith("valid-"):
            raise HTTPException(status_code=401, detail="bad token")
        return {"user_id": token.removeprefix("valid-"), "scopes": None}


class _ReusableAuth(_CountingAuthBase):
    reusable_within_request = True

    def authenticate(self, request, response, credential):
        return super().authenticate(request, response, credential)


def _counting_auth(reusable: bool) -> BaseAuth:
    return _ReusableAuth() if reusable else _CountingAuthBase()


class _AuthOn:
    introspection_scoped = False

    def auth_config(self):
        return {"method": "custom", "path": "x:y"}


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def test_recall_returns_a_copy_for_the_same_backend_and_token():
    backend, req = _counting_auth(reusable=True), _request()
    remember_identity(req, backend, "tok", {"user_id": "alice", "roles": ["a"]})
    first = recall_identity(req, backend, "tok")
    assert first == {"user_id": "alice", "roles": ["a"]}
    first["authz"] = "stamped"  # callers mutate their copy
    assert "authz" not in recall_identity(req, backend, "tok")


def test_recall_misses_on_another_token_or_backend():
    backend, req = _counting_auth(reusable=True), _request()
    remember_identity(req, backend, "tok", {"user_id": "alice"})
    assert recall_identity(req, backend, "other") is None
    assert recall_identity(req, _counting_auth(reusable=True), "tok") is None


def test_backends_that_do_not_opt_in_are_never_cached():
    backend, req = _counting_auth(reusable=False), _request()
    remember_identity(req, backend, "tok", {"user_id": "alice"})
    assert recall_identity(req, backend, "tok") is None


def test_builtin_jwt_opts_in_and_base_auth_does_not():
    from tenxgraph_api.src.app.core.auth.identity_cache import _reusable

    assert _reusable(JwtAuth()) is True
    assert _reusable(_CountingAuthBase()) is False


def test_jwt_subclass_overriding_authenticate_is_not_reused():
    """It inherits JwtAuth's flag but not its purity (cookies, DB lookups...)."""
    from tenxgraph_api.src.app.core.auth.identity_cache import _reusable

    class _CookieJwt(JwtAuth):
        def authenticate(self, request, response, credential):
            response.set_cookie("session", "refreshed")
            return super().authenticate(request, response, credential)

    class _PlainJwt(JwtAuth):
        pass  # same authenticate as JwtAuth: still reusable

    class _OptedIn(_CookieJwt):
        reusable_within_request = True  # declared on the class, but authenticate is inherited

    assert _reusable(_CookieJwt()) is False
    assert _reusable(_PlainJwt()) is True
    assert _reusable(_OptedIn()) is False


# ---------------------------------------------------------------------------
# end to end: rate limiter + RequirePermission
# ---------------------------------------------------------------------------


def _client(auth: _CountingAuthBase) -> TestClient:
    container = InjectQ()
    container.bind_instance(GraphConfig, _AuthOn())
    container.bind_instance(BaseAuth, auth)
    container.bind_instance(AuthorizationBackend, DefaultAuthorizationBackend())

    app = FastAPI()
    config = RateLimitConfig.from_dict(
        {"enabled": True, "requests": 100, "window": 60, "by": "user"}
    )
    app.add_middleware(RateLimitMiddleware, config=config, cache=InMemoryCacheStore())
    setup_fastapi(container, app)

    @app.get("/v1/threads")
    async def threads(user: dict[str, Any] = Depends(RequirePermission("checkpointer", "read"))):
        return {"user_id": user["user_id"], "has_authz": "authz" in user}

    patcher = patch("injectq.InjectQ.get_instance", return_value=container)
    patcher.start()
    client = TestClient(app)
    client._patcher = patcher  # type: ignore[attr-defined]
    return client


@pytest.mark.parametrize(("reusable", "expected_calls"), [(True, 1), (False, 2)])
def test_token_is_verified_once_per_request_when_reusable(reusable, expected_calls):
    auth = _counting_auth(reusable)
    client = _client(auth)
    try:
        resp = client.get("/v1/threads", headers={"Authorization": "Bearer valid-alice"})
    finally:
        client._patcher.stop()  # type: ignore[attr-defined]
    assert resp.status_code == 200
    assert resp.json() == {"user_id": "alice", "has_authz": True}
    assert auth.calls == expected_calls


def test_invalid_token_is_still_rejected_by_the_route():
    auth = _counting_auth(reusable=True)
    client = _client(auth)
    try:
        resp = client.get("/v1/threads", headers={"Authorization": "Bearer forged"})
    finally:
        client._patcher.stop()  # type: ignore[attr-defined]
    assert resp.status_code == 401
    assert auth.calls == 2  # failures are never cached


# ---------------------------------------------------------------------------
# auth_config is parsed once per config value
# ---------------------------------------------------------------------------


def test_auth_config_is_cached_per_value(tmp_path, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "g:app", "auth": "jwt"}')
    config = GraphConfig(str(path))

    calls = []
    real = GraphConfig._parse_auth_config
    monkeypatch.setattr(
        GraphConfig, "_parse_auth_config", staticmethod(lambda res: calls.append(res) or real(res))
    )
    for _ in range(5):
        assert config.auth_config() == {"method": "jwt"}
    assert len(calls) == 1

    config.auth_config()["method"] = "tampered"  # callers get a copy
    assert config.auth_config() == {"method": "jwt"}

    config.data["auth"] = None  # a changed value is parsed again
    assert config.auth_config() is None
    assert len(calls) == 2
