"""Checkpointer API schemas."""

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator
from tenxgraph.core.state import Message

from tenxgraph_api.src.app.core.validation.request_config import (
    BoundedDict,
    ClientInputError,
    request_size_error,
)


class ConfigSchema(BaseModel):
    """Schema for state data."""

    config: BoundedDict | None = Field(
        default_factory=dict, description="Configuration for the state"
    )


class StateResponseSchema(BaseModel):
    """Schema for state response."""

    state: dict[str, Any] | None = Field(default=None, description="State data")


class StateSchema(ConfigSchema):
    """Schema for putting state."""

    state: BoundedDict = Field(..., description="State data")

    @field_validator("state")
    @classmethod
    def context_within_limits(cls, v: dict[str, Any]) -> dict[str, Any]:
        # ``context`` is client-written messages: same size limits as a run request.
        if error := request_size_error(v.get("context"), "state.context"):
            raise ClientInputError(error)
        return v


MAX_FEEDBACK_BYTES = 8192
MAX_FEEDBACK_DEPTH = 10


def _deeper_than(value: Any, limit: int) -> bool:
    """Whether ``value`` (level 1) holds a dict or list at a level deeper than ``limit``."""
    if not isinstance(value, dict | list):
        return False
    if limit < 1:
        return True
    children = value.values() if isinstance(value, dict) else value
    return any(_deeper_than(child, limit - 1) for child in children)


class FeedbackSchema(BaseModel):
    """Schema for ``PUT /v1/threads/{thread_id}/messages/{message_id}``: feedback only."""

    feedback: dict[str, Any] = Field(
        ...,
        description=(
            f"Any JSON object, stored whole in the message's metadata['feedback']. At most "
            f"{MAX_FEEDBACK_BYTES} bytes serialized and {MAX_FEEDBACK_DEPTH} levels deep."
        ),
    )

    @field_validator("feedback")
    @classmethod
    def feedback_within_limits(cls, v: dict[str, Any]) -> dict[str, Any]:
        if _deeper_than(v, MAX_FEEDBACK_DEPTH):
            raise ValueError(f"feedback is nested deeper than {MAX_FEEDBACK_DEPTH} levels")
        if len(json.dumps(v)) > MAX_FEEDBACK_BYTES:
            raise ValueError(f"feedback is larger than {MAX_FEEDBACK_BYTES} bytes")
        return v


# Response schemas
class ResponseSchema(BaseModel):
    """Base response schema for checkpointer operations."""

    success: bool = Field(..., description="Whether the operation was successful")
    message: str = Field(..., description="Response message")
    data: Any | None = Field(default=None, description="Response data")


class MessagesListResponseSchema(BaseModel):
    """Response schema for message list operations."""

    messages: list[Message] | None = Field(default=None, description="List of messages")
    has_more: bool | None = Field(default=None, description="Whether another page follows")
    offset: int | None = Field(default=None, description="Offset applied to this page")
    limit: int | None = Field(default=None, description="Page size applied to this page")


class ThreadResponseSchema(BaseModel):
    """Response schema for thread operations."""

    thread: dict[str, Any] | None = Field(default=None, description="Thread data")


class ThreadsListResponseSchema(BaseModel):
    """Response schema for thread list operations."""

    threads: list[dict[str, Any]] | None = Field(default=None, description="List of threads")
    has_more: bool | None = Field(default=None, description="Whether another page follows")
    offset: int | None = Field(default=None, description="Offset applied to this page")
    limit: int | None = Field(default=None, description="Page size applied to this page")
