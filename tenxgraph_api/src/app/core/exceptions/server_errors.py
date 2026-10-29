"""Answers for failures nobody planned for, so clients can tell a retry from a bug.

``UnhandledErrorMiddleware`` sits innermost in the middleware stack. An exception that no
handler took becomes the same JSON envelope as every other error (with the request id, and
the security headers and CORS that the outer layers add), instead of Starlette's bare
``Internal Server Error`` text that skips them. The exception is also logged with its
traceback and sent to Sentry when it is set up.

``classify`` tells three kinds of failure from a plain bug: a database or Redis driver that
cannot be reached (503 with ``Retry-After``), a model provider's rate limit (429 with
``Retry-After``) and a graph that hit its recursion limit (422).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.utils import error_response


logger = logging.getLogger("tenxgraph_api.errors")

RETRY_AFTER_SECONDS = 5

_DRIVER_MODULES = ("redis", "asyncpg", "psycopg", "psycopg2", "psycopg_pool")
_MAX_CHAIN = 8


@dataclass(frozen=True)
class Classified:
    status_code: int
    error_code: str
    message: str
    retry_after: int | None = None


def _chain(exc: BaseException):
    """``exc`` and the exceptions it was raised from, without looping."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < _MAX_CHAIN:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _retry_after_of(exc: BaseException) -> int:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    try:
        value = headers.get("retry-after") if headers is not None else None
        return max(1, int(float(value))) if value is not None else RETRY_AFTER_SECONDS
    except (TypeError, ValueError):
        return RETRY_AFTER_SECONDS


def classify(exc: BaseException) -> Classified | None:
    """A retryable or client-fixable reading of ``exc``, or ``None`` for a plain server bug."""
    for item in _chain(exc):
        cls = type(item)
        module = (cls.__module__ or "").split(".")[0]
        name = cls.__name__
        if name == "GraphRecursionError":
            return Classified(
                422,
                "GRAPH_RECURSION_LIMIT",
                "The graph reached its recursion limit before finishing. "
                "Raise recursion_limit or check the graph for a loop.",
            )
        if "RateLimit" in name or name in {"ResourceExhausted", "TooManyRequests"}:
            return Classified(
                429,
                "PROVIDER_RATE_LIMITED",
                "The model provider is rate limiting this server. Retry shortly.",
                _retry_after_of(item),
            )
        if module in _DRIVER_MODULES and name.endswith(
            ("Error", "Exception", "Timeout", "Interrupted")
        ):
            return Classified(
                503,
                "SERVICE_UNAVAILABLE",
                "A backing service (database or cache) is unavailable. Retry shortly.",
                RETRY_AFTER_SECONDS,
            )
    return None


def unexpected_error_response(request: Request, exc: BaseException):
    """The JSON answer for ``exc``: classified when it is retryable, else a generic 500."""
    kind = classify(exc)
    if kind is None:
        kind = Classified(500, "INTERNAL_SERVER_ERROR", "An unexpected error occurred.")
        message = kind.message
    else:
        message = kind.message
    if get_settings().MODE != "production" and kind.status_code == 500:  # noqa: PLR2004
        message = f"{type(exc).__name__}: {log_safe(exc)}"
    response = error_response(
        request, error_code=kind.error_code, message=message, status_code=kind.status_code
    )
    if kind.retry_after is not None:
        response.headers["Retry-After"] = str(kind.retry_after)
    response.headers["Cache-Control"] = "no-store"
    return response


def report(request: Request, exc: BaseException) -> None:
    """Log with the traceback, and tell Sentry when it is configured."""
    logger.error(
        "Unhandled %s on %s %s",
        type(exc).__name__,
        request.method,
        log_safe(request.url.path),
        exc_info=exc,
    )
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: S110 - Sentry is optional and must never break the error path
        pass


class UnhandledErrorMiddleware:
    """Turn an exception nobody handled into the standard JSON error response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception as exc:
            if started:  # a streaming body already left; nothing better can be sent
                raise
            request = Request(scope)
            report(request, exc)
            response: Any = unexpected_error_response(request, exc)
            await response(scope, receive, send)
