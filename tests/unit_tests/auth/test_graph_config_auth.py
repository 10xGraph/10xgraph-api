"""
Unit tests for GraphConfig.auth_config method.

Tests cover all JWT authentication configuration scenarios including:
- No auth configured
- JWT auth with valid environment variables
- JWT auth with missing environment variables
- Custom auth configuration
- Invalid auth configuration
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


class TestGraphConfigAuthConfig:
    """Test suite for GraphConfig.auth_config method."""

    @pytest.fixture
    def temp_config_file(self, tmp_path: Path):
        """Create a temporary config file and return a function to write to it."""

        def _create_config(config_data: dict) -> str:
            config_path = tmp_path / "10xgraph.json"
            with open(config_path, "w") as f:
                json.dump(config_data, f)
            return str(config_path)

        return _create_config

    # =========================================================================
    # Test: No auth configured
    # =========================================================================
    def test_auth_config_returns_none_when_auth_not_in_config(
        self,
        temp_config_file,
    ):
        """Test that auth_config returns None when auth is not in config."""
        config_path = temp_config_file({"agent": "path/to/agent.py"})
        graph_config = GraphConfig(path=config_path)

        result = graph_config.auth_config()

        assert result is None

    def test_auth_config_returns_none_when_auth_is_null(
        self,
        temp_config_file,
    ):
        """Test that auth_config returns None when auth is explicitly null."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": None})
        graph_config = GraphConfig(path=config_path)

        result = graph_config.auth_config()

        assert result is None

    def test_auth_config_returns_none_when_auth_is_empty_string(
        self,
        temp_config_file,
    ):
        """Test that auth_config returns None when auth is empty string."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": ""})
        graph_config = GraphConfig(path=config_path)

        result = graph_config.auth_config()

        assert result is None

    # =========================================================================
    # Test: JWT auth with valid environment variables
    # =========================================================================
    def test_auth_config_returns_jwt_method_when_jwt_string_and_env_vars_set(
        self,
        temp_config_file,
    ):
        """Test that auth_config returns JWT method when 'jwt' string and env vars are set."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": "jwt"})

        with patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "test-secret",
                "JWT_ALGORITHM": "HS256",
            },
        ):
            graph_config = GraphConfig(path=config_path)
            result = graph_config.auth_config()

            assert result == {"method": "jwt"}

    def test_auth_config_jwt_string_with_jwt_substring(
        self,
        temp_config_file,
    ):
        """A string that only contains 'jwt' is not the jwt method (G-117: exact match)."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": "my-jwt-auth"})

        with patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "test-secret",
                "JWT_ALGORITHM": "HS256",
            },
        ):
            graph_config = GraphConfig(path=config_path)
            with pytest.raises(ValueError, match="Unsupported auth method"):
                graph_config.auth_config()

    def test_auth_config_jwt_uppercase_raises_error(
        self,
        temp_config_file,
    ):
        """Test that 'JWT' (uppercase) is not recognized (case-sensitive check)."""
        # Note: The current implementation uses 'jwt' in res, which is case-sensitive
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": "JWT"})

        with patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "test-secret",
                "JWT_ALGORITHM": "HS256",
            },
        ):
            graph_config = GraphConfig(path=config_path)

            with pytest.raises(ValueError) as exc_info:
                graph_config.auth_config()

            assert "Unsupported auth method" in str(exc_info.value)

    # =========================================================================
    # Test: JWT auth with missing environment variables
    # =========================================================================
    def test_auth_config_raises_error_when_jwt_secret_key_missing(
        self,
        temp_config_file,
    ):
        """Test that auth_config raises ValueError when JWT_SECRET_KEY is missing."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": "jwt"})

        with patch.dict(
            os.environ,
            {"JWT_ALGORITHM": "HS256"},
            clear=True,
        ):
            graph_config = GraphConfig(path=config_path)

            with pytest.raises(ValueError) as exc_info:
                graph_config.auth_config()

            assert "JWT_SECRET_KEY" in str(exc_info.value)
            assert "JWT_ALGORITHM" in str(exc_info.value)

    def test_auth_config_raises_error_when_jwt_algorithm_missing(
        self,
        temp_config_file,
    ):
        """Test that auth_config raises ValueError when JWT_ALGORITHM is missing."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": "jwt"})

        with patch.dict(
            os.environ,
            {"JWT_SECRET_KEY": "test-secret"},
            clear=True,
        ):
            graph_config = GraphConfig(path=config_path)

            with pytest.raises(ValueError) as exc_info:
                graph_config.auth_config()

            assert "JWT_SECRET_KEY" in str(exc_info.value)
            assert "JWT_ALGORITHM" in str(exc_info.value)

    def test_auth_config_raises_error_when_both_jwt_env_vars_missing(
        self,
        temp_config_file,
    ):
        """Test that auth_config raises ValueError when both JWT env vars are missing."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": "jwt"})

        with patch.dict(os.environ, {}, clear=True):
            graph_config = GraphConfig(path=config_path)

            with pytest.raises(ValueError) as exc_info:
                graph_config.auth_config()

            assert "JWT_SECRET_KEY" in str(exc_info.value)
            assert "JWT_ALGORITHM" in str(exc_info.value)

    def test_auth_config_raises_error_when_jwt_secret_key_empty(
        self,
        temp_config_file,
    ):
        """Test that auth_config raises ValueError when JWT_SECRET_KEY is empty string."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": "jwt"})

        with patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "",  # Empty string
                "JWT_ALGORITHM": "HS256",
            },
        ):
            graph_config = GraphConfig(path=config_path)

            with pytest.raises(ValueError) as exc_info:
                graph_config.auth_config()

            assert "JWT_SECRET_KEY" in str(exc_info.value)

    def test_auth_config_raises_error_when_jwt_algorithm_empty(
        self,
        temp_config_file,
    ):
        """Test that auth_config raises ValueError when JWT_ALGORITHM is empty string."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": "jwt"})

        with patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "test-secret",
                "JWT_ALGORITHM": "",  # Empty string
            },
        ):
            graph_config = GraphConfig(path=config_path)

            with pytest.raises(ValueError) as exc_info:
                graph_config.auth_config()

            assert "JWT_ALGORITHM" in str(exc_info.value)

    # =========================================================================
    # Test: Custom auth configuration
    # =========================================================================
    def test_auth_config_returns_custom_method_when_custom_config_valid(
        self,
        temp_config_file,
        tmp_path: Path,
    ):
        """Test that auth_config returns custom method when custom config is valid."""
        # Create a temporary custom auth file
        custom_auth_path = tmp_path / "custom_auth.py"
        custom_auth_path.write_text("# Custom auth module")

        config_path = temp_config_file(
            {
                "agent": "path/to/agent.py",
                "auth": {"method": "custom", "path": str(custom_auth_path)},
            }
        )
        graph_config = GraphConfig(path=config_path)

        result = graph_config.auth_config()

        assert result == {"method": "custom", "path": str(custom_auth_path)}

    def test_auth_config_raises_error_when_dict_missing_method(
        self,
        temp_config_file,
        tmp_path: Path,
    ):
        """Test that auth_config raises ValueError when dict is missing method."""
        custom_auth_path = tmp_path / "custom_auth.py"
        custom_auth_path.write_text("# Custom auth module")

        config_path = temp_config_file(
            {
                "agent": "path/to/agent.py",
                "auth": {"path": str(custom_auth_path)},  # Missing method
            }
        )
        graph_config = GraphConfig(path=config_path)

        with pytest.raises(ValueError) as exc_info:
            graph_config.auth_config()

        assert "Both method and path must be provided" in str(exc_info.value)

    def test_auth_config_raises_error_when_dict_missing_path(
        self,
        temp_config_file,
    ):
        """Test that auth_config raises ValueError when dict is missing path."""
        config_path = temp_config_file(
            {
                "agent": "path/to/agent.py",
                "auth": {"method": "custom"},  # Missing path
            }
        )
        graph_config = GraphConfig(path=config_path)

        with pytest.raises(ValueError) as exc_info:
            graph_config.auth_config()

        assert "Both method and path must be provided" in str(exc_info.value)

    # =========================================================================
    # Test: Invalid auth configuration
    # =========================================================================
    def test_auth_config_raises_error_for_unsupported_string(
        self,
        temp_config_file,
    ):
        """Test that auth_config raises ValueError for unsupported string."""
        config_path = temp_config_file(
            {"agent": "path/to/agent.py", "auth": "oauth2"}  # Not supported
        )
        graph_config = GraphConfig(path=config_path)

        with pytest.raises(ValueError) as exc_info:
            graph_config.auth_config()

        assert "Unsupported auth method" in str(exc_info.value)

    def test_auth_config_raises_error_for_invalid_type(
        self,
        temp_config_file,
    ):
        """Test that auth_config raises ValueError for invalid auth type."""
        config_path = temp_config_file(
            {"agent": "path/to/agent.py", "auth": 123}  # Invalid type
        )
        graph_config = GraphConfig(path=config_path)

        with pytest.raises(ValueError) as exc_info:
            graph_config.auth_config()

        assert "Unsupported auth method" in str(exc_info.value)

    def test_auth_config_raises_error_for_list_type(
        self,
        temp_config_file,
    ):
        """Test that auth_config raises ValueError for list auth type."""
        config_path = temp_config_file(
            {"agent": "path/to/agent.py", "auth": ["jwt", "oauth2"]}  # Invalid type
        )
        graph_config = GraphConfig(path=config_path)

        with pytest.raises(ValueError) as exc_info:
            graph_config.auth_config()

        assert "Unsupported auth method" in str(exc_info.value)


class TestGraphConfigJwtEnvLoading:
    """Test that GraphConfig properly validates JWT configuration during auth_config call."""

    @pytest.fixture
    def temp_config_file(self, tmp_path: Path):
        """Create a temporary config file and return a function to write to it."""

        def _create_config(config_data: dict) -> str:
            config_path = tmp_path / "10xgraph.json"
            with open(config_path, "w") as f:
                json.dump(config_data, f)
            return str(config_path)

        return _create_config

    def test_jwt_env_vars_are_validated_at_auth_config_call_time(
        self,
        temp_config_file,
    ):
        """Test that JWT env vars are validated when auth_config is called, not at init."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": "jwt"})

        # GraphConfig init should not fail even without env vars
        with patch.dict(os.environ, {}, clear=True):
            graph_config = GraphConfig(path=config_path)
            # This should succeed - no validation at init time

        # But auth_config should fail
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError):
                graph_config.auth_config()

    def test_jwt_works_when_env_vars_set_after_init(
        self,
        temp_config_file,
    ):
        """Test that JWT works when env vars are set after GraphConfig init."""
        config_path = temp_config_file({"agent": "path/to/agent.py", "auth": "jwt"})

        # Init without env vars
        with patch.dict(os.environ, {}, clear=True):
            graph_config = GraphConfig(path=config_path)

        # Set env vars after init
        with patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "late-set-secret",
                "JWT_ALGORITHM": "HS256",
            },
        ):
            result = graph_config.auth_config()
            assert result == {"method": "jwt"}
