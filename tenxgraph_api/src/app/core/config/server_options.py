"""Server options read from the environment, shared by every way of starting the server.

Kept free of heavy imports so the CLI, ``serve.py`` and the Gunicorn worker can all use it.
"""

from __future__ import annotations

import os


# Mirrors Settings.MAX_REQUEST_SIZE (the HTTP body cap); also caps one WebSocket message.
DEFAULT_MAX_REQUEST_SIZE = 10 * 1024 * 1024

LOOP_VALUES = ("auto", "asyncio", "uvloop")
HTTP_VALUES = ("auto", "h11", "httptools")


def max_request_size() -> int:
    raw = os.environ.get("MAX_REQUEST_SIZE")
    try:
        value = int(raw) if raw else DEFAULT_MAX_REQUEST_SIZE
    except ValueError:
        return DEFAULT_MAX_REQUEST_SIZE
    return value if value > 0 else DEFAULT_MAX_REQUEST_SIZE


def _choice(name: str, allowed: tuple[str, ...]) -> str:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return allowed[0]
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)} (got {value!r})")
    return value


def event_loop_choice() -> str:
    """``TENXGRAPH_LOOP``: auto (default), asyncio or uvloop. Invalid values raise ValueError."""
    return _choice("TENXGRAPH_LOOP", LOOP_VALUES)


def http_parser_choice() -> str:
    """``TENXGRAPH_HTTP``: auto (default), h11 or httptools. Invalid values raise ValueError."""
    return _choice("TENXGRAPH_HTTP", HTTP_VALUES)
