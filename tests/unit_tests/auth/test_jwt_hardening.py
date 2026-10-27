"""JWT hardening (L1): 401 on bad credentials, optional iss/aud, no weak HMAC secrets."""

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


SECRET = "s" * 48


def _settings(**overrides) -> Settings:
    values = {
        "JWT_SECRET_KEY": SECRET,
        "JWT_ALGORITHM": "HS256",
        # Production refuses wildcard hosts/origins; these tests are about the JWT secret.
        "ALLOWED_HOST": "api.example.com",
        "ORIGINS": "https://app.example.com",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _authenticate(claims: dict, **settings):
    token = jwt.encode({"user_id": "u1", "exp": int(time.time()) + 60, **claims}, SECRET)
    credential = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with patch(
        "tenxgraph_api.src.app.core.auth.jwt_auth.get_settings",
        return_value=_settings(**settings),
    ):
        return JwtAuth().authenticate(None, Response(), credential)


def test_bad_token_is_401():
    credential = HTTPAuthorizationCredentials(scheme="Bearer", credentials="nope")
    with patch("tenxgraph_api.src.app.core.auth.jwt_auth.get_settings", return_value=_settings()):
        with pytest.raises(UserAccountError) as exc:
            JwtAuth().authenticate(None, Response(), credential)
    assert exc.value.status_code == 401


def test_issuer_and_audience_are_enforced_when_configured():
    ok = _authenticate(
        {"iss": "auth.example", "aud": "10xgraph"},
        JWT_ISSUER="auth.example",
        JWT_AUDIENCE="10xgraph",
    )
    assert ok["user_id"] == "u1"

    for claims in (
        {"iss": "other", "aud": "10xgraph"},
        {"aud": "10xgraph"},
        {"iss": "auth.example"},
    ):
        with pytest.raises(UserAccountError) as exc:
            _authenticate(claims, JWT_ISSUER="auth.example", JWT_AUDIENCE="10xgraph")
        assert exc.value.status_code == 401


def test_issuer_and_audience_are_optional():
    assert _authenticate({})["user_id"] == "u1"


def test_short_hmac_secret_is_refused_in_production():
    with pytest.raises(ValueError, match="shorter than 32 bytes"):
        check_jwt_settings(_settings(JWT_SECRET_KEY="short", MODE="production"))


def test_short_hmac_secret_only_warns_in_development(caplog):
    check_jwt_settings(_settings(JWT_SECRET_KEY="short", MODE="development"))


def test_asymmetric_algorithms_are_not_length_checked():
    check_jwt_settings(_settings(JWT_SECRET_KEY="pem", JWT_ALGORITHM="RS256", MODE="production"))


def test_strong_secret_passes():
    check_jwt_settings(_settings(MODE="production"))
