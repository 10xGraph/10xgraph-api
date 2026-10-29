"""Checkpointer router module."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from injectq.integrations import InjectAPI
from tenxgraph.core.state import Message

from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.auth.request_config import client_config
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.routers.graph.thread_lock import thread_write_guard
from tenxgraph_api.src.app.utils.parse_output import parse_message_output
from tenxgraph_api.src.app.utils.response_helper import success_response
from tenxgraph_api.src.app.utils.swagger_helper import generate_swagger_responses

from .schemas.checkpointer_schemas import (
    ConfigSchema,
    FeedbackSchema,
    MessagesListResponseSchema,
    PutMessagesSchema,
    ResponseSchema,
    StateResponseSchema,
    StateSchema,
    ThreadResponseSchema,
    ThreadsListResponseSchema,
)
from .services.checkpointer_service import CheckpointerService


router = APIRouter(tags=["checkpointer"])

# Bound pagination so an unbounded/huge ``limit`` (or ``limit=None``) can never ask the
# backend to load a whole table into memory. Applied server-side regardless of client input.
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000


def _clamp_limit(limit: int | None) -> int:
    """Return a bounded page size: default when unset, capped at MAX_PAGE_LIMIT."""
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    return min(limit, MAX_PAGE_LIMIT)


def validate_thread_id(thread_id: int | str) -> None:
    if isinstance(thread_id, str):
        if not thread_id.strip():
            raise HTTPException(status_code=422, detail="thread_id cannot be empty or whitespace")
    elif isinstance(thread_id, int):
        if thread_id < 1:
            raise HTTPException(status_code=422, detail="thread_id must be a non-negative integer")
    else:
        raise HTTPException(status_code=422, detail="thread_id must be a string or integer")


@router.get(
    "/v1/threads/{thread_id}/state",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(StateResponseSchema),
    summary="Get state from checkpointer",
    description="Retrieve state data from the checkpointer using configuration.",
)
async def get_state(
    request: Request,
    thread_id: int | str,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(RequirePermission("checkpointer", "read")),
):
    """Get state from checkpointer.

    Args:
        request: State schema with configuration
        checkpointer: Injected checkpointer instance

    Returns:
        State response with state data or error
    """
    validate_thread_id(thread_id)

    config = {"thread_id": thread_id}

    result = await service.get_state(
        config,
        user,
    )

    return success_response(
        result,
        request,
    )


@router.put(
    "/v1/threads/{thread_id}/state",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(StateResponseSchema),
    summary="Put state to checkpointer",
    description="Store state data in the checkpointer using configuration.",
)
async def put_state(
    request: Request,
    thread_id: str | int,
    payload: StateSchema,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(RequirePermission("checkpointer", "write")),
):
    """Put state to checkpointer.

    Args:
        request: Request object
        payload: Put state schema with configuration and state data
        service: Injected checkpointer service
        user: Current authenticated user

    Returns:
        Success response or error
    """
    validate_thread_id(thread_id)
    # The path thread_id is the one RequirePermission checked; it is applied last so the
    # body config cannot redirect the call to another thread.
    config = {**client_config(payload.config), "thread_id": thread_id}

    async with thread_write_guard(thread_id):
        res = await service.put_state(
            config,
            user,
            payload.state,
        )

    return success_response(
        res,
        request,
    )


@router.delete(
    "/v1/threads/{thread_id}/state",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(ResponseSchema),
    summary="Clear state from checkpointer",
    description="Clear state data from the checkpointer using configuration.",
)
async def clear_state(
    request: Request,
    thread_id: int | str,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(RequirePermission("checkpointer", "delete")),
):
    """Clear state from checkpointer.

    Args:
        request: Request object
        payload: Clear state schema with configuration
        service: Injected checkpointer service
        user: Current authenticated user

    Returns:
        Success response or error
    """
    validate_thread_id(thread_id)
    config = {"thread_id": thread_id}

    async with thread_write_guard(thread_id):
        res = await service.clear_state(
            config,
            user,
        )

    return success_response(
        res,
        request,
    )


# Now Handle Messages


@router.post(
    "/v1/threads/{thread_id}/messages",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(ResponseSchema),
    summary="Import messages into a thread (admin)",
    description=(
        "Append messages of any role to a thread, to import history. Admin only: the caller "
        "must hold the `checkpointer:import` scope explicitly (an identity with no scopes is "
        "refused). Tool calls are still refused."
    ),
)
async def put_messages(
    request: Request,
    thread_id: str | int,
    payload: PutMessagesSchema,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(
        RequirePermission("checkpointer", "import", explicit_scope=True)
    ),
):
    """Put messages to checkpointer.

    Args:
        request: Request object
        payload: Put messages schema with configuration and messages
        service: Injected checkpointer service
        user: Current authenticated user

    Returns:
        Success response or error
    """
    validate_thread_id(thread_id)
    if not payload.messages:
        raise HTTPException(status_code=422, detail="messages must not be empty")

    # The path thread_id is the one RequirePermission checked; it is applied last so the
    # body config cannot redirect the call to another thread.
    config = {**client_config(payload.config), "thread_id": thread_id}

    async with thread_write_guard(thread_id):
        res = await service.put_messages(
            config,
            user,
            payload.messages,
            payload.metadata,
        )

    return success_response(
        res,
        request,
    )


@router.get(
    "/v1/threads/{thread_id}/messages/{message_id}",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(Message),
    summary="Get message from checkpointer",
    description=(
        "Retrieve a specific message from the checkpointer using configuration and message ID."
    ),
)
async def get_message(
    request: Request,
    thread_id: str | int,
    message_id: str | int,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(RequirePermission("checkpointer", "read")),
):
    """Get message from checkpointer.

    Args:
        request: Request object
        payload: Get message schema with configuration and message ID
        service: Injected checkpointer service
        user: Current authenticated user

    Returns:
        Message response with message data or error
    """
    validate_thread_id(thread_id)
    if not message_id or (isinstance(message_id, str) and not str(message_id).strip()):
        raise HTTPException(status_code=422, detail="message_id is required and cannot be empty")

    config = {"thread_id": thread_id}

    result = await service.get_message(
        config,
        user,
        message_id,
    )

    return success_response(
        parse_message_output(get_settings(), result),
        request,
    )


@router.put(
    "/v1/threads/{thread_id}/messages/{message_id}",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(Message),
    summary="Set feedback on a message",
    description=(
        "Store a feedback object (at most 8 KB serialized, 10 levels deep) in the message's "
        "metadata under `feedback`, replacing any earlier feedback. Nothing else on the "
        "message can change."
    ),
)
async def put_message_feedback(
    request: Request,
    thread_id: str | int,
    message_id: str | int,
    payload: FeedbackSchema,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(RequirePermission("checkpointer", "write")),
):
    """Set feedback on a message.

    Args:
        request: Request object
        payload: Feedback schema with the feedback object
        service: Injected checkpointer service
        user: Current authenticated user

    Returns:
        The updated message, or 404 when the thread has no such message
    """
    validate_thread_id(thread_id)
    if not message_id or (isinstance(message_id, str) and not str(message_id).strip()):
        raise HTTPException(status_code=422, detail="message_id is required and cannot be empty")

    result = await service.update_message_feedback(
        {"thread_id": thread_id},
        user,
        message_id,
        payload.feedback,
    )

    return success_response(
        parse_message_output(get_settings(), result),
        request,
    )


@router.get(
    "/v1/threads/{thread_id}/messages",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(MessagesListResponseSchema),
    summary="List messages from checkpointer",
    description="Retrieve a list of messages from the checkpointer using configuration "
    "and optional filters.",
)
async def list_messages(
    request: Request,
    thread_id: int | str,
    search: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(RequirePermission("checkpointer", "read")),
):
    """List messages from checkpointer.

    Args:
        request: Request object
        payload: List messages schema with configuration and optional filters
        service: Injected checkpointer service
        user: Current authenticated user

    Returns:
        Messages list response with messages data or error
    """
    validate_thread_id(thread_id)
    if offset is not None and offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")
    if limit is not None and limit <= 0:
        raise HTTPException(status_code=422, detail="limit must be > 0")

    config = {"thread_id": thread_id}

    result = await service.get_messages(
        config,
        user,
        search,
        offset,
        _clamp_limit(limit),
    )

    return success_response(
        parse_message_output(get_settings(), result),
        request,
    )


@router.delete(
    "/v1/threads/{thread_id}/messages/{message_id}",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(ResponseSchema),
    summary="Delete message from checkpointer",
    description="Delete a specific message from the checkpointer using configuration and ID.",
)
async def delete_message(
    request: Request,
    message_id: str | int,
    thread_id: str | int,
    payload: ConfigSchema,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(RequirePermission("checkpointer", "delete")),
):
    """Delete message from checkpointer.

    Args:
        request: Request object
        payload: Delete message schema with configuration and message ID
        service: Injected checkpointer service
        user: Current authenticated user

    Returns:
        Success response or error
    """
    validate_thread_id(thread_id)
    if not message_id or (isinstance(message_id, str) and not str(message_id).strip()):
        raise HTTPException(status_code=422, detail="message_id is required and cannot be empty")

    # The path thread_id is the one RequirePermission checked; it is applied last so the
    # body config cannot redirect the call to another thread.
    config = {**client_config(payload.config), "thread_id": thread_id}

    async with thread_write_guard(thread_id):
        await service.delete_message(
            config,
            user,
            message_id,
        )

    return success_response(
        {"success": True, "message": "Message deleted successfully"},
        request,
    )


# Handle Threads


@router.get(
    "/v1/threads/{thread_id}",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(ThreadResponseSchema),
    summary="Get thread from checkpointer",
    description="Retrieve a specific thread from the checkpointer using configuration.",
)
async def get_thread(
    request: Request,
    thread_id: str | int,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(RequirePermission("checkpointer", "read")),
):
    """Get thread from checkpointer.

    Args:
        request: Request object
        payload: Get thread schema with configuration and thread ID
        service: Injected checkpointer service
        user: Current authenticated user

    Returns:
        Thread response with thread data or error
    """
    validate_thread_id(thread_id)
    result = await service.get_thread(
        {"thread_id": thread_id},
        user,
    )

    return success_response(
        {"thread_data": result},
        request,
    )


@router.get(
    "/v1/threads",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(ThreadsListResponseSchema),
    summary="List threads from checkpointer",
    description="Retrieve a list of threads from the checkpointer with optional filters.",
)
async def list_threads(
    request: Request,
    search: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(RequirePermission("checkpointer", "read")),
):
    """List threads from checkpointer.

    Args:
        request: Request object
        payload: List threads schema with configuration and optional filters
        service: Injected checkpointer service
        user: Current authenticated user

    Returns:
        Threads list response with threads data or error
    """
    if limit is not None and limit <= 0:
        raise HTTPException(status_code=422, detail="limit must be > 0")
    if offset is not None and offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")

    result = await service.list_threads(
        user,
        search,
        offset,
        _clamp_limit(limit),
    )

    return success_response(
        result,
        request,
    )


@router.delete(
    "/v1/threads/{thread_id}",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(ResponseSchema),
    summary="Delete thread from checkpointer",
    description="Delete a specific thread from the checkpointer using configuration and thread ID.",
)
async def delete_thread(
    request: Request,
    thread_id: str | int,
    payload: ConfigSchema,
    service: CheckpointerService = InjectAPI(CheckpointerService),
    user: dict[str, Any] = Depends(RequirePermission("checkpointer", "delete")),
):
    """Delete thread from checkpointer.

    Args:
        request: Request object
        payload: Delete thread schema with configuration and thread ID
        service: Injected checkpointer service
        user: Current authenticated user

    Returns:
        Success response or error
    """
    validate_thread_id(thread_id)
    # The path thread_id is the one RequirePermission checked; it is applied last so the
    # body config cannot redirect the call to another thread.
    config = {**client_config(payload.config), "thread_id": thread_id}

    async with thread_write_guard(thread_id):
        res = await service.delete_thread(
            config,
            user,
            thread_id,
        )

    return success_response(
        res,
        request,
    )
