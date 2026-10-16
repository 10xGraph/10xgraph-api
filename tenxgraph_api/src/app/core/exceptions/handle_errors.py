# Handle all exceptions of the 10xGraph API here
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from tenxgraph.core.exceptions import (
    GraphError,
    GraphRecursionError,
    MetricsError,
    NodeError,
    SchemaVersionError,
    SerializationError,
    StorageError,
    TransientStorageError,
)
from tenxgraph.utils.validators import ValidationError

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.utils import error_response
from tenxgraph_api.src.app.utils.schemas import ErrorSchemas

from .resources_exceptions import ResourceNotFoundError as APIResourceNotFoundError
from .user_exception import (
    UserAccountError,
    UserPermissionError,
)


def _sanitize_error_message(message: str, error_code: str, is_production: bool) -> str:
    """
    Sanitize error messages for production to avoid exposing internal details.

    Args:
        message: Original error message
        error_code: Error code for the exception
        is_production: Whether the app is in production mode

    Returns:
        Sanitized message (generic in production, detailed in development)
    """
    if not is_production:
        return message

    # Generic messages by status code category
    generic_messages = {
        "VALIDATION_ERROR": "The request data is invalid. Please check your input.",
        "HTTPException": "An error occurred processing your request.",
        "GRAPH_": "An error occurred executing the graph.",
        "NODE_": "An error occurred in a graph node.",
        "STORAGE_": "An error occurred accessing storage.",
        "METRICS_": "An error occurred collecting metrics.",
        "SCHEMA_VERSION_": "Schema version mismatch.",
        "SERIALIZATION_": "An error occurred processing data.",
    }

    # Return generic message based on error code prefix
    for prefix, generic_msg in generic_messages.items():
        if error_code.startswith(prefix):
            return generic_msg

    return "An unexpected error occurred. Please contact support."


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
    settings = get_settings()
    is_production = settings.MODE == "production"

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        logger.error(f"HTTP exception: url: {request.base_url}", exc_info=exc)

        # Get request ID for tracking
        request_id = getattr(request.state, "request_id", "unknown")

        # A 4xx detail is written by the API for the client (401 bad token, 403 missing scope,
        # 404, 413, 422 bad input, 429): hiding it leaves the client unable to fix the request.
        # A 5xx detail may embed internals, so production replaces it.
        is_server_error = exc.status_code >= 500  # noqa: PLR2004
        message = _sanitize_error_message(
            str(exc.detail), "HTTPException", is_production and is_server_error
        )

        # Log full details but return sanitized message in production
        if is_production and is_server_error:
            logger.error(f"Request {request_id} - HTTPException details: {exc.detail}")

        return error_response(
            request,
            error_code="HTTPException",
            message=message,
            status_code=exc.status_code,
        )

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
            message = "The request data is invalid. Please check your input."
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
        logger.error(f"Value error exception: url: {request.base_url}", exc_info=exc)

        request_id = getattr(request.state, "request_id", "unknown")

        message = str(exc)
        if is_production:
            logger.error(f"Request {request_id} - ValueError details: {message}")
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
        logger.warning("UserAccountError on %s: %s", request.url.path, exc.error_code)
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
        logger.error(f"UserPermissionError: url: {request.base_url}", exc_info=exc)
        return error_response(
            request,
            error_code=exc.error_code,
            message=exc.message,
            status_code=exc.status_code,
        )

    @app.exception_handler(APIResourceNotFoundError)
    async def resource_not_found_exception_handler(request: Request, exc: APIResourceNotFoundError):
        logger.error(f"ResourceNotFoundError: url: {request.base_url}", exc_info=exc)
        return error_response(
            request,
            error_code=exc.error_code,
            message=exc.message,
            status_code=exc.status_code,
        )

    ## Need to handle 10xGraph specific exceptions here
    @app.exception_handler(ValidationError)
    async def graph_validation_exception_handler(request: Request, exc: ValidationError):
        logger.error(f"10xGraph ValidationError: url: {request.base_url}", exc_info=exc)

        request_id = getattr(request.state, "request_id", "unknown")
        message = _sanitize_error_message(str(exc), "AGENTFLOW_VALIDATION_ERROR", is_production)

        if is_production:
            logger.error(f"Request {request_id} - 10xGraph ValidationError: {exc}")

        return error_response(
            request,
            error_code="AGENTFLOW_VALIDATION_ERROR",
            message=message,
            status_code=422,
        )

    @app.exception_handler(GraphError)
    async def graph_error_exception_handler(request: Request, exc: GraphError):
        logger.error(f"GraphError: url: {request.base_url}", exc_info=exc)

        request_id = getattr(request.state, "request_id", "unknown")
        error_code = getattr(exc, "error_code", "GRAPH_000")
        original_message = getattr(exc, "message", str(exc))
        message = _sanitize_error_message(original_message, error_code, is_production)

        if is_production:
            logger.error(f"Request {request_id} - GraphError: {original_message}")

        return error_response(
            request,
            error_code=error_code,
            message=message,
            details=getattr(exc, "context", None) if not is_production else None,
            status_code=500,
        )

    @app.exception_handler(NodeError)
    async def node_error_exception_handler(request: Request, exc: NodeError):
        logger.error(f"NodeError: url: {request.base_url}", exc_info=exc)

        request_id = getattr(request.state, "request_id", "unknown")
        error_code = getattr(exc, "error_code", "NODE_000")
        original_message = getattr(exc, "message", str(exc))
        message = _sanitize_error_message(original_message, error_code, is_production)

        if is_production:
            logger.error(f"Request {request_id} - NodeError: {original_message}")

        return error_response(
            request,
            error_code=error_code,
            message=message,
            details=getattr(exc, "context", None) if not is_production else None,
            status_code=500,
        )

    @app.exception_handler(GraphRecursionError)
    async def graph_recursion_error_exception_handler(request: Request, exc: GraphRecursionError):
        logger.error(f"GraphRecursionError: url: {request.base_url}", exc_info=exc)

        request_id = getattr(request.state, "request_id", "unknown")
        error_code = getattr(exc, "error_code", "GRAPH_RECURSION_000")
        original_message = getattr(exc, "message", str(exc))
        message = _sanitize_error_message(original_message, error_code, is_production)

        if is_production:
            logger.error(f"Request {request_id} - GraphRecursionError: {original_message}")

        return error_response(
            request,
            error_code=error_code,
            message=message,
            details=getattr(exc, "context", None) if not is_production else None,
            status_code=500,
        )

    @app.exception_handler(StorageError)
    async def storage_error_exception_handler(request: Request, exc: StorageError):
        logger.error(f"StorageError: url: {request.base_url}", exc_info=exc)

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
            details=getattr(exc, "context", None) if not is_production else None,
            status_code=500,
        )

    @app.exception_handler(TransientStorageError)
    async def transient_storage_error_exception_handler(
        request: Request, exc: TransientStorageError
    ):
        logger.error(f"TransientStorageError: url: {request.base_url}", exc_info=exc)

        request_id = getattr(request.state, "request_id", "unknown")
        error_code = getattr(exc, "error_code", "TRANSIENT_STORAGE_000")
        original_message = getattr(exc, "message", str(exc))
        message = _sanitize_error_message(original_message, error_code, is_production)

        if is_production:
            logger.error(f"Request {request_id} - TransientStorageError: {original_message}")

        return error_response(
            request,
            error_code=error_code,
            message=message,
            details=getattr(exc, "context", None) if not is_production else None,
            status_code=503,
        )

    @app.exception_handler(MetricsError)
    async def metrics_error_exception_handler(request: Request, exc: MetricsError):
        logger.error(f"MetricsError: url: {request.base_url}", exc_info=exc)

        request_id = getattr(request.state, "request_id", "unknown")
        error_code = getattr(exc, "error_code", "METRICS_000")
        original_message = getattr(exc, "message", str(exc))
        message = _sanitize_error_message(original_message, error_code, is_production)

        if is_production:
            logger.error(f"Request {request_id} - MetricsError: {original_message}")

        return error_response(
            request,
            error_code=error_code,
            message=message,
            details=getattr(exc, "context", None) if not is_production else None,
            status_code=500,
        )

    @app.exception_handler(SchemaVersionError)
    async def schema_version_error_exception_handler(request: Request, exc: SchemaVersionError):
        logger.error(f"SchemaVersionError: url: {request.base_url}", exc_info=exc)

        request_id = getattr(request.state, "request_id", "unknown")
        error_code = getattr(exc, "error_code", "SCHEMA_VERSION_000")
        original_message = getattr(exc, "message", str(exc))
        message = _sanitize_error_message(original_message, error_code, is_production)

        if is_production:
            logger.error(f"Request {request_id} - SchemaVersionError: {original_message}")

        return error_response(
            request,
            error_code=error_code,
            message=message,
            details=getattr(exc, "context", None) if not is_production else None,
            status_code=422,
        )

    @app.exception_handler(SerializationError)
    async def serialization_error_exception_handler(request: Request, exc: SerializationError):
        logger.error(f"SerializationError: url: {request.base_url}", exc_info=exc)

        request_id = getattr(request.state, "request_id", "unknown")
        error_code = getattr(exc, "error_code", "SERIALIZATION_000")
        original_message = getattr(exc, "message", str(exc))
        message = _sanitize_error_message(original_message, error_code, is_production)

        if is_production:
            logger.error(f"Request {request_id} - SerializationError: {original_message}")

        return error_response(
            request,
            error_code=error_code,
            message=message,
            details=getattr(exc, "context", None) if not is_production else None,
            status_code=500,
        )
