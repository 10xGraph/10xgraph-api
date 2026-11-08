"""Tests for store router."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.fixture
def mock_request():
    """Mock FastAPI request."""
    request = MagicMock()
    request.state.request_id = "test-request-id"
    request.state.timestamp = "2024-01-01T00:00:00Z"
    return request


@pytest.fixture
def mock_service():
    """Mock StoreService."""
    return AsyncMock()


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    return {"id": "user-123", "name": "Test User"}


class TestCreateMemoryLogic:
    """Test POST /v1/store/memories endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.store.router.success_response")
    async def test_create_memory_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that create_memory calls service."""
        from tenxgraph_api.src.app.routers.store.router import create_memory
        from tenxgraph_api.src.app.routers.store.schemas.store_schemas import StoreMemorySchema

        mock_success_response.return_value = {"data": {}}
        mock_service.store_memory.return_value = {"id": "mem-1"}
        payload = StoreMemorySchema(content="Test memory", metadata=None)

        result = await create_memory(
            request=mock_request,
            payload=payload,
            service=mock_service,
            identity=mock_user,
        )

        mock_service.store_memory.assert_called_once()
        assert result == {"data": {}}


class TestSearchMemoriesLogic:
    """Test POST /v1/store/search endpoint logic."""

    @pytest.mark.asyncio
    async def test_search_memories_validates_empty_query(
        self, mock_request, mock_service, mock_user
    ):
        """Test that search_memories validates empty query."""
        from tenxgraph_api.src.app.routers.store.router import search_memories
        from tenxgraph_api.src.app.routers.store.schemas.store_schemas import SearchMemorySchema

        payload = SearchMemorySchema(query="", metadata_filters=None)

        with pytest.raises(HTTPException) as exc_info:
            await search_memories(
                request=mock_request,
                payload=payload,
                service=mock_service,
                identity=mock_user,
            )
        assert exc_info.value.status_code == 422
        assert "empty" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_search_memories_validates_whitespace_query(
        self, mock_request, mock_service, mock_user
    ):
        """Test that search_memories validates whitespace query."""
        from tenxgraph_api.src.app.routers.store.router import search_memories
        from tenxgraph_api.src.app.routers.store.schemas.store_schemas import SearchMemorySchema

        payload = SearchMemorySchema(query="   ", metadata_filters=None)

        with pytest.raises(HTTPException) as exc_info:
            await search_memories(
                request=mock_request,
                payload=payload,
                service=mock_service,
                identity=mock_user,
            )
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.store.router.success_response")
    async def test_search_memories_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that search_memories calls service."""
        from tenxgraph_api.src.app.routers.store.router import search_memories
        from tenxgraph_api.src.app.routers.store.schemas.store_schemas import SearchMemorySchema

        mock_success_response.return_value = {"data": {}}
        mock_service.search_memories.return_value = {"results": []}
        payload = SearchMemorySchema(query="test search", metadata_filters=None)

        result = await search_memories(
            request=mock_request,
            payload=payload,
            service=mock_service,
            identity=mock_user,
        )

        mock_service.search_memories.assert_called_once()


class TestGetMemoryLogic:
    """Test POST /v1/store/memories/{memory_id} endpoint logic."""

    @pytest.mark.asyncio
    async def test_get_memory_validates_empty_memory_id(
        self, mock_request, mock_service, mock_user
    ):
        """Test that get_memory validates empty memory_id."""
        from tenxgraph_api.src.app.routers.store.router import get_memory

        with pytest.raises(HTTPException) as exc_info:
            await get_memory(
                request=mock_request,
                memory_id="",
                payload=None,
                service=mock_service,
                identity=mock_user,
            )
        assert exc_info.value.status_code == 422
        assert "empty" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_memory_validates_whitespace_memory_id(
        self, mock_request, mock_service, mock_user
    ):
        """Test that get_memory validates whitespace memory_id."""
        from tenxgraph_api.src.app.routers.store.router import get_memory

        with pytest.raises(HTTPException) as exc_info:
            await get_memory(
                request=mock_request,
                memory_id="   ",
                payload=None,
                service=mock_service,
                identity=mock_user,
            )
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.store.router.success_response")
    async def test_get_memory_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that get_memory calls service."""
        from tenxgraph_api.src.app.routers.store.router import get_memory

        mock_success_response.return_value = {"data": {}}
        mock_service.get_memory.return_value = {"id": "mem-1", "content": "test"}

        result = await get_memory(
            request=mock_request,
            memory_id="mem-1",
            payload=None,
            service=mock_service,
            identity=mock_user,
        )

        mock_service.get_memory.assert_called_once()


class TestListMemoriesLogic:
    """Test POST /v1/store/memories/list endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.store.router.success_response")
    async def test_list_memories_with_default_payload(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that list_memories uses default payload when None."""
        from tenxgraph_api.src.app.routers.store.router import list_memories

        mock_success_response.return_value = {"data": {}}
        mock_service.list_memories.return_value = {"memories": []}

        result = await list_memories(
            request=mock_request,
            payload=None,
            service=mock_service,
            identity=mock_user,
        )

        mock_service.list_memories.assert_called_once()

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.store.router.success_response")
    async def test_list_memories_passes_options(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that list_memories passes options to service."""
        from tenxgraph_api.src.app.routers.store.router import list_memories
        from tenxgraph_api.src.app.routers.store.schemas.store_schemas import ListMemoriesSchema

        mock_success_response.return_value = {"data": {}}
        mock_service.list_memories.return_value = {"memories": []}
        payload = ListMemoriesSchema(config={"key": "value"}, limit=10, options={"opt": "value"})

        result = await list_memories(
            request=mock_request,
            payload=payload,
            service=mock_service,
            identity=mock_user,
        )

        call_args = mock_service.list_memories.call_args
        assert call_args[0][0] == {"key": "value"}  # config
        assert call_args[1]["limit"] == 10
        assert call_args[1]["options"] == {"opt": "value"}


class TestUpdateMemoryLogic:
    """Test PUT /v1/store/memories/{memory_id} endpoint logic."""

    @pytest.mark.asyncio
    async def test_update_memory_validates_empty_memory_id(
        self, mock_request, mock_service, mock_user
    ):
        """Test that update_memory validates empty memory_id."""
        from tenxgraph_api.src.app.routers.store.router import update_memory
        from tenxgraph_api.src.app.routers.store.schemas.store_schemas import UpdateMemorySchema

        payload = UpdateMemorySchema(content="new content")

        with pytest.raises(HTTPException) as exc_info:
            await update_memory(
                request=mock_request,
                memory_id="",
                payload=payload,
                service=mock_service,
                identity=mock_user,
            )
        assert exc_info.value.status_code == 422
        assert "empty" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.store.router.success_response")
    async def test_update_memory_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that update_memory calls service."""
        from tenxgraph_api.src.app.routers.store.router import update_memory
        from tenxgraph_api.src.app.routers.store.schemas.store_schemas import UpdateMemorySchema

        mock_success_response.return_value = {"data": {}}
        mock_service.update_memory.return_value = {"success": True}
        payload = UpdateMemorySchema(content="new content")

        result = await update_memory(
            request=mock_request,
            memory_id="mem-1",
            payload=payload,
            service=mock_service,
            identity=mock_user,
        )

        mock_service.update_memory.assert_called_once_with("mem-1", payload, mock_user)


class TestDeleteMemoryLogic:
    """Test DELETE /v1/store/memories/{memory_id} endpoint logic."""

    @pytest.mark.asyncio
    async def test_delete_memory_validates_empty_memory_id(
        self, mock_request, mock_service, mock_user
    ):
        """Test that delete_memory validates empty memory_id."""
        from tenxgraph_api.src.app.routers.store.router import delete_memory

        with pytest.raises(HTTPException) as exc_info:
            await delete_memory(
                request=mock_request,
                memory_id="",
                payload=None,
                service=mock_service,
                identity=mock_user,
            )
        assert exc_info.value.status_code == 422
        assert "empty" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_delete_memory_validates_whitespace_memory_id(
        self, mock_request, mock_service, mock_user
    ):
        """Test that delete_memory validates whitespace memory_id."""
        from tenxgraph_api.src.app.routers.store.router import delete_memory

        with pytest.raises(HTTPException) as exc_info:
            await delete_memory(
                request=mock_request,
                memory_id="   ",
                payload=None,
                service=mock_service,
                identity=mock_user,
            )
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.store.router.success_response")
    async def test_delete_memory_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that delete_memory calls service."""
        from tenxgraph_api.src.app.routers.store.router import delete_memory

        mock_success_response.return_value = {"data": {}}
        mock_service.delete_memory.return_value = {"success": True}

        result = await delete_memory(
            request=mock_request,
            memory_id="mem-1",
            payload=None,
            service=mock_service,
            identity=mock_user,
        )

        mock_service.delete_memory.assert_called_once()

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.store.router.success_response")
    async def test_delete_memory_with_config_and_options(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that delete_memory passes config and options."""
        from tenxgraph_api.src.app.routers.store.router import delete_memory
        from tenxgraph_api.src.app.routers.store.schemas.store_schemas import DeleteMemorySchema

        mock_success_response.return_value = {"data": {}}
        mock_service.delete_memory.return_value = {"success": True}
        payload = DeleteMemorySchema(config={"key": "value"}, options={"opt": "val"})

        result = await delete_memory(
            request=mock_request,
            memory_id="mem-1",
            payload=payload,
            service=mock_service,
            identity=mock_user,
        )

        call_args = mock_service.delete_memory.call_args
        assert call_args[0][0] == "mem-1"  # memory_id
        assert call_args[0][1] == {"key": "value"}  # config
        assert call_args[1]["options"] == {"opt": "val"}


class TestForgetMemoryLogic:
    """Test POST /v1/store/memories/forget endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.store.router.success_response")
    async def test_forget_memory_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that forget_memory calls service."""
        from tenxgraph_api.src.app.routers.store.router import forget_memory
        from tenxgraph_api.src.app.routers.store.schemas.store_schemas import ForgetMemorySchema

        mock_success_response.return_value = {"data": {}}
        mock_service.forget_memory.return_value = {"forgotten_count": 5}
        payload = ForgetMemorySchema(filters={"type": "old"})

        result = await forget_memory(
            request=mock_request,
            payload=payload,
            service=mock_service,
            identity=mock_user,
        )

        mock_service.forget_memory.assert_called_once_with(payload, mock_user)
