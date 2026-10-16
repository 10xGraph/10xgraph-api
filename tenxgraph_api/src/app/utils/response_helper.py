"""This module contains helper functions for creating responses
with metadata, error codes, and messages. The functions in this
module are used to create responses with metadata, error codes,
and messages. The success_response function is used to create
a success response with the provided data, message, status code,
and metadata. The error_response function is used to create an
error response with the provided error code, message, details,
status code, and metadata. The merge_metadata function is used
to merge metadata with request details and a message.

Examples:
    - success_response(res, request, message, status_code, metadata)
    - error_response(request, error_code, message, details, status_code, metadata)
    - merge_metadata(metadata, request, message)

"""

from typing import Any, TypeVar

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel

from .schemas import (
    ErrorOutputSchema,
    ErrorResponse,
    ErrorSchemas,
    SuccessResponse,
)


T = TypeVar("T")


def _json_response(model: BaseModel, status_code: int) -> Response:
    """Serialize a response model in pydantic-core (Rust) and wrap it in a Response.

    Starlette responses cannot take a model directly, and ``JSONResponse(model.model_dump())``
    would re-encode through the stdlib ``json`` module.
    """
    if type(model) is dict:
        return Response(
            content=model,
            status_code=status_code,
            media_type="application/json",
        )

    return Response(
        content=model.model_dump_json() if model else {},
        status_code=status_code,
        media_type="application/json",
    )


def merge_metadata(metadata: dict | None, request: Request, message: str = "") -> dict[Any, Any]:
    """Merges metadata with request details and a message.
    The metadata is updated with the request_id, timestamp,
    and message. This function is used to create a response
    with metadata, request details, and a message.

    Args:
        metadata: A dictionary containing metadata information. Defaults to None.
        request: An instance of the Request class from fastapi. Defaults to None.
        message: A string message to be included in the metadata. Defaults to "".

    Returns:
        metadata: A dictionary containing merged metadata with request details
            and the message.
    """
    if metadata:
        metadata.update(
            {
                "request_id": request.state.request_id,
                "timestamp": request.state.timestamp,
                "message": message,
            }
        )
        return metadata

    return {
        "request_id": request.state.request_id,
        "timestamp": request.state.timestamp,
        "message": message,
    }


def success_response(
    res: Any,
    request: Request,
    message: str = "OK",
    status_code: int = 200,
    metadata: dict | None = None,
):
    """Creates a success response with the provided data, message,
    status code, and metadata.

    Args:
        res (T): The data to be included in the response.
        request (Request): The FastAPI request object.
        message (str, optional): The message associated with the response.
            Defaults to OK.
        status_code (int, optional): The HTTP status code of the response.
            Defaults to 200.
        metadata (dict, optional): Additional metadata to be merged with the response.
            Defaults to None.

    Returns:
        Response: The JSON response containing the data,
            message, and metadata.
    """
    response: SuccessResponse = SuccessResponse(
        data=res, metadata=merge_metadata(metadata, request, message)
    )
    return _json_response(response, status_code)


def error_response(
    request: Request,
    error_code: str,
    message: str = "",
    details: list[ErrorSchemas] | None = None,
    status_code: int = 400,
    metadata: dict | None = None,
):
    """Creates an error response with the provided error code, message,
    details, status code, and metadata.

    Args:
        request (Request): The FastAPI request object. Default to None.
        error_code (str): The code associated with the error.
            Default to None.
        message (str, optional): The message describing the error. Default to "".
        details (list[ErrorSchemas], optional): Additional details about the error.
            Default to None.
        status_code (int, optional): The HTTP status code of the response.
            Default is 400.
        metadata (dict, optional): Additional metadata to be merged with the response.
            Default is None.

    Returns:
        Response: The JSON response containing the error details, message,
            and metadata.
    """
    error = ErrorResponse(
        error=ErrorOutputSchema(
            code=error_code, message=message, details=details if details else []
        ),
        metadata=merge_metadata(metadata, request, message),
    )
    return _json_response(error, status_code)
