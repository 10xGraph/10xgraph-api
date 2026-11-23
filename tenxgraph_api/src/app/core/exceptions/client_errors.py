"""What a client sees when a request or a run fails, the same on every transport.

``client_error(exc)`` gives the status, error code and message of the HTTP answer for ``exc``:
the values the handlers in ``handle_errors.py`` (and the graph routes) give. Invoke raises
``as_http_error(exc)``; stream, WebSocket and AG-UI put the same code and message in their
error chunk or event. A message written for the client keeps its text in production; any
other error gets the generic production text, never the exception text.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
from tenxgraph.core import exceptions as core_errors
from tenxgraph.core.exceptions.media_exceptions import UnsupportedMediaInputError
from tenxgraph.utils.validators import ValidationError as InputValidationError

from tenxgraph_api.src.app.core.config.settings import is_production
from tenxgraph_api.src.app.core.exceptions.server_errors import (
    BAD_DATA_CODE,
    RETRY_AFTER_SECONDS,
    Classified,
    classify,
)
from tenxgraph_api.src.app.core.validation.request_config import (
    InterruptConflictError,
    client_error_detail,
)

from .resources_exceptions import ResourceNotFoundError as APIResourceNotFoundError
from .user_exception import UserAccountError, UserPermissionError


# The answer for a thread the caller cannot see: one body whether it is missing or another
# user's, so the response never reveals that an id exists. Streams use the same code and text.
THREAD_NOT_FOUND_CODE = "THREAD_NOT_FOUND"
THREAD_NOT_FOUND_MESSAGE = "Thread not found"

# The code of every error the graph routes answer through ``HTTPException``.
HTTP_ERROR_CODE = "HTTPException"


class RunTimeoutError(TimeoutError):
    """A graph run passed ``runs.timeout`` and was cancelled."""

    def __init__(self, timeout: float) -> None:
        super().__init__(
            f"Graph run passed the runs.timeout limit of {timeout:g}s and was cancelled. "
            'Raise "runs.timeout" in 10xgraph.json to allow longer runs.'
        )
        self.timeout = timeout


def _sanitize_error_message(message: str, error_code: str, is_production: bool) -> str:
    """The client message for ``error_code``: ``message`` in development, generic in production."""
    if not is_production:
        return message

    generic_messages = {
        "VALIDATION_ERROR": "The request data is invalid. Please check your input.",
        "HTTPException": "An error occurred processing your request.",
        # Before "GRAPH_": the first matching prefix wins.
        "GRAPH_NODE_MISSING_": (
            "This thread was saved at a step that no longer exists in the graph. "
            "Start a new thread, or ask the operator to map the old step."
        ),
        "GRAPH_": "An error occurred executing the graph.",
        "STATE_": (
            "This thread was saved by a newer server version. "
            "The server must be upgraded to read it."
        ),
        "NODE_": "An error occurred in a graph node.",
        "STORAGE_": "An error occurred accessing storage.",
        "METRICS_": "An error occurred collecting metrics.",
        "SCHEMA_VERSION_": "Schema version mismatch.",
        "SERIALIZATION_": "An error occurred processing data.",
    }
    for prefix, generic_msg in generic_messages.items():
        if error_code.startswith(prefix):
            return generic_msg
    return "An unexpected error occurred. Please contact support."


_ERROR_CODE = re.compile(r"^\[([A-Z][A-Z_]*_\d+)\]")
# Core's own text for an unexpected tool failure: a tool name and a log reference, no detail.
_SAFE_TOOL_FAILURE = re.compile(r"failed with an internal error \(reference [0-9a-f]{8}\)\.$")


def generic_error_text(text: Any) -> Any:
    """The production form of an error text: a generic message keeping only its error code.

    Core's own safe tool-failure text (a tool name and a log reference) and non-strings pass
    through unchanged.
    """
    if not isinstance(text, str) or _SAFE_TOOL_FAILURE.search(text):
        return text
    code = _ERROR_CODE.match(text)
    return _sanitize_error_message(text, code.group(1) if code else "GRAPH_STREAM_ERROR", True)


def _resume_conflict_types() -> tuple[type[Exception], ...]:
    """A resume aimed at the wrong (or no) interrupt: 409."""
    return (
        InterruptConflictError,
        core_errors.InterruptMismatchError,
        core_errors.NothingToResumeError,
        core_errors.InterruptPendingError,
    )


def resume_error_types() -> tuple[type[Exception], ...]:
    """Every error that means "this resume does not fit the open interrupt"."""
    return (*_resume_conflict_types(), core_errors.ResumeValidationError)


# Core errors with a registered handler, most specific first:
# (class, status, default error code, Retry-After seconds).
_TYPED_ERRORS: tuple[tuple[type[Exception], int, str, int | None], ...] = (
    (core_errors.StoredNodeMissingError, 409, "GRAPH_NODE_MISSING_001", None),
    (core_errors.StateFormatVersionError, 500, "STATE_FORMAT_001", None),
    (core_errors.GraphRecursionError, 422, "GRAPH_RECURSION_000", None),
    (core_errors.NodeError, 500, "NODE_000", None),
    (core_errors.GraphError, 500, "GRAPH_000", None),
    (core_errors.TransientStorageError, 503, "TRANSIENT_STORAGE_000", RETRY_AFTER_SECONDS),
    (core_errors.SchemaVersionError, 422, "SCHEMA_VERSION_000", None),
    (core_errors.SerializationError, 500, "SERIALIZATION_000", None),
    (core_errors.StorageError, 500, "STORAGE_000", None),
    (core_errors.MetricsError, 500, "METRICS_000", None),
)

# Every exception class ``handle_errors.py`` registers a handler for (besides ValueError,
# which the graph routes answer themselves).
HANDLED_TYPES: tuple[type[Exception], ...] = (
    StarletteHTTPException,
    InputValidationError,
    UserAccountError,
    UserPermissionError,
    APIResourceNotFoundError,
    core_errors.ThreadNotFoundError,
    core_errors.GraphClosedError,
    *(entry[0] for entry in _TYPED_ERRORS),
)


def _http_text(detail: str, status: int) -> str:
    """The HTTPException handler's rule: a 5xx detail (except 501) is generic in production."""
    server_error = status >= 500 and status != 501  # noqa: PLR2004
    return _sanitize_error_message(detail, HTTP_ERROR_CODE, is_production() and server_error)


def _route_error(exc: Exception, what: str) -> Classified | None:  # noqa: PLR0911
    """The answer for errors the graph routes turn into an ``HTTPException`` themselves.

    ``None`` when a registered handler (or the unhandled-error middleware) answers ``exc``.
    """
    if isinstance(exc, resume_error_types()):
        status = 409 if isinstance(exc, _resume_conflict_types()) else 422
        return Classified(status, HTTP_ERROR_CODE, str(getattr(exc, "message", None) or exc))
    if isinstance(exc, UnsupportedMediaInputError):
        return Classified(422, HTTP_ERROR_CODE, exc.message)
    if isinstance(exc, RunTimeoutError):
        return Classified(504, HTTP_ERROR_CODE, _http_text(str(exc), 504))
    if isinstance(exc, HANDLED_TYPES):
        return None
    if isinstance(exc, ValueError):
        return Classified(422, HTTP_ERROR_CODE, client_error_detail(exc))
    if classify(exc) is not None:
        return None
    return Classified(500, HTTP_ERROR_CODE, _http_text(f"{what}: {exc}", 500))


def _handled_error(exc: Exception) -> Classified:  # noqa: PLR0911
    """The answer the registered handler (or the unhandled-error middleware) gives for ``exc``."""
    production = is_production()
    if isinstance(exc, StarletteHTTPException):
        retry = (exc.headers or {}).get("Retry-After")
        retry_after = int(retry) if isinstance(retry, str) and retry.isdigit() else None
        return Classified(
            exc.status_code,
            HTTP_ERROR_CODE,
            _http_text(str(exc.detail), exc.status_code),
            retry_after,
        )
    if isinstance(exc, InputValidationError):
        message = _sanitize_error_message(str(exc), "VALIDATION_ERROR", production)
        return Classified(422, "VALIDATION_ERROR", message)
    if isinstance(exc, UserAccountError | UserPermissionError | APIResourceNotFoundError):
        return Classified(exc.status_code, exc.error_code, exc.message)
    if isinstance(exc, core_errors.ThreadNotFoundError):
        return Classified(404, THREAD_NOT_FOUND_CODE, THREAD_NOT_FOUND_MESSAGE)
    if isinstance(exc, core_errors.GraphClosedError):
        return Classified(
            503,
            "SERVICE_UNAVAILABLE",
            "The server is shutting down. Retry the request.",
            RETRY_AFTER_SECONDS,
        )
    for cls, status, default_code, retry_after in _TYPED_ERRORS:
        if not isinstance(exc, cls):
            continue
        if cls is core_errors.StorageError:
            kind = classify(exc)
            # An outage (retryable) or data the database refused (422): not a server bug.
            outage = kind is not None and kind.status_code == 503  # noqa: PLR2004
            if kind is not None and (outage or kind.error_code == BAD_DATA_CODE):
                return kind
        code = getattr(exc, "error_code", default_code)
        message = _sanitize_error_message(getattr(exc, "message", str(exc)), code, production)
        return Classified(status, code, message, retry_after)
    kind = classify(exc)
    if kind is not None:
        return kind
    return Classified(500, "INTERNAL_SERVER_ERROR", "An unexpected error occurred.")


def client_error(exc: Exception, what: str = "Request failed") -> Classified:
    """Status, error code and client message for ``exc``, as the HTTP routes answer it.

    ``what`` starts the development message of an unexpected error ("<what>: <exception>").
    """
    return _route_error(exc, what) or _handled_error(exc)


def as_http_error(exc: Exception, what: str = "Request failed") -> Exception:
    """What a route raises for ``exc``: ``exc`` itself when a handler answers it, else an
    ``HTTPException`` with the status and message of :func:`client_error`."""
    routed = _route_error(exc, what)
    if routed is None:
        return exc
    headers = {"Retry-After": str(routed.retry_after)} if routed.retry_after else None
    return HTTPException(status_code=routed.status_code, detail=routed.message, headers=headers)
