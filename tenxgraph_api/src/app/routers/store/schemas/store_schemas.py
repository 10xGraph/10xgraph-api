"""Store API schemas."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints
from tenxgraph.core.state import Message
from tenxgraph.storage.store.store_schema import (
    DistanceMetric,
    MemoryRecord,
    MemorySearchResult,
    MemoryType,
    RetrievalStrategy,
)

from tenxgraph_api.src.app.core.auth.request_config import BoundedDict


# Upper bounds so one request cannot make the store (and the embedding / vector backend)
# return or rank an unbounded number of rows.
MAX_SEARCH_LIMIT = 100
MAX_LIST_LIMIT = 1000
MAX_LIST_OFFSET = 100_000
MAX_SEARCH_TOKENS = 16_000
# Character caps on free text. The text is embedded, so an unbounded body costs embedding
# tokens and memory. A query is a short question; stored content may be a long note.
MAX_QUERY_LENGTH = 8_000
MAX_CONTENT_LENGTH = 100_000

MemoryText = Annotated[str, StringConstraints(max_length=MAX_CONTENT_LENGTH)]


class BaseConfigSchema(BaseModel):
    """Base schema containing configuration overrides and store options."""

    config: BoundedDict | None = Field(
        default_factory=dict,
        description="Configuration values forwarded to the store backend.",
    )
    options: BoundedDict | None = Field(
        default=None,
        description="Extra keyword arguments to forward to the store backend.",
    )


class StoreMemorySchema(BaseConfigSchema):
    """Schema for storing a memory item."""

    content: MemoryText | Message = Field(..., description="Memory content or structured message.")
    memory_type: MemoryType = Field(
        default=MemoryType.EPISODIC,
        description="Memory classification used by the backend store.",
    )
    category: str = Field(default="general", description="Category label for the memory.")
    metadata: BoundedDict | None = Field(
        default=None,
        description="Arbitrary metadata associated with the memory.",
    )


class SearchMemorySchema(BaseConfigSchema):
    """Schema for searching memories."""

    query: str = Field(
        ...,
        max_length=MAX_QUERY_LENGTH,
        description="Textual query used for memory retrieval.",
    )
    memory_type: MemoryType | None = Field(
        default=None,
        description="Optional memory type filter.",
    )
    category: str | None = Field(
        default=None,
        description="Optional category filter.",
    )
    limit: int = Field(
        default=10,
        gt=0,
        le=MAX_SEARCH_LIMIT,
        description="Maximum number of results to return.",
    )
    score_threshold: float | None = Field(
        default=None,
        description="Minimum similarity score required for results.",
    )
    filters: BoundedDict | None = Field(
        default=None,
        description="Additional store-specific filters.",
    )
    retrieval_strategy: RetrievalStrategy = Field(
        default=RetrievalStrategy.SIMILARITY,
        description="Retrieval strategy used by the backend store.",
    )
    distance_metric: DistanceMetric = Field(
        default=DistanceMetric.COSINE,
        description="Distance metric applied during similarity search.",
    )
    max_tokens: int = Field(
        default=4000,
        gt=0,
        le=MAX_SEARCH_TOKENS,
        description="Maximum tokens used for truncation in similarity search.",
    )


class UpdateMemorySchema(BaseConfigSchema):
    """Schema for updating a memory."""

    content: MemoryText | Message = Field(..., description="Updated memory content or message.")
    metadata: BoundedDict | None = Field(
        default=None,
        description="Updated metadata for the memory.",
    )


class DeleteMemorySchema(BaseConfigSchema):
    """Schema for deleting a memory."""


class GetMemorySchema(BaseConfigSchema):
    """Schema for retrieving a single memory."""


class ListMemoriesSchema(BaseConfigSchema):
    """Schema for listing memories."""

    limit: int = Field(
        default=100,
        gt=0,
        le=MAX_LIST_LIMIT,
        description="Maximum number of memories to return.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=MAX_LIST_OFFSET,
        description="Number of memories to skip, for paging. Use next_offset from the response.",
    )


class ForgetMemorySchema(BaseConfigSchema):
    """Schema for forgetting memories based on filters."""

    memory_type: MemoryType | None = Field(
        default=None,
        description="Optional memory type to target for deletion.",
    )
    category: str | None = Field(
        default=None,
        description="Optional category to target for deletion.",
    )
    filters: BoundedDict | None = Field(
        default=None,
        description="Additional filters to control which memories are forgotten.",
    )


class MemoryCreateResponseSchema(BaseModel):
    """Response schema for create memory operations."""

    memory_id: str = Field(..., description="Identifier of the stored memory.")


class MemoryItemResponseSchema(BaseModel):
    """Response schema for single memory retrieval."""

    memory: MemorySearchResult | None = Field(
        default=None,
        description="Memory retrieved from the store, if available.",
    )


class MemoryListResponseSchema(BaseModel):
    """Response schema for listing memories."""

    memories: list[MemorySearchResult] = Field(
        default_factory=list,
        description="Collection of memories returned from the store.",
    )
    has_more: bool = Field(
        default=False,
        description="True when more memories follow this page.",
    )
    next_offset: int | None = Field(
        default=None,
        description="Offset to send for the next page, or null on the last page.",
    )


class MemorySearchResponseSchema(BaseModel):
    """Response schema for search operations."""

    results: list[MemorySearchResult] = Field(
        default_factory=list,
        description="Search results ranked by relevance.",
    )


class MemoryOperationResponseSchema(BaseModel):
    """Generic response schema for mutation operations."""

    success: bool = Field(..., description="Whether the store operation succeeded.")
    data: Any | None = Field(default=None, description="Optional payload returned by the store.")


__all__ = [
    "BaseConfigSchema",
    "DeleteMemorySchema",
    "DistanceMetric",
    "ForgetMemorySchema",
    "GetMemorySchema",
    "ListMemoriesSchema",
    "MemoryCreateResponseSchema",
    "MemoryItemResponseSchema",
    "MemoryListResponseSchema",
    "MemoryOperationResponseSchema",
    "MemoryRecord",
    "MemorySearchResponseSchema",
    "MemorySearchResult",
    "MemoryType",
    "RetrievalStrategy",
    "SearchMemorySchema",
    "StoreMemorySchema",
    "UpdateMemorySchema",
]
