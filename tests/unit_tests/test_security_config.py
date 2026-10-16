"""Unit tests for security configuration warnings."""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from tenxgraph_api.src.app.core.config.settings import Settings, get_settings


def test_mode_normalization():
    """Test that MODE is normalized to lowercase."""
    with patch.dict(os.environ, {"MODE": "PRODUCTION"}):
        settings = Settings()
        assert settings.MODE == "production"

    with patch.dict(os.environ, {"MODE": "Development"}):
        settings = Settings()
        assert settings.MODE == "development"


def test_cors_wildcard_warning_in_production(caplog):
    """Test warning for CORS wildcard in production."""
    with patch.dict(os.environ, {"MODE": "production", "ORIGINS": "*"}):
        settings = Settings()
        assert "CORS ORIGINS='*' in production" in caplog.text


def test_cors_wildcard_no_warning_in_development(caplog):
    """Test no warning for CORS wildcard in development."""
    with patch.dict(os.environ, {"MODE": "development", "ORIGINS": "*"}):
        settings = Settings()
        assert "CORS ORIGINS" not in caplog.text or "production" not in caplog.text


def test_debug_mode_is_forced_off_in_production(caplog):
    """IS_DEBUG=true is overridden in production, not just warned about."""
    with patch.dict(os.environ, {"MODE": "production", "IS_DEBUG": "true"}):
        settings = Settings()
        assert settings.IS_DEBUG is False
        assert "IS_DEBUG=true is ignored in production" in caplog.text


def test_debug_mode_kept_in_development():
    with patch.dict(os.environ, {"MODE": "development", "IS_DEBUG": "true"}):
        assert Settings().IS_DEBUG is True


def test_docs_enabled_warning_in_production(caplog):
    """Test warning for API docs enabled in production."""
    with patch.dict(os.environ, {"MODE": "production", "DOCS_PATH": "/docs"}):
        settings = Settings()
        # Production turns docs off by default; the warning is for an explicit path.
        assert "API documentation endpoints are enabled" in caplog.text


def test_allowed_host_wildcard_warning_in_production(caplog):
    """Test warning for ALLOWED_HOST wildcard in production."""
    with patch.dict(os.environ, {"MODE": "production", "ALLOWED_HOST": "*"}):
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
    with patch.dict(os.environ, {"MODE": "production", "ALLOWED_HOST": "*"}):
        Settings()
    assert "PRODUCTION SECURITY WARNINGS" in caplog.text
