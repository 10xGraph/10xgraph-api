"""Tests for log sanitization utilities."""

import logging
import time

from tenxgraph_api.src.app.core.utils.log_sanitizer import (
    BEARER_PATTERN,
    JWT_PATTERN,
    SENSITIVE_PATTERNS,
    SanitizingFormatter,
    _sanitize_string,
    _sanitize_value,
    log_safe,
    sanitize_for_logging,
    sanitize_log_message,
)


class TestSensitivePatterns:
    """Tests for sensitive pattern detection."""

    def test_sensitive_patterns_contains_common_keywords(self):
        """Test that SENSITIVE_PATTERNS contains common sensitive keywords."""
        assert "token" in SENSITIVE_PATTERNS
        assert "password" in SENSITIVE_PATTERNS
        assert "secret" in SENSITIVE_PATTERNS
        assert "key" in SENSITIVE_PATTERNS
        assert "api_key" in SENSITIVE_PATTERNS

    def test_jwt_pattern_matches_valid_jwt(self):
        """Test JWT pattern matches valid JWT tokens."""
        valid_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        assert JWT_PATTERN.match(valid_jwt)

    def test_jwt_pattern_matches_short_jwt(self):
        """Test JWT pattern matches JWT with fewer parts."""
        short_jwt = "eyJhbGc.eyJzdWI"
        assert JWT_PATTERN.match(short_jwt)

    def test_jwt_pattern_is_linear_on_adversarial_input(self):
        """A string built to backtrack (CodeQL py/polynomial-redos) is rejected quickly."""
        evil = "-.-" + "--" * 20_000 + "!"
        start = time.perf_counter()
        assert JWT_PATTERN.match(evil) is None
        # The old pattern took seconds here; the fixed one takes well under a millisecond.
        assert time.perf_counter() - start < 0.5

    def test_bearer_pattern_matches_bearer_token(self):
        """Test BEARER_PATTERN matches bearer tokens."""
        bearer = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        assert BEARER_PATTERN.match(bearer)

    def test_bearer_pattern_case_insensitive(self):
        """Test BEARER_PATTERN is case insensitive."""
        bearer_lower = "bearer dGVzdHRva2Vu"
        bearer_upper = "BEARER dGVzdHRva2Vu"
        bearer_mixed = "BeArEr dGVzdHRva2Vu"

        assert BEARER_PATTERN.match(bearer_lower)
        assert BEARER_PATTERN.match(bearer_upper)
        assert BEARER_PATTERN.match(bearer_mixed)


class TestSanitizeString:
    """Tests for _sanitize_string function."""

    def test_sanitize_jwt_token(self):
        """Test JWT token detection and sanitization."""
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = _sanitize_string(jwt)
        assert result == "***JWT_TOKEN***"

    def test_sanitize_bearer_token(self):
        """Test bearer token detection and sanitization."""
        bearer = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = _sanitize_string(bearer)
        assert result == "***BEARER_TOKEN***"

    def test_sanitize_long_alphanumeric_string(self):
        """Test long alphanumeric string sanitization."""
        long_token = "a" * 40  # 40 alphanumeric characters
        result = _sanitize_string(long_token)
        assert result.startswith("aaaa")
        assert result.endswith("aaaa")
        assert "..." in result

    def test_sanitize_string_with_short_length(self):
        """Test short strings are not sanitized."""
        short = "hello"
        result = _sanitize_string(short)
        assert result == short

    def test_sanitize_string_with_dashes(self):
        """Test alphanumeric string with dashes."""
        token_with_dashes = "a-a-a-" + "a" * 35
        result = _sanitize_string(token_with_dashes)
        # Should be truncated
        assert len(result) < len(token_with_dashes)

    def test_sanitize_string_with_underscores(self):
        """Test alphanumeric string with underscores."""
        token_with_underscores = "a_a_a_" + "a" * 35
        result = _sanitize_string(token_with_underscores)
        # Should be truncated
        assert len(result) < len(token_with_underscores)

    def test_sanitize_non_alphanumeric_string(self):
        """Test strings with special characters are not sanitized."""
        text = "hello@world.com"
        result = _sanitize_string(text)
        assert result == text


class TestSanitizeValue:
    """Tests for _sanitize_value function."""

    def test_sanitize_value_with_token_key(self):
        """Test value with 'token' key is sanitized."""
        result = _sanitize_value("token", "secret_token_value", max_depth=10, current_depth=0)
        assert result == "***REDACTED***"

    def test_sanitize_value_with_password_key(self):
        """Test value with 'password' key is sanitized."""
        result = _sanitize_value("password", "my_password", max_depth=10, current_depth=0)
        assert result == "***REDACTED***"

    def test_sanitize_value_with_api_key(self):
        """Test value with 'api_key' key is sanitized."""
        result = _sanitize_value("api_key", "abcd1234efgh5678", max_depth=10, current_depth=0)
        assert result == "***REDACTED***"

    def test_sanitize_value_case_insensitive(self):
        """Test key matching is case insensitive."""
        result = _sanitize_value("TOKEN", "secret", max_depth=10, current_depth=0)
        assert result == "***REDACTED***"

    def test_sanitize_value_normal_key(self):
        """Test normal key/value is passed through."""
        result = _sanitize_value("user_id", "12345", max_depth=10, current_depth=0)
        assert result == "12345"

    def test_sanitize_value_partial_match(self):
        """Test keys with partial sensitive pattern match."""
        result = _sanitize_value("authorization", "Bearer token", max_depth=10, current_depth=0)
        assert result == "***REDACTED***"


class TestSanitizeForLogging:
    """Tests for sanitize_for_logging function."""

    def test_sanitize_dict_with_sensitive_keys(self):
        """Test sanitizing dictionary with sensitive keys."""
        data = {"user_id": "123", "token": "secret"}
        result = sanitize_for_logging(data)

        assert result["user_id"] == "123"
        assert result["token"] == "***REDACTED***"

    def test_sanitize_nested_dict(self):
        """Test sanitizing nested dictionaries."""
        data = {"user": {"id": "123", "password": "secret"}}
        result = sanitize_for_logging(data)

        assert result["user"]["id"] == "123"
        assert result["user"]["password"] == "***REDACTED***"

    def test_sanitize_list(self):
        """Test sanitizing lists."""
        data = ["value1", "token_value", "value3"]
        result = sanitize_for_logging(data)

        assert result[0] == "value1"
        assert result[2] == "value3"

    def test_sanitize_tuple(self):
        """Test sanitizing tuples."""
        data = ("value1", "normal_value", "value3")
        result = sanitize_for_logging(data)

        assert isinstance(result, tuple)
        assert result[0] == "value1"

    def test_sanitize_string(self):
        """Test sanitizing string values."""
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = sanitize_for_logging(jwt)

        assert result == "***JWT_TOKEN***"

    def test_sanitize_non_sensitive_types(self):
        """Test that non-sensitive types are preserved."""
        data = {"number": 42, "flag": True, "null": None}
        result = sanitize_for_logging(data)

        assert result["number"] == 42
        assert result["flag"] is True
        assert result["null"] is None

    def test_sanitize_max_depth(self):
        """Test max depth protection."""
        # Create deeply nested structure
        data = {"a": {"b": {"c": {"d": {"e": {"f": "value"}}}}}}
        result = sanitize_for_logging(data, max_depth=3)

        # Should stop at depth limit
        assert result["a"]["b"]["c"] == "***MAX_DEPTH_REACHED***"

    def test_sanitize_mixed_structure(self):
        """Test sanitizing mixed structure."""
        data = {"user": {"id": "123", "items": [{"token": "secret1"}, {"token": "secret2"}]}}
        result = sanitize_for_logging(data)

        assert result["user"]["id"] == "123"
        assert isinstance(result["user"]["items"], list)
        assert len(result["user"]["items"]) == 2
        # Each dict in list should have tokens redacted
        assert result["user"]["items"][0]["token"] == "***REDACTED***"
        assert result["user"]["items"][1]["token"] == "***REDACTED***"

    def test_sanitize_authorization_header(self):
        """Test sanitizing authorization header."""
        data = {"Authorization": "Bearer eyJhbGc..."}
        result = sanitize_for_logging(data)

        assert result["Authorization"] == "***REDACTED***"

    def test_sanitize_does_not_modify_original(self):
        """Test that original data is not modified."""
        original = {"token": "secret", "id": "123"}
        original_copy = original.copy()

        sanitize_for_logging(original)

        # Original should be unchanged
        assert original == original_copy
        assert original["token"] == "secret"


class TestSanitizeLogMessage:
    """Tests for sanitize_log_message function."""

    def test_sanitize_log_message_with_args(self):
        """Test sanitizing log message with positional args."""
        msg, args, kwargs = sanitize_log_message("User %s logged in", {"token": "secret"})

        assert msg == "User %s logged in"
        assert args[0]["token"] == "***REDACTED***"
        assert kwargs == {}

    def test_sanitize_log_message_with_kwargs(self):
        """Test sanitizing log message with keyword args."""
        # Note: kwargs passed directly to sanitize_for_logging won't check keys
        # They're sanitized individually as values
        msg, args, kwargs = sanitize_log_message(
            "User logged in", data={"token": "secret"}, user_id="123"
        )

        assert msg == "User logged in"
        assert args == ()
        # data is a dict, so its keys are checked
        assert kwargs["data"]["token"] == "***REDACTED***"
        assert kwargs["user_id"] == "123"

    def test_sanitize_log_message_multiple_args(self):
        """Test sanitizing log message with multiple args."""
        msg, args, kwargs = sanitize_log_message(
            "User %s with data %s", "john", {"token": "secret"}
        )

        assert msg == "User %s with data %s"
        assert args[0] == "john"
        assert args[1]["token"] == "***REDACTED***"

    def test_sanitize_log_message_nested_data(self):
        """Test sanitizing log message with nested data."""
        msg, args, kwargs = sanitize_log_message(
            "Auth data: %s", {"user": {"token": "secret", "id": "123"}}
        )

        assert msg == "Auth data: %s"
        assert args[0]["user"]["token"] == "***REDACTED***"
        assert args[0]["user"]["id"] == "123"


class TestSanitizingFormatter:
    """Tests for SanitizingFormatter class."""

    def test_sanitizing_formatter_initialization(self):
        """Test SanitizingFormatter initialization."""
        base_formatter = logging.Formatter("%(message)s")
        formatter = SanitizingFormatter(base_formatter)

        assert formatter.base_formatter is base_formatter

    def test_sanitizing_formatter_format(self):
        """Test SanitizingFormatter.format method."""
        base_formatter = logging.Formatter("%(message)s")
        formatter = SanitizingFormatter(base_formatter)

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Token: %s",
            args=({"password": "secret"},),
            exc_info=None,
        )

        # The formatter will sanitize the args
        original_msg = record.getMessage()
        result = formatter.format(record)
        # Just verify formatting works
        assert result is not None
        assert isinstance(result, str)

    def test_sanitizing_formatter_with_no_args(self):
        """Test SanitizingFormatter with record without args."""
        base_formatter = logging.Formatter("%(message)s")
        formatter = SanitizingFormatter(base_formatter)

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Test message",
            args=None,
            exc_info=None,
        )

        result = formatter.format(record)
        assert result == "Test message"

    def test_sanitizing_formatter_preserves_formatting(self):
        """Test SanitizingFormatter preserves base formatter behavior."""
        base_formatter = logging.Formatter("[%(levelname)s] %(message)s")
        formatter = SanitizingFormatter(base_formatter)

        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname="test.py",
            lineno=10,
            msg="Warning message",
            args=(),
            exc_info=None,
        )

        result = formatter.format(record)
        assert "[WARNING]" in result
        assert "Warning message" in result


class TestLogSafe:
    """Tests for log_safe (log injection guard)."""

    def test_escapes_line_breaks(self):
        forged = "alice\r\nWARNING Admin login for root"
        assert log_safe(forged) == "alice\\r\\nWARNING Admin login for root"
        assert "\n" not in log_safe(forged) and "\r" not in log_safe(forged)

    def test_leaves_plain_values_and_stringifies(self):
        assert log_safe("runs:user:alice") == "runs:user:alice"
        assert log_safe(42) == "42"
