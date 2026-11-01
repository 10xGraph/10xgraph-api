"""JWT claim validation: user_id types and scope claims (``scope`` / ``scopes``)."""

# ruff: noqa: S101

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest
from fastapi import Response
from fastapi.security import HTTPAuthorizationCredentials

from tenxgraph_api.src.app.core.auth.authorization import (
    DefaultAuthorizationBackend,
    ScopeClaimError,
    scopes_from_claims,
)
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.jwt_auth import JwtAuth
from tenxgraph_api.src.app.core.auth.revocation import (
    TokenRevocationConfig,
    revoke_token,
    revoke_user,
    token_revoked,
)
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.core.exceptions.user_exception import UserAccountError
from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router
from tests.integration_tests.conftest import build_app, make_client


SECRET = "test-super-secret-key-for-testing-purposes"
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403


@pytest.fixture(autouse=True)
def jwt_env():
    get_settings.cache_clear()
    with patch.dict(os.environ, {"JWT_SECRET_KEY": SECRET, "JWT_ALGORITHM": "HS256"}):
        yield
    get_settings.cache_clear()


def _token(**claims) -> str:
    payload = {"exp": datetime.now(UTC) + timedelta(hours=1), **claims}
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _authenticate(**claims) -> dict:
    credential = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_token(**claims))
    return JwtAuth().authenticate(None, Response(), credential)


# ---------------------------------------------------------------------------
# user_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [None, "", "   ", True, False, 1.5, 0.0, ["a"], [], {"a": 1}, {}], ids=repr
)
def test_bad_user_id_types_are_401(bad):
    with pytest.raises(UserAccountError) as exc:
        _authenticate(user_id=bad)
    assert exc.value.status_code == HTTP_UNAUTHORIZED
    assert exc.value.error_code == "INVALID_TOKEN"


def test_int_user_id_is_stored_as_a_string():
    assert _authenticate(user_id=7)["user_id"] == "7"


def test_string_user_id_is_unchanged():
    assert _authenticate(user_id="alice")["user_id"] == "alice"


@pytest.mark.asyncio
async def test_jti_revocation_is_checked_with_an_odd_user_id():
    store, on = InMemoryCacheStore(), TokenRevocationConfig()
    await revoke_token(store, "t-1")
    for odd in (None, "", 0, ["x"], {"a": 1}):
        assert await token_revoked({"user_id": odd, "jti": "t-1"}, store, on) is True
    assert await token_revoked({"user_id": "", "jti": "other"}, store, on) is False


@pytest.mark.asyncio
async def test_user_revocation_still_applies_to_a_valid_user_id():
    store, on = InMemoryCacheStore(), TokenRevocationConfig()
    await revoke_user(store, "alice", before=100)
    assert await token_revoked({"user_id": "alice", "iat": 50}, store, on) is True
    assert await token_revoked({"user_id": "bob", "iat": 50}, store, on) is False


# ---------------------------------------------------------------------------
# scope claims
# ---------------------------------------------------------------------------


def test_scope_claim_string_is_split():
    assert scopes_from_claims({"scope": "graph:read graph:invoke"}) == [
        "graph:invoke",
        "graph:read",
    ]


def test_scopes_string_and_list_forms_merge_into_exact_names():
    user = {"scope": "graph:read", "scopes": ["graph:invoke", "graph:read"]}
    assert scopes_from_claims(user) == ["graph:invoke", "graph:read"]
    assert scopes_from_claims({"scopes": "graph:read graph:invoke"}) == [
        "graph:invoke",
        "graph:read",
    ]
    assert scopes_from_claims({"scopes": ("a:b",)}) == ["a:b"]


def test_no_scope_claim_is_unrestricted():
    assert scopes_from_claims({"user_id": "a"}) is None
    assert DefaultAuthorizationBackend().scopes_for({"user_id": "a"}) is None


def test_scope_names_match_exactly_not_by_substring():
    granted = DefaultAuthorizationBackend().scopes_for({"scopes": "xgraph:invoke_admin"})
    assert "graph:invoke" not in granted
    assert "xgraph:invoke_admin" in granted


@pytest.mark.parametrize("bad", [5, 1.5, {"a": 1}, ["graph:read", 3], [["x"]], True])
def test_wrong_type_scope_claim_raises(bad):
    with pytest.raises(ScopeClaimError):
        scopes_from_claims({"scope": bad})
    with pytest.raises(ScopeClaimError):
        scopes_from_claims({"scopes": bad})


@pytest.mark.parametrize("claim", ["scope", "scopes"])
def test_jwt_with_wrong_type_scope_claim_is_401(claim):
    with pytest.raises(UserAccountError) as exc:
        _authenticate(user_id="a", **{claim: 5})
    assert exc.value.status_code == HTTP_UNAUTHORIZED


def test_jwt_keeps_scope_claim_for_scopes_for():
    user = _authenticate(user_id="a", scope="graph:read")
    assert DefaultAuthorizationBackend().scopes_for(user) == ["graph:read"]


# ---------------------------------------------------------------------------
# over HTTP with a real JWT
# ---------------------------------------------------------------------------


def _client():
    return make_client(
        build_app(routers=[checkpointer_router], extra_bindings={BaseAuth: JwtAuth()})
    )


def _get(client, **claims):
    headers = {"Authorization": f"Bearer {_token(**claims)}"}
    return client.get("/v1/threads/t1/state", headers=headers)


def test_singular_scope_claim_restricts_the_token():
    client = _client()
    assert _get(client, user_id="a", scope="graph:read").status_code == HTTP_FORBIDDEN
    assert _get(client, user_id="a", scope="graph:read checkpointer:read").status_code == HTTP_OK


def test_scopes_string_uses_set_membership():
    client = _client()
    assert _get(client, user_id="a", scopes="checkpointer:read graph:invoke").status_code == (
        HTTP_OK
    )
    # A substring of a longer scope name does not grant anything.
    assert _get(client, user_id="a", scopes="xcheckpointer:read_admin").status_code == (
        HTTP_FORBIDDEN
    )


def test_no_scope_claim_is_unrestricted_over_http():
    assert _get(_client(), user_id="a").status_code == HTTP_OK


def test_custom_auth_with_wrong_type_scope_claim_is_401():
    class _BadScopeAuth(BaseAuth):
        def authenticate(self, request, response, credential):
            return {"user_id": "a", "scope": {"graph:read": True}}

    client = make_client(
        build_app(routers=[checkpointer_router], extra_bindings={BaseAuth: _BadScopeAuth()})
    )
    assert client.get("/v1/threads/t1/state").status_code == HTTP_UNAUTHORIZED
