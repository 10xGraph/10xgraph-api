"""Unit tests for StoreService."""

from uuid import uuid4

import pytest
from tenxgraph.storage.store.store_schema import DistanceMetric, MemoryType, RetrievalStrategy

from tenxgraph_api.src.app.routers.store.schemas.store_schemas import (
    ForgetMemorySchema,
    SearchMemorySchema,
    StoreMemorySchema,
    UpdateMemorySchema,
)


@pytest.mark.asyncio
class TestStoreMemory:
    """Tests for store_memory method."""

    async def test_store_memory_with_string_content(self, store_service, mock_store, mock_user):
        """Test storing a memory with string content."""
        # Arrange
        memory_id = str(uuid4())
        mock_store.astore.return_value = memory_id
        payload = StoreMemorySchema(
            content="Test memory content",
            memory_type=MemoryType.EPISODIC,
            category="general",
            metadata={"tag": "test"},
        )

        # Act
        result = await store_service.store_memory(payload, mock_user)

        # Assert
        assert result.memory_id == memory_id
        mock_store.astore.assert_called_once()
        call_args = mock_store.astore.call_args
        assert call_args[0][1] == "Test memory content"
        assert call_args[1]["memory_type"] == MemoryType.EPISODIC
        assert call_args[1]["category"] == "general"
        assert call_args[1]["metadata"] == {"tag": "test"}

    async def test_store_memory_with_message_content(
        self, store_service, mock_store, mock_user, sample_message
    ):
        """Test storing a memory with Message content."""
        # Arrange
        memory_id = str(uuid4())
        mock_store.astore.return_value = memory_id
        payload = StoreMemorySchema(
            content=sample_message,
            memory_type=MemoryType.SEMANTIC,
            category="conversation",
        )

        # Act
        result = await store_service.store_memory(payload, mock_user)

        # Assert
        assert result.memory_id == memory_id
        mock_store.astore.assert_called_once()
        call_args = mock_store.astore.call_args
        assert call_args[0][1] == sample_message
        assert call_args[1]["memory_type"] == MemoryType.SEMANTIC

    async def test_store_memory_with_custom_config(self, store_service, mock_store, mock_user):
        """Test storing memory with custom configuration."""
        # Arrange
        memory_id = str(uuid4())
        mock_store.astore.return_value = memory_id
        custom_config = {"embedding_model": "custom-model"}
        payload = StoreMemorySchema(
            content="Test memory",
            config=custom_config,
        )

        # Act
        result = await store_service.store_memory(payload, mock_user)

        # Assert
        assert result.memory_id == memory_id
        call_args = mock_store.astore.call_args
        config = call_args[0][0]
        assert config["embedding_model"] == "custom-model"
        assert config["user_id"] == "test-user-123"

    async def test_store_memory_with_options(self, store_service, mock_store, mock_user):
        """Test storing memory with additional options."""
        # Arrange
        memory_id = str(uuid4())
        mock_store.astore.return_value = memory_id
        payload = StoreMemorySchema(
            content="Test memory",
            options={"infer": False, "replace_metadata": True},
        )

        # Act
        result = await store_service.store_memory(payload, mock_user)

        # Assert
        assert result.memory_id == memory_id
        call_args = mock_store.astore.call_args
        assert call_args[1]["infer"] is False
        assert call_args[1]["replace_metadata"] is True

    async def test_store_memory_no_store_raises_error(self, mock_user):
        """Test storing memory when store is not configured."""
        # Arrange
        from tenxgraph_api.src.app.routers.store.services.store_service import (
            StoreService,
        )

        service = StoreService(store=None)
        payload = StoreMemorySchema(content="Test memory")

        # Act & Assert
        with pytest.raises(ValueError, match="Store is not configured"):
            await service.store_memory(payload, mock_user)


@pytest.mark.asyncio
class TestSearchMemories:
    """Tests for search_memories method."""

    async def test_search_memories_basic(
        self, store_service, mock_store, mock_user, sample_memory_results
    ):
        """Test basic memory search."""
        # Arrange
        mock_store.asearch.return_value = sample_memory_results
        payload = SearchMemorySchema(query="test query")

        # Act
        result = await store_service.search_memories(payload, mock_user)

        # Assert
        assert len(result.results) == 2
        assert result.results[0].content == "First memory"
        mock_store.asearch.assert_called_once()

    async def test_search_memories_with_filters(
        self, store_service, mock_store, mock_user, sample_memory_results
    ):
        """Test memory search with filters."""
        # Arrange
        mock_store.asearch.return_value = sample_memory_results
        payload = SearchMemorySchema(
            query="test query",
            memory_type=MemoryType.EPISODIC,
            category="general",
            limit=5,
            score_threshold=0.8,
            filters={"tag": "important"},
        )

        # Act
        result = await store_service.search_memories(payload, mock_user)

        # Assert
        assert len(result.results) == 2
        call_args = mock_store.asearch.call_args
        assert call_args[0][1] == "test query"
        assert call_args[1]["memory_type"] == MemoryType.EPISODIC
        assert call_args[1]["category"] == "general"
        assert call_args[1]["limit"] == 5
        assert call_args[1]["score_threshold"] == 0.8
        assert call_args[1]["filters"] == {"tag": "important"}

    async def test_search_memories_with_retrieval_strategy(
        self, store_service, mock_store, mock_user, sample_memory_results
    ):
        """Test memory search with retrieval strategy."""
        # Arrange
        mock_store.asearch.return_value = sample_memory_results
        payload = SearchMemorySchema(
            query="test query",
            retrieval_strategy=RetrievalStrategy.HYBRID,
            distance_metric=DistanceMetric.EUCLIDEAN,
            max_tokens=2000,
        )

        # Act
        result = await store_service.search_memories(payload, mock_user)

        # Assert
        call_args = mock_store.asearch.call_args
        assert call_args[1]["retrieval_strategy"] == RetrievalStrategy.HYBRID
        assert call_args[1]["distance_metric"] == DistanceMetric.EUCLIDEAN
        assert call_args[1]["max_tokens"] == 2000

    async def test_search_memories_empty_results(self, store_service, mock_store, mock_user):
        """Test memory search with no results."""
        # Arrange
        mock_store.asearch.return_value = []
        payload = SearchMemorySchema(query="nonexistent query")

        # Act
        result = await store_service.search_memories(payload, mock_user)

        # Assert
        assert len(result.results) == 0


@pytest.mark.asyncio
class TestGetMemory:
    """Tests for get_memory method."""

    async def test_get_memory_success(
        self, store_service, mock_store, mock_user, sample_memory_id, sample_memory_result
    ):
        """Test retrieving a memory by ID."""
        # Arrange
        mock_store.aget.return_value = sample_memory_result

        # Act
        result = await store_service.get_memory(sample_memory_id, {}, mock_user)

        # Assert
        assert result.memory == sample_memory_result
        mock_store.aget.assert_called_once_with(
            {"user": mock_user, "user_id": "test-user-123"}, sample_memory_id
        )

    async def test_get_memory_with_config(
        self, store_service, mock_store, mock_user, sample_memory_id, sample_memory_result
    ):
        """Test retrieving memory with custom config."""
        # Arrange
        mock_store.aget.return_value = sample_memory_result
        config = {"custom": "value"}

        # Act
        result = await store_service.get_memory(sample_memory_id, config, mock_user)

        # Assert
        call_args = mock_store.aget.call_args
        assert call_args[0][0]["custom"] == "value"
        assert call_args[0][0]["user_id"] == "test-user-123"

    async def test_get_memory_with_options(
        self, store_service, mock_store, mock_user, sample_memory_id, sample_memory_result
    ):
        """Test retrieving memory with options."""
        # Arrange
        mock_store.aget.return_value = sample_memory_result
        options = {"infer": False}

        # Act
        result = await store_service.get_memory(sample_memory_id, {}, mock_user, options=options)

        # Assert
        call_args = mock_store.aget.call_args
        assert call_args[1]["infer"] is False

    async def test_get_memory_not_found(
        self, store_service, mock_store, mock_user, sample_memory_id
    ):
        """Test retrieving non-existent memory."""
        # Arrange
        mock_store.aget.return_value = None

        # Act
        result = await store_service.get_memory(sample_memory_id, {}, mock_user)

        # Assert
        assert result.memory is None


@pytest.mark.asyncio
class TestListMemories:
    """Tests for list_memories method."""

    async def test_list_memories_default(
        self, store_service, mock_store, mock_user, sample_memory_results
    ):
        """Test listing memories with default limit."""
        # Arrange
        mock_store.aget_all.return_value = sample_memory_results

        # Act
        result = await store_service.list_memories({}, mock_user)

        # Assert
        assert len(result.memories) == 2
        mock_store.aget_all.assert_called_once()
        call_args = mock_store.aget_all.call_args
        assert call_args[1]["limit"] == 100

    async def test_list_memories_custom_limit(
        self, store_service, mock_store, mock_user, sample_memory_results
    ):
        """Test listing memories with custom limit."""
        # Arrange
        mock_store.aget_all.return_value = sample_memory_results[:1]

        # Act
        result = await store_service.list_memories({}, mock_user, limit=1)

        # Assert
        assert len(result.memories) == 1
        call_args = mock_store.aget_all.call_args
        assert call_args[1]["limit"] == 1

    async def test_list_memories_with_options(
        self, store_service, mock_store, mock_user, sample_memory_results
    ):
        """Test listing memories with options."""
        # Arrange
        mock_store.aget_all.return_value = sample_memory_results
        options = {"infer": False}

        # Act
        result = await store_service.list_memories({}, mock_user, options=options)

        # Assert
        call_args = mock_store.aget_all.call_args
        assert call_args[1]["infer"] is False

    async def test_list_memories_empty(self, store_service, mock_store, mock_user):
        """Test listing memories when none exist."""
        # Arrange
        mock_store.aget_all.return_value = []

        # Act
        result = await store_service.list_memories({}, mock_user)

        # Assert
        assert len(result.memories) == 0


@pytest.mark.asyncio
class TestUpdateMemory:
    """Tests for update_memory method."""

    async def test_update_memory_with_string(
        self, store_service, mock_store, mock_user, sample_memory_id
    ):
        """Test updating memory with string content."""
        # Arrange
        mock_store.aupdate.return_value = {"updated": True}
        payload = UpdateMemorySchema(
            content="Updated content",
            metadata={"updated": True},
        )

        # Act
        result = await store_service.update_memory(sample_memory_id, payload, mock_user)

        # Assert
        assert result.success is True
        assert result.data == {"updated": True}
        mock_store.aupdate.assert_called_once()
        call_args = mock_store.aupdate.call_args
        assert call_args[0][1] == sample_memory_id
        assert call_args[0][2] == "Updated content"
        assert call_args[1]["metadata"] == {"updated": True}

    async def test_update_memory_with_message(
        self, store_service, mock_store, mock_user, sample_memory_id, sample_message
    ):
        """Test updating memory with Message content."""
        # Arrange
        mock_store.aupdate.return_value = {"updated": True}
        payload = UpdateMemorySchema(content=sample_message)

        # Act
        result = await store_service.update_memory(sample_memory_id, payload, mock_user)

        # Assert
        assert result.success is True
        call_args = mock_store.aupdate.call_args
        assert call_args[0][2] == sample_message

    async def test_update_memory_with_options(
        self, store_service, mock_store, mock_user, sample_memory_id
    ):
        """Test updating memory with options."""
        # Arrange
        mock_store.aupdate.return_value = {"updated": True}
        payload = UpdateMemorySchema(
            content="Updated content",
            options={"replace_metadata": True},
        )

        # Act
        result = await store_service.update_memory(sample_memory_id, payload, mock_user)

        # Assert
        call_args = mock_store.aupdate.call_args
        assert call_args[1]["replace_metadata"] is True


@pytest.mark.asyncio
class TestDeleteMemory:
    """Tests for delete_memory method."""

    async def test_delete_memory_success(
        self, store_service, mock_store, mock_user, sample_memory_id
    ):
        """Test deleting a memory."""
        # Arrange
        mock_store.adelete.return_value = {"deleted": True}

        # Act
        result = await store_service.delete_memory(sample_memory_id, {}, mock_user)

        # Assert
        assert result.success is True
        assert result.data == {"deleted": True}
        mock_store.adelete.assert_called_once_with(
            {"user": mock_user, "user_id": "test-user-123"}, sample_memory_id
        )

    async def test_delete_memory_with_config(
        self, store_service, mock_store, mock_user, sample_memory_id
    ):
        """Test deleting memory with config."""
        # Arrange
        mock_store.adelete.return_value = {"deleted": True}
        config = {"soft_delete": True}

        # Act
        result = await store_service.delete_memory(sample_memory_id, config, mock_user)

        # Assert
        call_args = mock_store.adelete.call_args
        assert call_args[0][0]["soft_delete"] is True

    async def test_delete_memory_with_options(
        self, store_service, mock_store, mock_user, sample_memory_id
    ):
        """Test deleting memory with options."""
        # Arrange
        mock_store.adelete.return_value = {"deleted": True}
        options = {"infer": False}

        # Act
        result = await store_service.delete_memory(sample_memory_id, {}, mock_user, options=options)

        # Assert
        call_args = mock_store.adelete.call_args
        assert call_args[1]["infer"] is False


@pytest.mark.asyncio
class TestForgetMemory:
    """Tests for forget_memory method."""

    async def test_forget_memory_with_type(self, store_service, mock_store, mock_user):
        """Test forgetting memories by type."""
        # Arrange
        mock_store.aforget_memory.return_value = {"count": 5}
        payload = ForgetMemorySchema(memory_type=MemoryType.EPISODIC)

        # Act
        result = await store_service.forget_memory(payload, mock_user)

        # Assert
        assert result.success is True
        assert result.data == {"count": 5}
        call_args = mock_store.aforget_memory.call_args
        assert call_args[1]["memory_type"] == MemoryType.EPISODIC

    async def test_forget_memory_with_category(self, store_service, mock_store, mock_user):
        """Test forgetting memories by category."""
        # Arrange
        mock_store.aforget_memory.return_value = {"count": 3}
        payload = ForgetMemorySchema(category="work")

        # Act
        result = await store_service.forget_memory(payload, mock_user)

        # Assert
        call_args = mock_store.aforget_memory.call_args
        assert call_args[1]["category"] == "work"

    async def test_forget_memory_with_filters(self, store_service, mock_store, mock_user):
        """Test forgetting memories with filters."""
        # Arrange
        mock_store.aforget_memory.return_value = {"count": 2}
        payload = ForgetMemorySchema(
            memory_type=MemoryType.SEMANTIC,
            category="personal",
            filters={"tag": "old"},
        )

        # Act
        result = await store_service.forget_memory(payload, mock_user)

        # Assert
        call_args = mock_store.aforget_memory.call_args
        assert call_args[1]["memory_type"] == MemoryType.SEMANTIC
        assert call_args[1]["category"] == "personal"
        assert call_args[1]["filters"] == {"tag": "old"}

    async def test_forget_memory_with_options(self, store_service, mock_store, mock_user):
        """Test forgetting memories with options."""
        # Arrange
        mock_store.aforget_memory.return_value = {"count": 1}
        payload = ForgetMemorySchema(
            memory_type=MemoryType.EPISODIC,
            options={"infer": False},
        )

        # Act
        result = await store_service.forget_memory(payload, mock_user)

        # Assert
        call_args = mock_store.aforget_memory.call_args
        assert call_args[1]["infer"] is False

    async def test_forget_memory_excludes_none_values(self, store_service, mock_store, mock_user):
        """Test that None values are excluded from forget call."""
        # Arrange
        mock_store.aforget_memory.return_value = {"count": 0}
        payload = ForgetMemorySchema(memory_type=None, category=None, filters=None)

        # Act
        result = await store_service.forget_memory(payload, mock_user)

        # Assert
        call_args = mock_store.aforget_memory.call_args
        # Only config should be passed, no memory_type, category, or filters
        assert "memory_type" not in call_args[1]
        assert "category" not in call_args[1]
        assert "filters" not in call_args[1]


@pytest.mark.asyncio
class TestReservedFilterKeys:
    """J-4: client filters never carry owner keys to the store."""

    HOSTILE = {
        "user_id": "victim",
        "agent_id": "a",
        "app_id": "b",
        "run_id": "c",
        "source": "chat",
        "OR": [{"user_id": "victim"}, {"tag": "x"}],
    }

    async def test_search_drops_reserved_keys(self, store_service, mock_store, mock_user):
        mock_store.asearch.return_value = []
        await store_service.search_memories(
            SearchMemorySchema(query="q", filters=dict(self.HOSTILE)), mock_user
        )
        assert mock_store.asearch.call_args.kwargs["filters"] == {
            "source": "chat",
            "OR": [{"tag": "x"}],
        }

    async def test_forget_drops_reserved_keys(self, store_service, mock_store, mock_user):
        await store_service.forget_memory(ForgetMemorySchema(filters=dict(self.HOSTILE)), mock_user)
        assert mock_store.aforget_memory.call_args.kwargs["filters"] == {
            "source": "chat",
            "OR": [{"tag": "x"}],
        }
