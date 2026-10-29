"""Store router module."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from injectq.integrations import InjectAPI

from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.utils.response_helper import success_response
from tenxgraph_api.src.app.utils.swagger_helper import generate_swagger_responses

from .schemas.store_schemas import (
    DeleteMemorySchema,
    ForgetMemorySchema,
    GetMemorySchema,
    ListMemoriesSchema,
    MemoryCreateResponseSchema,
    MemoryItemResponseSchema,
    MemoryListResponseSchema,
    MemoryOperationResponseSchema,
    MemorySearchResponseSchema,
    SearchMemorySchema,
    StoreMemorySchema,
    UpdateMemorySchema,
)
from .services.store_service import StoreService


router = APIRouter(tags=["store"])


@router.post(
    "/v1/store/memories",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(MemoryCreateResponseSchema),
    summary="Store a memory",
    description="Persist a memory payload using the configured store backend.",
)
async def create_memory(
    request: Request,
    payload: StoreMemorySchema,
    service: StoreService = InjectAPI(StoreService),
    user: dict[str, Any] = Depends(RequirePermission("store", "write")),
):
    """Store a memory item using the configured store."""

    result = await service.store_memory(payload, user)
    return success_response(result, request, message="Memory stored successfully")


@router.post(
    "/v1/store/search",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(MemorySearchResponseSchema),
    summary="Search memories",
    description="Search memories stored in the backend based on semantic similarity and filters.",
)
async def search_memories(
    request: Request,
    payload: SearchMemorySchema,
    service: StoreService = InjectAPI(StoreService),
    user: dict[str, Any] = Depends(RequirePermission("store", "read")),
):
    """Search stored memories."""
    if not payload.query or (isinstance(payload.query, str) and not payload.query.strip()):
        raise HTTPException(status_code=422, detail="query is required and cannot be empty")

    result = await service.search_memories(payload, user)
    return success_response(result, request)


# NOTE: static sub-paths (/list, /forget) MUST be registered before the
# /{memory_id} catch-all below — FastAPI matches top-down, so otherwise
# `POST /v1/store/memories/list` binds to `{memory_id="list"}` and never lists.
@router.post(
    "/v1/store/memories/list",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(MemoryListResponseSchema),
    summary="List memories",
    description="List memories from the configured store backend.",
)
async def list_memories(
    request: Request,
    payload: ListMemoriesSchema | None = Body(
        default=None,
        description="Optional configuration, limit, and options for listing memories.",
    ),
    service: StoreService = InjectAPI(StoreService),
    user: dict[str, Any] = Depends(RequirePermission("store", "read")),
):
    """List stored memories."""

    if payload and payload.limit is not None and payload.limit <= 0:
        raise HTTPException(status_code=422, detail="limit must be > 0")

    if payload is None:
        payload = ListMemoriesSchema()
    cfg = payload.config or {}
    opts = payload.options
    result = await service.list_memories(
        cfg, user, limit=payload.limit, options=opts, offset=payload.offset
    )
    return success_response(result, request)


@router.put(
    "/v1/store/memories/{memory_id}",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(MemoryOperationResponseSchema),
    summary="Update a memory",
    description="Update the content or metadata of a stored memory.",
)
async def update_memory(
    request: Request,
    memory_id: str,
    payload: UpdateMemorySchema,
    service: StoreService = InjectAPI(StoreService),
    user: dict[str, Any] = Depends(RequirePermission("store", "write")),
):
    """Update a stored memory."""
    if not memory_id or not memory_id.strip():
        raise HTTPException(status_code=422, detail="memory_id is required and cannot be empty")

    result = await service.update_memory(memory_id, payload, user)
    return success_response(result, request, message="Memory updated successfully")


@router.delete(
    "/v1/store/memories/{memory_id}",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(MemoryOperationResponseSchema),
    summary="Delete a memory",
    description="Delete a stored memory by its identifier.",
)
async def delete_memory(
    request: Request,
    memory_id: str,
    payload: DeleteMemorySchema | None = Body(
        default=None,
        description="Optional configuration overrides forwarded to the store backend.",
    ),
    service: StoreService = InjectAPI(StoreService),
    user: dict[str, Any] = Depends(RequirePermission("store", "delete")),
):
    """Delete a stored memory."""
    if not memory_id or not memory_id.strip():
        raise HTTPException(status_code=422, detail="memory_id is required and cannot be empty")

    config_payload = payload.config if payload else {}
    options_payload = payload.options if payload else None
    result = await service.delete_memory(memory_id, config_payload, user, options=options_payload)
    return success_response(result, request, message="Memory deleted successfully")


@router.post(
    "/v1/store/memories/forget",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(MemoryOperationResponseSchema),
    summary="Forget memories",
    description="Forget memories matching the provided filters from the store backend.",
)
async def forget_memory(
    request: Request,
    payload: ForgetMemorySchema,
    service: StoreService = InjectAPI(StoreService),
    user: dict[str, Any] = Depends(RequirePermission("store", "delete")),
):
    """Forget memories based on filters."""

    result = await service.forget_memory(payload, user)
    return success_response(result, request, message="Memories removed successfully")


# Registered last so the static /list and /forget paths above take precedence
# over this {memory_id} catch-all.
@router.post(
    "/v1/store/memories/{memory_id}",
    status_code=status.HTTP_200_OK,
    responses=generate_swagger_responses(MemoryItemResponseSchema),
    summary="Get a memory",
    description="Retrieve a memory by its identifier from the configured store backend.",
)
async def get_memory(
    request: Request,
    memory_id: str,
    payload: GetMemorySchema | None = Body(
        default=None,
        description="Optional configuration and options for retrieving the memory.",
    ),
    service: StoreService = InjectAPI(StoreService),
    user: dict[str, Any] = Depends(RequirePermission("store", "read")),
):
    """Get a memory by ID."""
    if not memory_id or not memory_id.strip():
        raise HTTPException(status_code=422, detail="memory_id is required and cannot be empty")

    cfg = payload.config if payload else {}
    opts = payload.options if payload else None
    result = await service.get_memory(memory_id, cfg, user, options=opts)
    return success_response(result, request)
