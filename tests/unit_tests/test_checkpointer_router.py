"""Tests for checkpointer router."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from tenxgraph_api.src.app.routers.checkpointer.router import (
    validate_thread_id,
)


@pytest.mark.parametrize("thread_id", ["thread-123", 1, 999])
def test_validate_thread_id_accepts_valid_values(thread_id):
    validate_thread_id(thread_id)


@pytest.mark.parametrize(
    ("thread_id", "message"),
    [
        ("", "empty or whitespace"),
        ("   ", "empty or whitespace"),
        (0, "non-negative"),
        (-1, "non-negative"),
        ([1, 2, 3], "string or integer"),
    ],
)
def test_validate_thread_id_rejects_invalid_values(thread_id, message):
    with pytest.raises(HTTPException) as exc_info:
        validate_thread_id(thread_id)
    assert exc_info.value.status_code == 422
    assert message in exc_info.value.detail


@pytest.fixture
def mock_request():
    """Mock FastAPI request."""
    request = MagicMock()
    request.state.request_id = "test-request-id"
    request.state.timestamp = "2024-01-01T00:00:00Z"
    return request


@pytest.fixture
def mock_service():
    """Mock CheckpointerService."""
    return AsyncMock()


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    return {"id": "user-123", "name": "Test User"}


class TestGetStateLogic:
    """Test GET state endpoint logic."""


    @pytest.mark.asyncio
    async def test_get_state_validates_thread_id(self, mock_request, mock_service, mock_user):
        """Test that get_state validates thread_id."""
        from tenxgraph_api.src.app.routers.checkpointer.router import get_state

        with pytest.raises(HTTPException):
            await get_state(
                request=mock_request,
                thread_id=-1,
                service=mock_service,
                identity=mock_user,
            )


class TestPutStateLogic:
    """Test PUT state endpoint logic."""


    @pytest.mark.asyncio
    async def test_put_state_validates_thread_id(self, mock_request, mock_service, mock_user):
        """Test that put_state validates thread_id."""
        from tenxgraph_api.src.app.routers.checkpointer.router import put_state
        from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
            StateSchema,
        )

        payload = StateSchema(state={}, config=None)

        with pytest.raises(HTTPException):
            await put_state(
                request=mock_request,
                thread_id="",
                payload=payload,
                service=mock_service,
                identity=mock_user,
            )




class TestGetMessageLogic:
    """Test GET message endpoint logic."""

    @pytest.mark.asyncio
    async def test_get_message_validates_empty_message_id(
        self, mock_request, mock_service, mock_user
    ):
        """Test that get_message validates message_id."""
        from tenxgraph_api.src.app.routers.checkpointer.router import get_message

        with pytest.raises(HTTPException) as exc_info:
            await get_message(
                request=mock_request,
                thread_id="thread-123",
                message_id="",
                service=mock_service,
                identity=mock_user,
            )
        assert exc_info.value.status_code == 422





class TestDeleteMessageLogic:
    """Test DELETE message endpoint logic."""

    @pytest.mark.asyncio
    async def test_delete_message_validates_message_id(self, mock_request, mock_service, mock_user):
        """Test that delete_message validates message_id."""
        from tenxgraph_api.src.app.routers.checkpointer.router import delete_message
        from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
            ConfigSchema,
        )

        payload = ConfigSchema(config=None)

        with pytest.raises(HTTPException) as exc_info:
            await delete_message(
                request=mock_request,
                thread_id="thread-123",
                message_id="",
                payload=payload,
                service=mock_service,
                identity=mock_user,
            )
        assert exc_info.value.status_code == 422







class TestDeleteThreadLogic:
    """Test DELETE thread endpoint logic."""


    @pytest.mark.asyncio
    async def test_delete_thread_validates_thread_id(self, mock_request, mock_service, mock_user):
        """Test that delete_thread validates thread_id."""
        from tenxgraph_api.src.app.routers.checkpointer.router import delete_thread
        from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
            ConfigSchema,
        )

        payload = ConfigSchema(config=None)

        with pytest.raises(HTTPException):
            await delete_thread(
                request=mock_request,
                thread_id="",
                payload=payload,
                service=mock_service,
                identity=mock_user,
            )
