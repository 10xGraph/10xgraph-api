"""Unit tests for security configuration warnings."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import tenxgraph_api
from tenxgraph_api.src.app.core.config.settings import Settings, get_settings


SAFE_HOSTS = {"ALLOWED_HOST": "api.example.com", "ORIGINS": "https://app.example.com"}


def test_mode_normalization():
    """Test that MODE is normalized to lowercase."""
    with patch.dict(os.environ, {"MODE": "PRODUCTION", **SAFE_HOSTS}):
        settings = Settings()
        assert settings.MODE == "production"

    with patch.dict(os.environ, {"MODE": "Development"}):
        settings = Settings()
        assert settings.MODE == "development"


def test_cors_wildcard_warning_in_production(caplog):
    """Test warning for CORS wildcard in production."""
    with patch.dict(
        os.environ, {"MODE": "production", "ORIGINS": "*", "STRICT_PRODUCTION_CHECKS": "false"}
    ):
        settings = Settings()
        assert "CORS ORIGINS='*' in production" in caplog.text


def test_cors_wildcard_no_warning_in_development(caplog):
    """Test no warning for CORS wildcard in development."""
    with patch.dict(os.environ, {"MODE": "development", "ORIGINS": "*"}):
        settings = Settings()
        assert "CORS ORIGINS" not in caplog.text or "production" not in caplog.text


def test_debug_mode_is_forced_off_in_production(caplog):
    """IS_DEBUG=true is overridden in production, not just warned about."""
    with patch.dict(os.environ, {"MODE": "production", "IS_DEBUG": "true", **SAFE_HOSTS}):
        settings = Settings()
        assert settings.IS_DEBUG is False
        assert "IS_DEBUG=true is ignored in production" in caplog.text


def test_debug_mode_kept_in_development():
    with patch.dict(os.environ, {"MODE": "development", "IS_DEBUG": "true"}):
        assert Settings().IS_DEBUG is True


def test_docs_enabled_warning_in_production(caplog):
    """Test warning for API docs enabled in production."""
    with patch.dict(
        os.environ,
        {
            "MODE": "production",
            "DOCS_PATH": "/docs",
            **SAFE_HOSTS,
            "STRICT_PRODUCTION_CHECKS": "false",
        },
    ):
        settings = Settings()
        # Production turns docs off by default; the warning is for an explicit path.
        assert "API documentation endpoints are enabled" in caplog.text


def test_allowed_host_wildcard_warning_in_production(caplog):
    """Test warning for ALLOWED_HOST wildcard in production."""
    with patch.dict(
        os.environ, {"MODE": "production", "ALLOWED_HOST": "*", "STRICT_PRODUCTION_CHECKS": "false"}
    ):
        settings = Settings()
        assert "ALLOWED_HOST='*' in production" in caplog.text


def test_no_warnings_in_development(caplog):
    """Test that no security warnings appear in development mode."""
    with patch.dict(os.environ, {"MODE": "development"}):
        settings = Settings()
        assert "SECURITY WARNING" not in caplog.text


def test_multiple_warnings_in_production(caplog):
    """Test that multiple warnings can appear together."""
    with patch.dict(
        os.environ,
        {
            "MODE": "production",
            "ORIGINS": "*",
            "IS_DEBUG": "true",
            "ALLOWED_HOST": "*",
            "STRICT_PRODUCTION_CHECKS": "false",
        },
    ):
        settings = Settings()

        log_text = caplog.text
        assert "CORS ORIGINS='*'" in log_text
        assert "IS_DEBUG=true is ignored" in log_text
        assert "ALLOWED_HOST='*'" in log_text


def test_max_request_size_default():
    """Test that MAX_REQUEST_SIZE has correct default."""
    settings = Settings()
    assert settings.MAX_REQUEST_SIZE == 10 * 1024 * 1024  # 10MB


def test_max_request_size_configurable():
    """Test that MAX_REQUEST_SIZE is configurable via env var."""
    with patch.dict(os.environ, {"MAX_REQUEST_SIZE": "5242880"}):  # 5MB
        settings = Settings()
        assert settings.MAX_REQUEST_SIZE == 5242880


def test_settings_caching():
    """Test that get_settings returns cached instance."""
    # Clear cache first
    get_settings.cache_clear()

    settings1 = get_settings()
    settings2 = get_settings()

    assert settings1 is settings2


# ---------------------------------------------------------------------------
# STRICT_PRODUCTION_CHECKS
# ---------------------------------------------------------------------------


STRICT_SAFE = {
    "MODE": "production",
    "STRICT_PRODUCTION_CHECKS": "true",
    "ALLOWED_HOST": "api.example.com",
    "ORIGINS": "https://app.example.com",
}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"ALLOWED_HOST": "*"}, "ALLOWED_HOST='*'"),
        ({"ORIGINS": "*"}, "ORIGINS='*'"),
        ({"ORIGINS": "https://a.example.com,*"}, "ORIGINS='*'"),
        ({"ORIGINS": " *"}, "ORIGINS='*'"),
        ({"ALLOWED_HOST": " *"}, "ALLOWED_HOST='*'"),
        ({"ALLOWED_HOST": "api.example.com, *"}, "ALLOWED_HOST='*'"),
        ({"DOCS_PATH": "/docs"}, "documentation endpoints"),
    ],
)
def test_strict_mode_refuses_insecure_production_settings(override, message):
    with patch.dict(os.environ, {**STRICT_SAFE, **override}), pytest.raises(ValidationError) as exc:
        Settings()
    assert "Refusing to start" in str(exc.value)
    assert message in str(exc.value)


def test_strict_mode_accepts_hardened_production_settings():
    with patch.dict(os.environ, STRICT_SAFE):
        settings = Settings()
    assert settings.STRICT_PRODUCTION_CHECKS is True


def test_strict_mode_is_ignored_in_development():
    with patch.dict(
        os.environ, {"MODE": "development", "STRICT_PRODUCTION_CHECKS": "true", "ALLOWED_HOST": "*"}
    ):
        Settings()


def test_without_strict_mode_production_only_warns(caplog):
    with patch.dict(
        os.environ, {"MODE": "production", "ALLOWED_HOST": "*", "STRICT_PRODUCTION_CHECKS": "false"}
    ):
        Settings()
    assert "PRODUCTION SECURITY WARNINGS" in caplog.text


UNSAFE_PRODUCTION = {"MODE": "production", "ALLOWED_HOST": "*", "ORIGINS": "*"}


def test_strict_checks_are_on_by_default_in_production(monkeypatch):
    monkeypatch.delenv("STRICT_PRODUCTION_CHECKS", raising=False)
    with patch.dict(os.environ, UNSAFE_PRODUCTION), pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    message = str(exc.value)
    assert "Refusing to start" in message
    assert "STRICT_PRODUCTION_CHECKS=false" in message


def test_wildcard_allowed_host_alone_refuses_production_start(monkeypatch):
    monkeypatch.delenv("STRICT_PRODUCTION_CHECKS", raising=False)
    env = {"MODE": "production", "ORIGINS": "https://app.example.com", "ALLOWED_HOST": "*"}
    with patch.dict(os.environ, env), pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_explicit_false_opts_out_of_strict_checks(caplog):
    with patch.dict(os.environ, {**UNSAFE_PRODUCTION, "STRICT_PRODUCTION_CHECKS": "false"}):
        settings = Settings(_env_file=None)
    assert settings.STRICT_PRODUCTION_CHECKS is False
    assert "PRODUCTION SECURITY WARNINGS" in caplog.text


def test_strict_checks_default_on_in_production_off_in_development(monkeypatch):
    monkeypatch.delenv("STRICT_PRODUCTION_CHECKS", raising=False)
    safe = {"ALLOWED_HOST": "api.example.com", "ORIGINS": "https://app.example.com"}
    with patch.dict(os.environ, {"MODE": "production", **safe}):
        assert Settings(_env_file=None).STRICT_PRODUCTION_CHECKS is True
    with patch.dict(os.environ, {"MODE": "development", "ALLOWED_HOST": "*"}):
        assert Settings(_env_file=None).STRICT_PRODUCTION_CHECKS is False


def test_prod_env_example_has_no_wildcard_origin_or_host():
    path = Path(tenxgraph_api.__file__).parent / "cli" / "templates" / "prod" / ".env.example"
    values = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("ORIGINS", "ALLOWED_HOST"):
            values[key] = value.strip().strip('"')
    assert set(values) == {"ORIGINS", "ALLOWED_HOST"}
    assert all("*" not in value for value in values.values())
    with patch.dict(os.environ, {"MODE": "production", **values}):
        Settings(_env_file=None)  # strict checks pass with the template's values
