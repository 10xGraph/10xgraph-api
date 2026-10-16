"""
Log sanitization utilities for 10xGraph CLI.

This module provides utilities to sanitize sensitive data before logging,
preventing tokens, passwords, and other credentials from appearing in logs.
"""

import logging
import re
from typing import Any


# Patterns for detecting sensitive field names
SENSITIVE_PATTERNS = {
    "token",
    "password",
    "secret",
    "key",
    "credential",
    "authorization",
    "api_key",
    "access_token",
    "refresh_token",
    "auth",
    "bearer",
    "jwt",
    "session",
    "cookie",
    "private_key",
    "passphrase",
    "pin",
    "ssn",
    "credit_card",
}

# Regex pattern to detect JWT tokens (three base64url parts separated by dots)
# Each part excludes ".", so there is one way to split a string: no polynomial backtracking.
JWT_PATTERN = re.compile(r"^[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+(?:\.[A-Za-z0-9_.+/=-]*)?$")

# Regex pattern to detect bearer tokens
BEARER_PATTERN = re.compile(r"^Bearer\s+[A-Za-z0-9\-_.~+/]+=*$", re.IGNORECASE)

# Tokens embedded anywhere in a message (f-string logs carry no args to sanitize).
_INLINE_SECRET_PATTERNS = (
    (re.compile(r"Bearer\s+[A-Za-z0-9\-_.~+/]+=*", re.IGNORECASE), "Bearer ***REDACTED***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "***JWT_TOKEN***"),
)


def log_safe(value: Any) -> str:
    """``value`` as a single log line, for client-influenced values such as ids and paths.

    CR and LF are escaped so a value cannot end the entry and forge a new one.
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def redact_text(text: str) -> str:
    """Redact bearer tokens and JWTs that appear anywhere inside ``text``."""
    for pattern, replacement in _INLINE_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_for_logging(data: Any, max_depth: int = 10, _current_depth: int = 0) -> Any:
    """
    Recursively sanitize sensitive data for safe logging.

    This function walks through data structures and replaces sensitive values
    with redaction markers. It detects:
    - Dictionary keys containing sensitive patterns
    - JWT tokens (by structure)
    - Bearer tokens
    - Authorization headers

    Args:
        data: The data to sanitize (dict, list, str, or any other type)
        max_depth: Maximum recursion depth to prevent infinite loops
        _current_depth: Internal parameter tracking current recursion depth

    Returns:
        Sanitized copy of the data with sensitive values redacted

    Examples:
        >>> sanitize_for_logging({"user_id": "123", "token": "abc123"})
        {'user_id': '123', 'token': '***REDACTED***'}

        >>> sanitize_for_logging({"Authorization": "Bearer eyJhbGc..."})
        {'Authorization': '***REDACTED***'}
    """
    # Prevent excessive recursion
    if _current_depth >= max_depth:
        return "***MAX_DEPTH_REACHED***"

    if isinstance(data, dict):
        return {k: _sanitize_value(k, v, max_depth, _current_depth + 1) for k, v in data.items()}
    if isinstance(data, list):
        return [sanitize_for_logging(item, max_depth, _current_depth + 1) for item in data]
    if isinstance(data, tuple):
        return tuple(sanitize_for_logging(item, max_depth, _current_depth + 1) for item in data)
    if isinstance(data, str):
        return _sanitize_string(data)
    return data


def _sanitize_value(key: str, value: Any, max_depth: int, current_depth: int) -> Any:
    """
    Sanitize a value based on its key name and content.

    Args:
        key: The dictionary key
        value: The value to sanitize
        max_depth: Maximum recursion depth
        current_depth: Current recursion depth

    Returns:
        Sanitized value
    """
    # Check if key name contains sensitive patterns
    key_lower = key.lower()
    if any(pattern in key_lower for pattern in SENSITIVE_PATTERNS):
        return "***REDACTED***"

    # Recursively sanitize the value
    return sanitize_for_logging(value, max_depth, current_depth)


def _sanitize_string(value: str) -> str:
    """
    Sanitize a string value if it appears to be sensitive.

    Args:
        value: String to check and potentially sanitize

    Returns:
        Original string or redaction marker
    """
    max_value = 32
    # Check for JWT token pattern
    if len(value) > max_value and JWT_PATTERN.match(value):
        return "***JWT_TOKEN***"

    # Check for Bearer token pattern
    if BEARER_PATTERN.match(value):
        return "***BEARER_TOKEN***"

    # Check if string looks like a long random token (>32 chars, alphanumeric)
    if len(value) > max_value and value.replace("-", "").replace("_", "").isalnum():
        # Could be an API key or token
        return f"{value[:4]}...{value[-4:]}"

    return value


def sanitize_log_message(message: str, *args: Any, **kwargs: Any) -> tuple[str, tuple, dict]:
    """
    Sanitize log message arguments.

    This is useful for sanitizing arguments passed to logger.debug(), logger.info(), etc.

    Args:
        message: Log message format string
        *args: Positional arguments for the log message
        **kwargs: Keyword arguments for the log message

    Returns:
        Tuple of (message, sanitized_args, sanitized_kwargs)

    Examples:
        >>> msg, args, kwargs = sanitize_log_message(
        ...     "User %s logged in", {"user_id": "123", "token": "secret"}
        ... )
        >>> # args will have sanitized data
    """
    sanitized_args = tuple(sanitize_for_logging(arg) for arg in args)
    sanitized_kwargs = {k: sanitize_for_logging(v) for k, v in kwargs.items()}
    return message, sanitized_args, sanitized_kwargs


class SanitizingFormatter(logging.Formatter):
    """
    A wrapper for log formatters that sanitizes sensitive data.

    This can be used to wrap existing formatters to add sanitization. It is a
    proper ``logging.Formatter`` subclass so it can be passed to
    ``Handler.setFormatter`` without type errors, while delegating the actual
    formatting to the wrapped base formatter.

    Example:
        import logging

        formatter = logging.Formatter('%(asctime)s - %(message)s')
        sanitizing_formatter = SanitizingFormatter(formatter)
        handler.setFormatter(sanitizing_formatter)
    """

    def __init__(self, base_formatter: logging.Formatter) -> None:
        """
        Initialize the sanitizing formatter.

        Args:
            base_formatter: The underlying formatter to wrap
        """
        super().__init__()
        self.base_formatter = base_formatter

    def format(self, record: logging.LogRecord) -> str:
        """
        Format the log record with sanitization.

        Args:
            record: LogRecord to format

        Returns:
            Formatted and sanitized log string
        """
        # Sanitize the message arguments
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(sanitize_for_logging(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = sanitize_for_logging(record.args)

        # Format using the base formatter, then catch tokens already baked into the text
        # (f-string messages, exception text, tracebacks).
        return redact_text(self.base_formatter.format(record))
