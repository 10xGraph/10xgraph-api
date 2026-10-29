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


# G-117: algorithm allowlist, PyJWT checked at boot, exact auth parse


@pytest.mark.parametrize("algorithm", ["none", "NONE", "None", "", "HS1", "hs256", "RS999"])
def test_unknown_or_none_algorithm_is_refused_at_boot(algorithm):
    with pytest.raises(ValueError, match="not allowed"):
        check_jwt_settings(_settings(JWT_ALGORITHM=algorithm))


@pytest.mark.parametrize("algorithm", ["HS256", "HS384", "HS512", "RS256", "ES256", "EdDSA"])
def test_allowed_algorithms_pass_boot(algorithm):
    check_jwt_settings(_settings(JWT_SECRET_KEY="x" * 64, JWT_ALGORITHM=algorithm))


def test_algorithm_none_never_authenticates():
    credential = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=jwt.encode({"user_id": "u1"}, None, algorithm="none")
    )
    with patch(
        "tenxgraph_api.src.app.core.auth.jwt_auth.get_settings",
        return_value=_settings(JWT_ALGORITHM="none"),
    ):
        with pytest.raises(UserAccountError) as exc:
            JwtAuth().authenticate(None, Response(), credential)
    assert exc.value.status_code == 500


def test_missing_pyjwt_fails_at_boot():
    with patch("tenxgraph_api.src.app.core.auth.jwt_auth.jwt", None):
        with pytest.raises(ImportError, match="PyJWT is required"):
            check_jwt_settings(_settings())


def test_auth_string_is_matched_exactly(monkeypatch):
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

    monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    assert GraphConfig._parse_auth_config("jwt") == {"method": "jwt"}
    for bad in ("JWT", " jwt", "no-jwt-here", "jwt_custom", "notjwt", "custom jwt"):
        with pytest.raises(ValueError, match="Unsupported auth method"):
            GraphConfig._parse_auth_config(bad)
