"""Checkpointer API schemas."""

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator
from tenxgraph.core.state import Message

from tenxgraph_api.src.app.core.auth.request_config import (
    ClientInputError,
    client_message_error,
    request_size_error,
)


class ConfigSchema(BaseModel):
    """Schema for state data."""

    config: dict[str, Any] | None = Field(
        default_factory=dict, description="Configuration for the state"
    )


class StateResponseSchema(BaseModel):
    """Schema for state response."""

    state: dict[str, Any] | None = Field(None, description="State data")


class StateSchema(ConfigSchema):
    """Schema for putting state."""

    state: dict[str, Any] = Field(..., description="State data")

    @field_validator("state")
    @classmethod
    def context_within_limits(cls, v: dict[str, Any]) -> dict[str, Any]:
        # ``context`` is client-written messages: same size limits as a run request.
        if error := request_size_error(v.get("context"), "state.context"):
            raise ClientInputError(error)
        return v


class PutMessagesSchema(ConfigSchema):
    """Schema for putting messages."""

    messages: list[Message] = Field(..., description="List of messages to store")
    metadata: dict[str, Any] | None = Field(None, description="Optional metadata")

    @field_validator("messages", mode="before")
    @classmethod
    def messages_within_limits(cls, v: Any) -> Any:
        if error := request_size_error(v):
            raise ClientInputError(error)
        return v

    @field_validator("messages")
    @classmethod
    def messages_must_not_call_tools(cls, v: list[Message]) -> list[Message]:
        # Admin import of history: any role is fine, but a tool call would still run on resume.
        if error := client_message_error(v, user_only=False):
            raise ValueError(error)
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


class GetMessageSchema(ConfigSchema):
    """Schema for getting a single message."""

    message_id: str = Field(..., description="Message ID to retrieve")


class ListMessagesSchema(ConfigSchema):
    """Schema for listing messages."""

    search: str | None = Field(None, description="Search query")
    offset: int | None = Field(None, description="Number of messages to skip")
    limit: int | None = Field(None, description="Maximum number of messages to return")


class DeleteMessageSchema(ConfigSchema):
    """Schema for deleting a message."""

    message_id: str = Field(..., description="Message ID to delete")


class PutThreadSchema(ConfigSchema):
    """Schema for putting thread info."""

    thread_info: dict[str, Any] = Field(..., description="Thread information to store")


class GetThreadSchema(ConfigSchema):
    """Schema for getting a thread."""

    thread_id: str = Field(..., description="Thread ID to retrieve")


class ListThreadsSchema(ConfigSchema):
    """Schema for listing threads."""

    search: str | None = Field(None, description="Search query")
    offset: int | None = Field(None, description="Number of threads to skip")
    limit: int | None = Field(None, description="Maximum number of threads to return")


class DeleteThreadSchema(ConfigSchema):
    """Schema for deleting a thread."""

    thread_id: str = Field(..., description="Thread ID to delete")


# Response schemas
class ResponseSchema(BaseModel):
    """Base response schema for checkpointer operations."""

    success: bool = Field(..., description="Whether the operation was successful")
    message: str = Field(..., description="Response message")
    data: Any | None = Field(None, description="Response data")


class MessageResponseSchema(BaseModel):
    """Response schema for message operations."""

    message: Message | None = Field(None, description="Message data")


class MessagesListResponseSchema(BaseModel):
    """Response schema for message list operations."""

    messages: list[Message] | None = Field(None, description="List of messages")


class ThreadResponseSchema(BaseModel):
    """Response schema for thread operations."""

    thread: dict[str, Any] | None = Field(None, description="Thread data")


class ThreadsListResponseSchema(BaseModel):
    """Response schema for thread list operations."""

    threads: list[dict[str, Any]] | None = Field(None, description="List of threads")
