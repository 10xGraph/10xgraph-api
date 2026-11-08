"""Tests for checkpointer router."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from tenxgraph_api.src.app.routers.checkpointer.router import (
    validate_thread_id,
)


class TestValidateThreadId:
    """Test validate_thread_id function."""

    def test_validate_thread_id_with_valid_string(self):
        """Test valid string thread_id."""
        # Should not raise
        validate_thread_id("thread-123")

    def test_validate_thread_id_with_empty_string(self):
        """Test empty string thread_id raises exception."""
        with pytest.raises(HTTPException) as exc_info:
            validate_thread_id("")
        assert exc_info.value.status_code == 422
        assert "empty or whitespace" in exc_info.value.detail

    def test_validate_thread_id_with_whitespace_string(self):
        """Test whitespace-only string thread_id raises exception."""
        with pytest.raises(HTTPException) as exc_info:
            validate_thread_id("   ")
        assert exc_info.value.status_code == 422
        assert "empty or whitespace" in exc_info.value.detail

    def test_validate_thread_id_with_valid_int(self):
        """Test valid positive integer thread_id."""
        # Should not raise
        validate_thread_id(1)
        validate_thread_id(999)

    def test_validate_thread_id_with_zero(self):
        """Test zero thread_id raises exception."""
        with pytest.raises(HTTPException) as exc_info:
            validate_thread_id(0)
        assert exc_info.value.status_code == 422
        assert "non-negative" in exc_info.value.detail

    def test_validate_thread_id_with_negative_int(self):
        """Test negative integer thread_id raises exception."""
        with pytest.raises(HTTPException) as exc_info:
            validate_thread_id(-1)
        assert exc_info.value.status_code == 422
        assert "non-negative" in exc_info.value.detail

    def test_validate_thread_id_with_invalid_type(self):
        """Test invalid type thread_id raises exception."""
        with pytest.raises(HTTPException) as exc_info:
            validate_thread_id([1, 2, 3])
        assert exc_info.value.status_code == 422
        assert "string or integer" in exc_info.value.detail


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
    @patch("tenxgraph_api.src.app.routers.checkpointer.router.success_response")
    async def test_get_state_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that get_state calls service with correct config."""
        from tenxgraph_api.src.app.routers.checkpointer.router import get_state

        mock_success_response.return_value = {"data": {}}
        mock_service.get_state.return_value = {"key": "value"}

        await get_state(
            request=mock_request,
            thread_id="thread-123",
            service=mock_service,
            identity=mock_user,
        )

        mock_service.get_state.assert_called_once_with({"thread_id": "thread-123"}, mock_user)

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
    @patch("tenxgraph_api.src.app.routers.checkpointer.router.success_response")
    async def test_put_state_merges_config(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that put_state merges config properly."""
        from tenxgraph_api.src.app.routers.checkpointer.router import put_state
        from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
            StateSchema,
        )

        mock_success_response.return_value = {"data": {}}
        mock_service.put_state.return_value = {}
        payload = StateSchema(state={"key": "value"}, config={"extra": "config"})

        await put_state(
            request=mock_request,
            thread_id="thread-123",
            payload=payload,
            service=mock_service,
            identity=mock_user,
        )

        call_args = mock_service.put_state.call_args
        config_arg = call_args[0][0]
        assert config_arg["thread_id"] == "thread-123"
        assert config_arg["extra"] == "config"

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


class TestClearStateLogic:
    """Test DELETE state endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.checkpointer.router.success_response")
    async def test_clear_state_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that clear_state calls service with correct config."""
        from tenxgraph_api.src.app.routers.checkpointer.router import clear_state

        mock_success_response.return_value = {"data": {}}
        mock_service.clear_state.return_value = {}

        await clear_state(
            request=mock_request,
            thread_id="thread-123",
            service=mock_service,
            identity=mock_user,
        )

        mock_service.clear_state.assert_called_once_with({"thread_id": "thread-123"}, mock_user)


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

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.checkpointer.router.success_response")
    async def test_get_message_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that get_message calls service."""
        from tenxgraph_api.src.app.routers.checkpointer.router import get_message

        mock_success_response.return_value = {"data": {}}
        mock_service.get_message.return_value = {}

        await get_message(
            request=mock_request,
            thread_id="thread-123",
            message_id="msg-1",
            service=mock_service,
            identity=mock_user,
        )

        mock_service.get_message.assert_called_once()


class TestListMessagesLogic:
    """Test GET messages endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.checkpointer.router.success_response")
    async def test_list_messages_passes_filters(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that list_messages passes filters to service."""
        from tenxgraph_api.src.app.routers.checkpointer.router import list_messages

        mock_success_response.return_value = {"data": {}}
        mock_service.get_messages.return_value = {}

        await list_messages(
            request=mock_request,
            thread_id="thread-123",
            search="test",
            offset=10,
            limit=20,
            service=mock_service,
            identity=mock_user,
        )

        call_args = mock_service.get_messages.call_args
        assert call_args[0][2] == "test"
        assert call_args[0][3] == 10
        assert call_args[0][4] == 20


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

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.checkpointer.router.success_response")
    async def test_delete_message_merges_config(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that delete_message merges config properly."""
        from tenxgraph_api.src.app.routers.checkpointer.router import delete_message
        from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
            ConfigSchema,
        )

        mock_success_response.return_value = {"data": {}}
        mock_service.delete_message.return_value = None
        payload = ConfigSchema(config={"extra": "config"})

        await delete_message(
            request=mock_request,
            thread_id="thread-123",
            message_id="msg-1",
            payload=payload,
            service=mock_service,
            identity=mock_user,
        )

        call_args = mock_service.delete_message.call_args
        config_arg = call_args[0][0]
        assert config_arg["extra"] == "config"


class TestGetThreadLogic:
    """Test GET thread endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.checkpointer.router.success_response")
    async def test_get_thread_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that get_thread calls service."""
        from tenxgraph_api.src.app.routers.checkpointer.router import get_thread

        mock_success_response.return_value = {"data": {}}
        mock_service.get_thread.return_value = {}

        await get_thread(
            request=mock_request,
            thread_id="thread-123",
            service=mock_service,
            identity=mock_user,
        )

        mock_service.get_thread.assert_called_once()


class TestListThreadsLogic:
    """Test GET threads endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.checkpointer.router.success_response")
    async def test_list_threads_passes_filters(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that list_threads passes filters to service."""
        from tenxgraph_api.src.app.routers.checkpointer.router import list_threads

        mock_success_response.return_value = {"data": {}}
        mock_service.list_threads.return_value = {}

        await list_threads(
            request=mock_request,
            search="test",
            offset=5,
            limit=10,
            service=mock_service,
            identity=mock_user,
        )

        call_args = mock_service.list_threads.call_args
        assert call_args[0][1] == "test"
        assert call_args[0][2] == 5
        assert call_args[0][3] == 10


class TestDeleteThreadLogic:
    """Test DELETE thread endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.checkpointer.router.success_response")
    async def test_delete_thread_merges_config(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that delete_thread merges config properly."""
        from tenxgraph_api.src.app.routers.checkpointer.router import delete_thread
        from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
            ConfigSchema,
        )

        mock_success_response.return_value = {"data": {}}
        mock_service.delete_thread.return_value = None
        payload = ConfigSchema(config={"extra": "config"})

        await delete_thread(
            request=mock_request,
            thread_id="thread-123",
            payload=payload,
            service=mock_service,
            identity=mock_user,
        )

        call_args = mock_service.delete_thread.call_args
        config_arg = call_args[0][0]
        assert config_arg["extra"] == "config"

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
