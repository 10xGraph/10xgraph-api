"""Tests for Sentry configuration initialization."""

import logging
from unittest.mock import Mock, patch

from tenxgraph_api.src.app.core.config.sentry_config import init_sentry


class TestInitSentry:
    """Tests for init_sentry function."""

    def test_init_sentry_no_dsn(self):
        """Test Sentry init when DSN is not configured."""
        mock_settings = Mock()
        mock_settings.SENTRY_DSN = None
        mock_settings.MODE = "DEVELOPMENT"

        with patch("tenxgraph_api.src.app.core.config.sentry_config.logger") as mock_logger:
            init_sentry(mock_settings)
            mock_logger.warning.assert_called_once()

    def test_init_sentry_empty_dsn(self):
        """Test Sentry init when DSN is empty string."""
        mock_settings = Mock()
        mock_settings.SENTRY_DSN = ""
        mock_settings.MODE = "DEVELOPMENT"

        with patch("tenxgraph_api.src.app.core.config.sentry_config.logger") as mock_logger:
            init_sentry(mock_settings)
            mock_logger.warning.assert_called_once()

    def test_init_sentry_invalid_environment(self):
        """Test Sentry init with invalid environment."""
        mock_settings = Mock()
        mock_settings.SENTRY_DSN = "https://example@sentry.io/12345"
        mock_settings.MODE = "INVALID"

        with patch("tenxgraph_api.src.app.core.config.sentry_config.logger") as mock_logger:
            init_sentry(mock_settings)
            # Should warn about invalid environment
            mock_logger.warning.assert_called()




    def test_init_sentry_import_error(self):
        """Test Sentry init handles ImportError gracefully."""
        mock_settings = Mock()
        mock_settings.SENTRY_DSN = "https://example@sentry.io/12345"
        mock_settings.MODE = "production"

        with patch("tenxgraph_api.src.app.core.config.sentry_config.logger") as mock_logger:
            with patch("builtins.__import__", side_effect=ImportError("sentry_sdk not found")):
                init_sentry(mock_settings)
                # Should log warning about missing sentry_sdk
                mock_logger.warning.assert_called()






    def test_init_sentry_none_mode(self):
        """Test Sentry init when MODE is None."""
        mock_settings = Mock()
        mock_settings.SENTRY_DSN = "https://example@sentry.io/12345"
        mock_settings.MODE = None

        with patch("tenxgraph_api.src.app.core.config.sentry_config.logger") as mock_logger:
            init_sentry(mock_settings)
            # Should warn about invalid environment
            mock_logger.warning.assert_called()


def test_only_server_errors_are_reported_and_sampling_is_configurable():
    """L9: a 403 is not a Sentry event, and tracing is not forced to 100%."""
    from types import ModuleType

    from tenxgraph_api.src.app.core.config.settings import Settings

    captured = {}
    sdk = ModuleType("sentry_sdk")
    sdk.init = lambda **kwargs: captured.update(kwargs)
    modules = {"sentry_sdk": sdk}
    for name, cls in (("fastapi", "FastApiIntegration"), ("starlette", "StarletteIntegration")):
        module = ModuleType(f"sentry_sdk.integrations.{name}")
        setattr(module, cls, lambda **kwargs: kwargs)
        modules[f"sentry_sdk.integrations.{name}"] = module
    logging_module = ModuleType("sentry_sdk.integrations.logging")
    logging_module.LoggingIntegration = lambda **kwargs: {"logging": kwargs}
    modules["sentry_sdk.integrations.logging"] = logging_module
    modules["sentry_sdk.integrations"] = ModuleType("sentry_sdk.integrations")

    settings = Settings(
        SENTRY_DSN="https://example@sentry.io/1",
        MODE="production",
        ALLOWED_HOST="api.example.com",
        ORIGINS="https://app.example.com",
        SENTRY_TRACES_SAMPLE_RATE=0.25,
        _env_file=None,
    )
    with patch.dict("sys.modules", modules):
        init_sentry(settings)

    web = [i for i in captured["integrations"] if "failed_request_status_codes" in i]
    logging_kwargs = next(i["logging"] for i in captured["integrations"] if "logging" in i)
    assert logging_kwargs == {"level": logging.INFO, "event_level": logging.ERROR}
    assert captured["max_request_body_size"] == "never"
    assert captured["include_local_variables"] is False
    assert captured["send_default_pii"] is False
    for integration in web:
        codes = integration["failed_request_status_codes"]
        assert 403 not in codes
        assert {500, 502, 599} <= codes
    assert captured["traces_sample_rate"] == 0.25
    assert captured["profiles_sample_rate"] == 0.0
