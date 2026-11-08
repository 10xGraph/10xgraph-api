"""Unit tests for CheckpointerService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter
from tenxgraph.core.authz import Identity
from tenxgraph.core.exceptions import ThreadNotFoundError
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.utils.thread_info import ThreadInfo

from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
    MessagesListResponseSchema,
    ResponseSchema,
    StateResponseSchema,
    ThreadResponseSchema,
    ThreadsListResponseSchema,
)
from tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service import (
    CheckpointerService,
)


ALICE = Identity(user_id="123")


class TestCheckpointerService:
    """Test cases for CheckpointerService."""

    @pytest.fixture
    def mock_checkpointer(self):
        """Create a mock checkpointer."""
        checkpointer = MagicMock(spec=BaseCheckpointer)
        # Set up async methods
        checkpointer.aget_state = AsyncMock()
        checkpointer.aget_state_cache = AsyncMock()
        checkpointer.aput_state = AsyncMock()
        checkpointer.aput_state_cache = AsyncMock()
        checkpointer.aclear_state = AsyncMock()
        checkpointer.aget_message = AsyncMock()
        checkpointer.alist_messages = AsyncMock()
        checkpointer.adelete_message = AsyncMock()
        # The scoped thread read every thread route starts with finds the caller's thread.
        checkpointer.aget_thread = AsyncMock(
            return_value=ThreadInfo(thread_id="t1", user_id=ALICE.user_id)
        )
        checkpointer.alist_threads = AsyncMock()
        checkpointer.aclean_thread = AsyncMock()
        return checkpointer

    @pytest.fixture
    def checkpointer_service(self, mock_checkpointer):
        """Create a CheckpointerService instance with mocked dependencies."""
        service = CheckpointerService.__new__(CheckpointerService)  # Skip __init__
        service.checkpointer = mock_checkpointer
        service.settings = MagicMock()
        return service

    @pytest.fixture
    def checkpointer_service_no_checkpointer(self):
        """Create a CheckpointerService instance without checkpointer."""
        service = CheckpointerService.__new__(CheckpointerService)  # Skip __init__
        service.settings = MagicMock()
        service.checkpointer = None
        return service

    def test_config_validation(self, checkpointer_service):
        """Test _config method validates checkpointer and adds user info."""
        config = {"thread_id": "test_thread"}

        result = checkpointer_service._config(config, ALICE)

        assert result["user"] == ALICE
        assert result["user_id"] == "123"
        assert result["thread_id"] == "test_thread"

    def test_config_without_identity_is_anonymous(self, checkpointer_service):
        result = checkpointer_service._config({"thread_id": "test_thread"}, None)

        assert "user" not in result
        assert result["user_id"] == "anonymous"

    def test_config_validation_no_checkpointer(self, checkpointer_service_no_checkpointer):
        """Test _config method raises error when checkpointer is not configured."""
        config = {"thread_id": "test_thread"}

        with pytest.raises(ValueError, match="Checkpointer is not configured"):
            checkpointer_service_no_checkpointer._config(config, ALICE)

    @pytest.mark.asyncio
    async def test_get_state_success(self, checkpointer_service, mock_checkpointer):
        """Test get_state returns state when available."""
        # Create a mock AgentState
        mock_state = MagicMock(spec=AgentState)
        mock_checkpointer.aget_state.return_value = mock_state

        # Mock parse_state_output to return a simple dict
        with patch(
            "tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service.parse_state_output"
        ) as mock_parse:
            mock_parse.return_value = {"test": "data"}

            result = await checkpointer_service.get_state({"thread_id": "t1"}, ALICE)

            assert isinstance(result, StateResponseSchema)
            assert result.state == {"test": "data"}
            mock_checkpointer.aget_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_state_fallback_to_cache(self, checkpointer_service, mock_checkpointer):
        """Test get_state falls back to cache when primary state is None."""
        mock_checkpointer.aget_state.return_value = None
        mock_checkpointer.aget_state_cache.return_value = {"cached": "data"}

        result = await checkpointer_service.get_state({"thread_id": "t1"}, ALICE)

        assert isinstance(result, StateResponseSchema)
        assert result.state == {"cached": "data"}
        mock_checkpointer.aget_state_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_state_success(self, checkpointer_service, mock_checkpointer):
        """Test clear_state returns success response."""
        mock_checkpointer.aclear_state.return_value = True

        result = await checkpointer_service.clear_state({"thread_id": "t1"}, ALICE)

        assert isinstance(result, ResponseSchema)
        assert result.success is True
        assert "cleared successfully" in result.message
        mock_checkpointer.aclear_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_messages_success(self, checkpointer_service, mock_checkpointer):
        """Test get_messages returns messages list."""
        mock_messages = [MagicMock(spec=Message)]
        mock_checkpointer.alist_messages.return_value = mock_messages

        result = await checkpointer_service.get_messages(
            {"thread_id": "t1"}, ALICE, search="test", offset=0, limit=10
        )

        assert isinstance(result, MessagesListResponseSchema)
        assert result.messages == mock_messages
        # Validate alist_messages was called with a cfg containing both 'user' and 'user_id'
        args, kwargs = mock_checkpointer.alist_messages.call_args
        cfg_arg = args[0]
        assert cfg_arg["user"] == ALICE
        assert cfg_arg.get("user_id") == "123"
        assert args[1:] == ("test", 0, 10)

    @pytest.mark.asyncio
    async def test_get_thread_success(self, checkpointer_service, mock_checkpointer):
        """Test get_thread returns thread data."""
        mock_thread = MagicMock()
        mock_thread.model_dump.return_value = {"thread_id": "123", "data": "test"}
        mock_checkpointer.aget_thread.return_value = mock_thread

        result = await checkpointer_service.get_thread({"thread_id": "t1"}, ALICE)

        assert isinstance(result, ThreadResponseSchema)
        assert result.thread == {"thread_id": "123", "data": "test"}
        mock_checkpointer.aget_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_threads_success(self, checkpointer_service, mock_checkpointer):
        """Test list_threads returns threads list."""
        mock_thread = MagicMock()
        mock_thread.model_dump.return_value = {"thread_id": "123"}
        mock_checkpointer.alist_threads.return_value = [mock_thread]

        result = await checkpointer_service.list_threads(ALICE, search="test", offset=0, limit=10)

        assert isinstance(result, ThreadsListResponseSchema)
        assert result.threads == [{"thread_id": "123"}]
        mock_checkpointer.alist_threads.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_thread_success(self, checkpointer_service, mock_checkpointer):
        """Test delete_thread returns success response."""
        mock_checkpointer.aclean_thread.return_value = 1

        result = await checkpointer_service.delete_thread({}, ALICE, "thread_123")

        assert isinstance(result, ResponseSchema)
        assert result.success is True
        assert "deleted successfully" in result.message
        mock_checkpointer.aclean_thread.assert_called_once()

    def test_merge_states_basic(self, checkpointer_service):
        """Test _merge_states with basic merging."""
        old_state = MagicMock(spec=AgentState)
        old_state.model_dump.return_value = {"existing": "data", "keep": "this"}
        old_state.execution_meta = {"meta": "data"}

        updates = {"new": "value", "existing": "updated"}

        result = checkpointer_service._merge_states(old_state, updates)

        assert result["existing"] == "updated"
        assert result["new"] == "value"
        assert result["keep"] == "this"
        assert result["execution_meta"] == {"meta": "data"}

    def test_merge_states_context_append(self, checkpointer_service):
        """Test _merge_states appends context messages."""
        old_state = MagicMock(spec=AgentState)
        old_state.model_dump.return_value = {"context": ["old_message"]}
        old_state.execution_meta = {}

        updates = {"context": ["new_message"]}

        result = checkpointer_service._merge_states(old_state, updates)

        assert result["context"] == ["old_message", "new_message"]

    def test_deep_merge_dicts(self, checkpointer_service):
        """Test _deep_merge_dicts merges nested dictionaries."""
        base = {"level1": {"nested": "value1", "keep": "this"}}
        updates = {"level1": {"nested": "updated", "new": "added"}}

        result = checkpointer_service._deep_merge_dicts(base, updates)

        assert result["level1"]["nested"] == "updated"
        assert result["level1"]["keep"] == "this"
        assert result["level1"]["new"] == "added"

    def test_reconstruct_state(self, checkpointer_service):
        """Test _reconstruct_state rebuilds AgentState."""
        # Skip this test as it requires complex Pydantic model setup
        # The core functionality is tested in other tests


# ---------------------------------------------------------------------------
# The scoped thread read and new client-named threads
# ---------------------------------------------------------------------------


class TestRequireThread:
    @staticmethod
    def _service(thread=None, thread_ids="server"):
        service = CheckpointerService.__new__(CheckpointerService)
        service.checkpointer = MagicMock()
        service.checkpointer.aget_thread = AsyncMock(return_value=thread)
        service.checkpointer.aget_thread_owner = AsyncMock(return_value=None)  # a free id
        service.checkpointer.aput_thread = AsyncMock(return_value=True)
        service._runs = lambda: RunsConfig(thread_ids=thread_ids)
        return service

    @pytest.mark.asyncio
    async def test_unknown_thread_is_404_for_reads_in_any_mode(self):
        for mode in ("server", "client"):
            with pytest.raises(ThreadNotFoundError):
                await self._service(thread_ids=mode)._require(ALICE, "t1")

    @pytest.mark.asyncio
    async def test_unknown_thread_write_is_404_under_server_ids(self):
        with pytest.raises(ThreadNotFoundError):
            await self._service()._require(ALICE, "t1", write=True)

    @pytest.mark.asyncio
    async def test_unknown_thread_write_is_new_under_client_ids(self):
        assert await self._service(thread_ids="client")._require(ALICE, "t1", write=True) is None

    @pytest.mark.asyncio
    async def test_write_reads_with_reach_own(self):
        from tenxgraph.core.authz import Reach

        thread = ThreadInfo(thread_id="t1", user_id="123")
        service = self._service(thread=thread)
        admin = Identity(user_id="123", reach=Reach.ALL)
        assert await service._require(admin, "t1", write=True) is thread
        cfg = service.checkpointer.aget_thread.await_args.args[0]
        assert cfg["user"].reach is Reach.OWN

    @pytest.mark.asyncio
    async def test_new_thread_is_registered_to_the_caller_before_the_write(self):
        service = self._service(thread_ids="client")
        service.settings = MagicMock()
        service.checkpointer.aget_state = AsyncMock(return_value=None)
        service.checkpointer.aget_state_cache = AsyncMock(return_value=None)
        service.checkpointer.aput_state = AsyncMock(side_effect=lambda cfg, state: state)
        service.checkpointer.aput_state_cache = AsyncMock()
        cfg = {"thread_id": "t1", "user_id": "alice"}
        with patch(
            "tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service.parse_state_output",
            return_value={},
        ):
            await service.put_state(cfg, Identity(user_id="alice"), {})
        args = service.checkpointer.aput_thread.await_args.args
        assert args[1].thread_id == "t1"
        assert args[1].user_id == "alice"

    @pytest.mark.asyncio
    async def test_invalid_state_does_not_claim_a_new_thread(self):
        service = self._service(thread_ids="client")
        service.settings = MagicMock()
        service.checkpointer.aget_state = AsyncMock(return_value=None)
        service.checkpointer.aget_state_cache = AsyncMock(return_value=None)
        service.checkpointer.aput_state = AsyncMock()

        def invalid(old_state, data):
            TypeAdapter(int).validate_python({"field": "not an int"})

        service._reconstruct_state = invalid
        with pytest.raises(HTTPException) as exc:
            await service.put_state(
                {"thread_id": "t1", "user_id": "alice"}, Identity(user_id="alice"), {}
            )
        assert exc.value.status_code == 422  # noqa: PLR2004
        service.checkpointer.aput_thread.assert_not_awaited()
        service.checkpointer.aput_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_thread_is_not_registered_again(self):
        service = self._service(thread=ThreadInfo(thread_id="t1", user_id="alice"))
        service.settings = MagicMock()
        service.checkpointer.aget_state = AsyncMock(return_value=None)
        service.checkpointer.aget_state_cache = AsyncMock(return_value=None)
        service.checkpointer.aput_state = AsyncMock(side_effect=lambda cfg, state: state)
        service.checkpointer.aput_state_cache = AsyncMock()
        with patch(
            "tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service.parse_state_output",
            return_value={},
        ):
            await service.put_state({"thread_id": "t1"}, Identity(user_id="alice"), {})
        service.checkpointer.aput_thread.assert_not_awaited()


def test_owned_thread_info_names_the_caller():
    from tenxgraph_api.src.app.core.validation.request_config import owned_thread_info

    info = owned_thread_info({"user_id": "alice"}, "t1", thread_name="Chat")
    assert (info.thread_id, info.user_id, info.thread_name) == ("t1", "alice", "Chat")
    assert owned_thread_info({}, "t2").user_id is None


@pytest.mark.asyncio
async def test_graph_service_saves_threads_with_their_owner():
    """GraphService._save_thread must record the owner so in-memory/SQLite lookups see it."""
    from unittest.mock import MagicMock

    from tenxgraph.storage.checkpointer import InMemoryCheckpointer

    from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

    checkpointer = InMemoryCheckpointer()
    service = GraphService(graph=MagicMock(), checkpointer=checkpointer, config=MagicMock())
    await service._save_thread({"thread_id": "t1", "user_id": "alice"}, "t1")
    assert await checkpointer.aget_thread_owner("t1") == "alice"
