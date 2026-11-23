# Handle all exceptions of the 10xGraph API here
from http import HTTPStatus

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from tenxgraph.core.exceptions import (
    GraphClosedError,
    GraphError,
    GraphRecursionError,
    MetricsError,
    NodeError,
    SchemaVersionError,
    SerializationError,
    StateFormatVersionError,
    StorageError,
    StoredNodeMissingError,
    ThreadNotFoundError,
    TransientStorageError,
)
from tenxgraph.utils.validators import ValidationError

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.audit import audit_event, audit_http_not_found, audit_not_found
from tenxgraph_api.src.app.core.config.settings import is_production as production_mode
from tenxgraph_api.src.app.core.exceptions.client_errors import (
    THREAD_NOT_FOUND_CODE,
    THREAD_NOT_FOUND_MESSAGE,
    _sanitize_error_message,
)
from tenxgraph_api.src.app.core.exceptions.server_errors import (
    BAD_DATA_CODE,
    RETRY_AFTER_SECONDS,
    classify,
    unexpected_error_response,
)
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.core.validation.request_config import (
    CLIENT_MESSAGE_ERRORS,
    ClientInputError,
)
from tenxgraph_api.src.app.utils import error_response
from tenxgraph_api.src.app.utils.schemas import ErrorSchemas

from .resources_exceptions import ResourceNotFoundError as APIResourceNotFoundError
from .user_exception import (
    UserAccountError,
    UserPermissionError,
)


def _context_details(exc: Exception) -> list[ErrorSchemas]:
    """Turn an exception's ``context`` dict into the list shape ``details`` expects.

    Each key becomes one entry: ``loc`` is the key, ``msg`` the value as text.
    """
    context = getattr(exc, "context", None) or {}
    return [
        ErrorSchemas(loc=[str(key)], msg=str(value), type="context")
        for key, value in context.items()
    ]


def _log_handled(
    request: Request, exc: Exception, status_code: int, error_code: str, label: str
) -> None:
    """Log a handled exception at the level of the status the handler returns.

    A 4xx is the client's mistake: one WARNING line (method, path, status, code), no traceback
    and no body. A 5xx is a server fault: ERROR with the traceback. A 501 is neither: the
    route is not offered by this setup, so it is logged like a 4xx.
    """
    if status_code < 500 or status_code == 501:  # noqa: PLR2004
        logger.warning(
            "%s: %s %s -> %s %s",
            label,
            request.method,
            log_safe(request.url.path),
            status_code,
            error_code,
        )
    else:
        logger.error(f"{label}: url: {request.base_url}", exc_info=exc)


def init_errors_handler(app: FastAPI):  # noqa: PLR0915
    """
    Initialize error handlers for the FastAPI application.

    Args:
        app (FastAPI): The FastAPI application instance.

    Raises:
        HTTPException: Handles HTTP exceptions.
        RequestValidationError: Handles request validation errors.
        ValueError: Handles value errors.
        UserAccountError: Handles custom user account errors.
        UserPermissionError: Handles custom user permission errors.
        APIResourceNotFoundError: Handles custom API resource not found errors.
    """
    is_production = production_mode()

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        _log_handled(request, exc, exc.status_code, "HTTPException", "HTTP exception")
        if exc.status_code == 404:  # noqa: PLR2004
            await audit_http_not_found()

        # Get request ID for tracking
        request_id = getattr(request.state, "request_id", "unknown")

        # A 4xx detail is written by the API for the client (401 bad token, 403 missing permission,
        # 404, 413, 422 bad input, 429): hiding it leaves the client unable to fix the request.
        # A 5xx detail may embed internals, so production replaces it.
        is_server_error = exc.status_code >= 500 and exc.status_code != 501  # noqa: PLR2004
        message = _sanitize_error_message(
            str(exc.detail), "HTTPException", is_production and is_server_error
        )

        # Log full details but return sanitized message in production
        if is_production and is_server_error:
            logger.error(f"Request {request_id} - HTTPException details: {exc.detail}")

        response = error_response(
            request,
            error_code="HTTPException",
            message=message,
            status_code=exc.status_code,
        )
        # Headers the route set on the exception (Retry-After, WWW-Authenticate) must reach
        # the client.
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        request_id = getattr(request.state, "request_id", "unknown")
        details = [ErrorSchemas(**error) for error in exc.errors()]

        # Log where and why validation failed, never the submitted values: exc (and its
        # traceback) embeds the raw request body, which can hold messages, files or tokens.
        logger.warning(
            "Request %s - validation failed: %s %s: %s",
            request_id,
            request.method,
            request.url.path,
            [f"{'.'.join(map(str, d.loc))}: {d.msg}" for d in details],
        )

        if is_production:
            # A ClientInputError raised in a validator (for example a request size limit) is
            # written for the client, so its message survives production.
            client_messages = [
                str(error["ctx"]["error"])
                for error in exc.errors()
                if isinstance((error.get("ctx") or {}).get("error"), ClientInputError)
            ]
            message = (
                client_messages[0]
                if client_messages
                else "The request data is invalid. Please check your input."
            )
        else:
            message = str(exc.body) if exc.body else "Validation error"

        return error_response(
            request,
            error_code="VALIDATION_ERROR",
            message=message,
            details=details if not is_production else None,
            status_code=422,
        )

    @app.exception_handler(ValueError)
    async def value_exception_handler(request: Request, exc: ValueError):
        _log_handled(request, exc, 422, "VALIDATION_ERROR", "Value error exception")

        request_id = getattr(request.state, "request_id", "unknown")

        message = str(exc)
        # A ClientInputError or ModelNotFoundError is written for the client, so its message
        # survives production.
        if is_production and not isinstance(exc, CLIENT_MESSAGE_ERRORS):
            logger.warning("Request %s - ValueError details: %s", request_id, log_safe(message))
            message = "Invalid input provided."

        return error_response(
            request,
            error_code="VALIDATION_ERROR",
            message=message,
            status_code=422,
        )

    ########################################
    ##### Custom exception handler here ####
    ########################################
    @app.exception_handler(UserAccountError)
    async def user_account_exception_handler(request: Request, exc: UserAccountError):
        _log_handled(request, exc, exc.status_code, exc.error_code, "UserAccountError")
        response = error_response(
            request,
            error_code=exc.error_code,
            message=exc.message,
            status_code=exc.status_code,
        )
        if exc.status_code == 401:  # noqa: PLR2004
            response.headers["WWW-Authenticate"] = 'Bearer realm="auth_required"'
        return response

    @app.exception_handler(UserPermissionError)
    async def user_write_exception_handler(request: Request, exc: UserPermissionError):
        _log_handled(request, exc, exc.status_code, exc.error_code, "UserPermissionError")
        return error_response(
            request,
            error_code=exc.error_code,
            message=exc.message,
            status_code=exc.status_code,
        )

    @app.exception_handler(APIResourceNotFoundError)
    async def resource_not_found_exception_handler(request: Request, exc: APIResourceNotFoundError):
        _log_handled(request, exc, exc.status_code, exc.error_code, "ResourceNotFoundError")
        return error_response(
            request,
            error_code=exc.error_code,
            message=exc.message,
            status_code=exc.status_code,
        )

    ## Need to handle 10xGraph specific exceptions here
    @app.exception_handler(ValidationError)
    async def graph_validation_exception_handler(request: Request, exc: ValidationError):
        _log_handled(request, exc, 422, "VALIDATION_ERROR", "10xGraph ValidationError")
        await audit_event("input", "denied", reason="validation")

        request_id = getattr(request.state, "request_id", "unknown")
        message = _sanitize_error_message(str(exc), "VALIDATION_ERROR", is_production)

        if is_production:
            logger.warning("Request %s - 10xGraph ValidationError: %s", request_id, log_safe(exc))

        return error_response(
            request,
            error_code="VALIDATION_ERROR",
            message=message,
            status_code=422,
        )

    @app.exception_handler(GraphClosedError)
    async def graph_closed_exception_handler(request: Request, exc: GraphClosedError):
        # The server is shutting down: this replica takes no new runs, another one will.
        _log_handled(request, exc, 503, "SERVICE_UNAVAILABLE", "Graph closed")
        response = error_response(
            request,
            error_code="SERVICE_UNAVAILABLE",
            message="The server is shutting down. Retry the request.",
            status_code=503,
        )
        response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
        return response

    @app.exception_handler(StoredNodeMissingError)
    async def stored_node_missing_exception_handler(request: Request, exc: StoredNodeMissingError):
        # The thread's stored position no longer exists in this graph: the client can start a
        # new thread, so it is a 409 and not a 500.
        error_code = getattr(exc, "error_code", "GRAPH_NODE_MISSING_001")
        _log_handled(request, exc, 409, error_code, "StoredNodeMissingError")
        message = _sanitize_error_message(
            getattr(exc, "message", str(exc)), error_code, is_production
        )
        return error_response(
            request,
            error_code=error_code,
            message=message,
            details=_context_details(exc) if not is_production else None,
            status_code=409,
        )

    @app.exception_handler(StateFormatVersionError)
    async def state_format_version_exception_handler(
        request: Request, exc: StateFormatVersionError
    ):
        # Stored state is newer than this server reads (a rollback): an operator problem.
        error_code = getattr(exc, "error_code", "STATE_FORMAT_001")
        logger.error("StateFormatVersionError: url: %s", request.base_url, exc_info=exc)
        message = _sanitize_error_message(
            getattr(exc, "message", str(exc)), error_code, is_production
        )
        return error_response(
            request,
            error_code=error_code,
            message=message,
            details=_context_details(exc) if not is_production else None,
            status_code=500,
        )

    @app.exception_handler(ThreadNotFoundError)
    async def thread_not_found_exception_handler(request: Request, exc: ThreadNotFoundError):
        # A subclass of StorageError: Starlette picks the handler of the closest class, so this
        # one wins over the generic 500. No details: the context holds the thread id.
        _log_handled(request, exc, 404, THREAD_NOT_FOUND_CODE, "ThreadNotFoundError")
        await audit_not_found(exc)  # skipped when the thread lookup already recorded it
        return error_response(
            request,
            error_code=THREAD_NOT_FOUND_CODE,
            message=THREAD_NOT_FOUND_MESSAGE,
            status_code=404,
        )

    @app.exception_handler(StorageError)
    async def storage_error_exception_handler(request: Request, exc: StorageError):
        logger.error(f"StorageError: url: {request.base_url}", exc_info=exc)

        kind = classify(exc)
        if kind is not None and (
            kind.status_code == HTTPStatus.SERVICE_UNAVAILABLE or kind.error_code == BAD_DATA_CODE
        ):
            # The database or cache could not be reached (retryable), or it refused the
            # client's data (422): not a bug in the server.
            return unexpected_error_response(request, exc)

        request_id = getattr(request.state, "request_id", "unknown")
        error_code = getattr(exc, "error_code", "STORAGE_000")
        original_message = getattr(exc, "message", str(exc))
        message = _sanitize_error_message(original_message, error_code, is_production)

        if is_production:
            logger.error(f"Request {request_id} - StorageError: {original_message}")

        return error_response(
            request,
            error_code=error_code,
            message=message,
            details=_context_details(exc) if not is_production else None,
            status_code=500,
        )

    def add_handler(
        exc_class: type[Exception],
        default_code: str,
        status_code: int,
        *,
        log_code: str | None = None,
        retry_after: bool = False,
    ) -> None:
        """Register the handler shared by the core errors that only differ in class, default
        code and status: the message is generic in production, details come from the context."""
        name = exc_class.__name__

        @app.exception_handler(exc_class)
        async def handler(request: Request, exc: Exception):
            error_code = getattr(exc, "error_code", default_code)
            _log_handled(request, exc, status_code, log_code or error_code, name)

            request_id = getattr(request.state, "request_id", "unknown")
            original_message = getattr(exc, "message", str(exc))
            message = _sanitize_error_message(original_message, error_code, is_production)

            if is_production and status_code >= 500:  # noqa: PLR2004
                logger.error(f"Request {request_id} - {name}: {original_message}")
            elif is_production:
                logger.warning("Request %s - %s: %s", request_id, name, log_safe(original_message))

            response = error_response(
                request,
                error_code=error_code,
                message=message,
                details=_context_details(exc) if not is_production else None,
                status_code=status_code,
            )
            if retry_after:
                response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
            return response

    add_handler(GraphError, "GRAPH_000", 500)
    add_handler(NodeError, "NODE_000", 500)
    add_handler(GraphRecursionError, "GRAPH_RECURSION_000", 422, log_code="RECURSION")
    add_handler(TransientStorageError, "TRANSIENT_STORAGE_000", 503, retry_after=True)
    add_handler(MetricsError, "METRICS_000", 500)
    add_handler(SchemaVersionError, "SCHEMA_VERSION_000", 422)
    add_handler(SerializationError, "SERIALIZATION_000", 500)
