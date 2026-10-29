"""JWT key rotation (JWT_SECRET_KEYS with kid) and the optional maximum token age."""

# ruff: noqa: S101, S105, S106, PLR2004

import time
from unittest.mock import patch

import jwt
import pytest
from fastapi import Response
from fastapi.security import HTTPAuthorizationCredentials

from tenxgraph_api.src.app.core.auth.jwt_auth import JwtAuth, check_jwt_settings
from tenxgraph_api.src.app.core.config.settings import Settings
from tenxgraph_api.src.app.core.exceptions import UserAccountError


OLD = "o" * 48
NEW = "n" * 48
OTHER = "x" * 48


def _settings(**overrides) -> Settings:
    values = {
        "ALLOWED_HOST": "api.example.com",
        "ORIGINS": "https://app.example.com",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _auth(secret: str, settings: Settings, headers: dict | None = None, **claims):
    token = jwt.encode(
        {"user_id": "u1", "exp": int(time.time()) + 60, **claims},
        secret,
        algorithm="HS256",
        headers=headers,
    )
    credential = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with patch("tenxgraph_api.src.app.core.auth.jwt_auth.get_settings", return_value=settings):
        return JwtAuth().authenticate(None, Response(), credential)


def _rotating() -> Settings:
    return _settings(
        JWT_SECRET_KEY=OLD,
        JWT_SECRET_KEYS=[{"kid": "2026-10", "key": NEW}],
    )


def test_old_and_new_key_both_verify_during_rotation():
    settings = _rotating()
    assert _auth(OLD, settings)["user_id"] == "u1"
    assert _auth(NEW, settings, headers={"kid": "2026-10"})["user_id"] == "u1"
    assert _auth(NEW, settings)["user_id"] == "u1"  # no kid: every key is tried


def test_kid_selects_only_the_matching_key():
    settings = _settings(JWT_SECRET_KEYS=[{"kid": "a", "key": OLD}, {"kid": "b", "key": NEW}])
    with pytest.raises(UserAccountError) as exc:
        _auth(OLD, settings, headers={"kid": "b"})  # signed with a's key but claims b
    assert exc.value.status_code == 401
    assert _auth(OLD, settings, headers={"kid": "a"})["user_id"] == "u1"


def test_unknown_kid_falls_back_to_keys_without_kid_only():
    settings = _settings(JWT_SECRET_KEY=OLD, JWT_SECRET_KEYS=[{"kid": "a", "key": NEW}])
    assert _auth(OLD, settings, headers={"kid": "zzz"})["user_id"] == "u1"
    with pytest.raises(UserAccountError):
        _auth(NEW, settings, headers={"kid": "zzz"})


def test_unlisted_key_is_refused():
    with pytest.raises(UserAccountError) as exc:
        _auth(OTHER, _rotating())
    assert exc.value.status_code == 401


def test_keys_list_alone_is_enough_and_plain_strings_work():
    settings = _settings(JWT_SECRET_KEYS=[NEW])
    assert _auth(NEW, settings)["user_id"] == "u1"


def test_expired_token_is_still_expired_with_several_keys():
    with pytest.raises(UserAccountError) as exc:
        _auth(NEW, _rotating(), exp=int(time.time()) - 5)
    assert exc.value.error_code == "EXPIRED_TOKEN"


def test_secret_key_alone_unchanged():
    settings = _settings(JWT_SECRET_KEY=OLD)
    assert _auth(OLD, settings, headers={"kid": "anything"})["user_id"] == "u1"


def test_every_rotation_key_is_length_checked_in_production():
    settings = _settings(
        MODE="production", JWT_SECRET_KEY=OLD, JWT_SECRET_KEYS=[{"kid": "a", "key": "short"}]
    )
    with pytest.raises(ValueError, match="shorter"):
        check_jwt_settings(settings)


def test_max_token_age_off_by_default():
    old = int(time.time()) - 10_000_000
    assert _auth(OLD, _settings(JWT_SECRET_KEY=OLD), iat=old)["user_id"] == "u1"


def test_max_token_age_refuses_old_and_missing_iat():
    settings = _settings(JWT_SECRET_KEY=OLD, JWT_MAX_TOKEN_AGE_SECONDS=300)
    assert _auth(OLD, settings, iat=int(time.time()) - 10)["user_id"] == "u1"
    for claims in ({"iat": int(time.time()) - 1000}, {}):
        with pytest.raises(UserAccountError) as exc:
            _auth(OLD, settings, **claims)
        assert exc.value.status_code == 401
