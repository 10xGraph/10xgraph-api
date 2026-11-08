"""JWT claim validation: user_id types."""

# ruff: noqa: S101

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest
from fastapi import Response
from fastapi.security import HTTPAuthorizationCredentials

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


SECRET = "test-super-secret-key-for-testing-purposes"
HTTP_UNAUTHORIZED = 401


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
