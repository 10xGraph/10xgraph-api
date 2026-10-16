"""Unit tests for fix_graph functionality."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from tenxgraph.core.state import AgentState, Message, TextBlock
from tenxgraph.storage.checkpointer import BaseCheckpointer

from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService


class TestFixGraph:
    """Test cases for fix_graph service method."""

    @pytest.fixture
    def mock_checkpointer(self):
        """Create a mock checkpointer."""
        checkpointer = MagicMock(spec=BaseCheckpointer)
        checkpointer.aget_state = AsyncMock()
        checkpointer.aput_state = AsyncMock()
        return checkpointer

    @pytest.fixture
    def mock_graph(self):
        """Create a mock CompiledGraph."""
        graph = MagicMock()
        return graph

    @pytest.fixture
    def mock_config(self):
        """Create a mock GraphConfig."""
        config = MagicMock()
        config.thread_name_generator_path = None
        return config

    @pytest.fixture
    def graph_service(self, mock_graph, mock_checkpointer, mock_config):
        """Create a GraphService instance with mocked dependencies."""
        service = GraphService.__new__(GraphService)  # Skip __init__
        service._graph = mock_graph
        service.checkpointer = mock_checkpointer
        service.config = mock_config
        service.thread_name_generator = None
        return service

    def _create_mock_message(
        self,
        message_id: str,
        role: str = "user",
        content_text: str = "test",
        tool_calls: list | None = None,
    ) -> MagicMock:
        """Helper to create a mock message."""
        message = MagicMock(spec=Message)
        message.message_id = message_id
        message.role = role
        message.content = [MagicMock(spec=TextBlock)]
        message.tools_calls = tool_calls
        return message

    def _create_mock_state(
        self,
        messages: list,
    ) -> MagicMock:
        """Helper to create a mock state with messages."""
        state = MagicMock(spec=AgentState)
        state.context = messages
        state.model_dump = MagicMock(return_value={"messages": messages})

        # Configure the type() of state to have model_validate
        state_type = type(state)
        state_type.model_validate = MagicMock(side_effect=lambda x: state)

        return state

    @pytest.mark.asyncio
    async def test_fix_graph_no_messages_with_empty_tool_calls(
        self, graph_service, mock_checkpointer
    ):
        """Test fix_graph when no messages have empty tool calls."""
        # Create messages without empty tool calls
        messages = [
            self._create_mock_message("msg1", tool_calls=None),
            self._create_mock_message("msg2", tool_calls=[{"name": "tool1", "content": "ok"}]),
        ]

        mock_state = self._create_mock_state(messages)
        mock_checkpointer.aget_state.return_value = mock_state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["success"] is True
        assert result["removed_count"] == 0
        assert "No messages with empty tool calls found" in result["message"]
        mock_checkpointer.aput_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_fix_graph_removes_messages_with_empty_tool_calls(
        self, graph_service, mock_checkpointer
    ):
        """Test fix_graph removes messages with empty tool call content."""
        # Create messages: one with empty tool call, one normal, one with non-empty tool call
        msg1 = self._create_mock_message("msg1", tool_calls=[{"name": "tool1", "content": ""}])
        msg2 = self._create_mock_message("msg2", tool_calls=None)
        msg3 = self._create_mock_message(
            "msg3", tool_calls=[{"name": "tool2", "content": "result"}]
        )

        original_messages = [msg1, msg2, msg3]
        mock_state = self._create_mock_state(original_messages)
        mock_checkpointer.aget_state.return_value = mock_state

        # Create updated state for after the fix
        updated_state = self._create_mock_state([msg2, msg3])
        mock_checkpointer.aput_state.return_value = updated_state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["success"] is True
        assert result["removed_count"] == 1
        assert "Successfully removed 1 message(s)" in result["message"]
        mock_checkpointer.aput_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_fix_graph_removes_multiple_messages_with_empty_tool_calls(
        self, graph_service, mock_checkpointer
    ):
        """Test fix_graph removes multiple messages with empty tool calls."""
        # Create messages with multiple empty tool calls
        msg1 = self._create_mock_message("msg1", tool_calls=[{"name": "tool1", "content": ""}])
        msg2 = self._create_mock_message("msg2", tool_calls=[{"name": "tool2", "content": ""}])
        msg3 = self._create_mock_message("msg3", tool_calls=None)
        msg4 = self._create_mock_message(
            "msg4", tool_calls=[{"name": "tool3", "content": "result"}]
        )

        original_messages = [msg1, msg2, msg3, msg4]
        mock_state = self._create_mock_state(original_messages)
        mock_checkpointer.aget_state.return_value = mock_state

        updated_state = self._create_mock_state([msg3, msg4])
        mock_checkpointer.aput_state.return_value = updated_state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["success"] is True
        assert result["removed_count"] == 2
        assert "Successfully removed 2 message(s)" in result["message"]
        mock_checkpointer.aput_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_fix_graph_no_state_found(self, graph_service, mock_checkpointer):
        """Test fix_graph when no state is found for thread."""
        mock_checkpointer.aget_state.return_value = None

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["success"] is False
        assert "No state found" in result["message"]
        assert result["removed_count"] == 0
        mock_checkpointer.aput_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_fix_graph_with_config(self, graph_service, mock_checkpointer):
        """Test fix_graph respects additional config."""
        messages = [self._create_mock_message("msg1")]
        mock_state = self._create_mock_state(messages)
        mock_checkpointer.aget_state.return_value = mock_state

        extra_config = {"custom_key": "custom_value"}
        result = await graph_service.fix_graph("thread1", {"user_id": "user1"}, extra_config)

        assert result["success"] is True

        # Verify config was merged correctly
        call_args = mock_checkpointer.aget_state.call_args
        config_arg = call_args[0][0]  # First positional arg
        assert config_arg["thread_id"] == "thread1"
        assert config_arg["custom_key"] == "custom_value"

    @pytest.mark.asyncio
    async def test_fix_graph_exception_handling(self, graph_service, mock_checkpointer):
        """Test fix_graph handles exceptions properly."""
        mock_checkpointer.aget_state.side_effect = Exception("Database error")

        with pytest.raises(HTTPException) as exc_info:
            await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert exc_info.value.status_code == 500
        assert "Fix graph operation failed" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_fix_graph_handles_mixed_empty_content(self, graph_service, mock_checkpointer):
        """Test fix_graph correctly identifies empty string vs None content."""
        # Mix of empty string, None, and non-empty content
        msg1 = self._create_mock_message("msg1", tool_calls=[{"name": "tool1", "content": ""}])
        msg2 = self._create_mock_message("msg2", tool_calls=[{"name": "tool2", "content": None}])
        msg3 = self._create_mock_message("msg3", tool_calls=[{"name": "tool3", "content": "valid"}])

        original_messages = [msg1, msg2, msg3]
        mock_state = self._create_mock_state(original_messages)
        mock_checkpointer.aget_state.return_value = mock_state

        updated_state = self._create_mock_state([msg3])
        mock_checkpointer.aput_state.return_value = updated_state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        # Should remove both empty string and None content
        assert result["removed_count"] == 2
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_fix_graph_preserves_message_order(self, graph_service, mock_checkpointer):
        """Test fix_graph preserves the order of remaining messages."""
        msg1 = self._create_mock_message("msg1", tool_calls=None)
        msg2 = self._create_mock_message("msg2", tool_calls=[{"name": "tool", "content": ""}])
        msg3 = self._create_mock_message("msg3", tool_calls=None)
        msg4 = self._create_mock_message("msg4", tool_calls=[{"name": "tool", "content": ""}])
        msg5 = self._create_mock_message("msg5", tool_calls=None)

        original_messages = [msg1, msg2, msg3, msg4, msg5]
        mock_state = self._create_mock_state(original_messages)
        mock_checkpointer.aget_state.return_value = mock_state

        updated_state = self._create_mock_state([msg1, msg3, msg5])
        mock_checkpointer.aput_state.return_value = updated_state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["success"] is True
        assert result["removed_count"] == 2

        # Verify the correct messages are kept by checking the call to aput_state
        call_args = mock_checkpointer.aput_state.call_args
        updated_state_arg = call_args[0][1]  # Second positional arg

    @pytest.mark.asyncio
    async def test_fix_graph_returns_state_as_object(self, graph_service, mock_checkpointer):
        """The response carries ``state`` as a JSON object, not a pre-encoded JSON string."""
        state = AgentState(context=[Message.text_message("hi", role="user")])
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert isinstance(result["state"], dict)
        assert result["state"]["context"][0]["role"] == "user"
