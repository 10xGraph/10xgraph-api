"""Unit tests for fix_graph functionality."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from tenxgraph.core.state import (
    AgentState,
    ExecutionStatus,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
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
        return MagicMock()

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

    @staticmethod
    def _call(call_id: str, name: str = "lookup") -> dict:
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}

    @staticmethod
    def _assistant(*call_ids: str) -> Message:
        return Message(
            role="assistant",
            content=[TextBlock(text="calling")],
            tools_calls=[TestFixGraph._call(c) for c in call_ids],
        )

    @staticmethod
    def _result(call_id: str) -> Message:
        return Message.tool_message(
            [ToolResultBlock(call_id=call_id, output="ok", status="completed")]
        )

    @staticmethod
    def _user(text: str = "hi") -> Message:
        return Message.text_message(text, role="user")

    def _roles(self, mock_checkpointer) -> list[str]:
        saved = mock_checkpointer.aput_state.call_args[0][1]
        return [m.role for m in saved.context]

    @pytest.mark.asyncio
    async def test_healthy_tool_call_history_is_untouched(self, graph_service, mock_checkpointer):
        """[user, assistant(tool call), tool, assistant] has nothing to fix."""
        state = AgentState(
            context=[
                self._user(),
                self._assistant("c1"),
                self._result("c1"),
                Message.text_message("done", role="assistant"),
            ]
        )
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["success"] is True
        assert result["removed_count"] == 0
        assert "No unanswered tool calls" in result["message"]
        mock_checkpointer.aput_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_dangling_tool_call_is_removed(self, graph_service, mock_checkpointer):
        state = AgentState(
            context=[
                self._user("a"),
                self._assistant("c1"),
                self._user("b"),
                Message.text_message("answer", role="assistant"),
            ]
        )
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["removed_count"] == 1
        assert "Successfully removed 1 message(s)" in result["message"]
        mock_checkpointer.aput_state.assert_called_once()
        assert self._roles(mock_checkpointer) == ["user", "user", "assistant"]

    @pytest.mark.asyncio
    async def test_partially_answered_message_and_its_result_are_removed(
        self, graph_service, mock_checkpointer
    ):
        state = AgentState(
            context=[
                self._user(),
                self._assistant("c1", "c2"),
                self._result("c1"),
                Message.text_message("later", role="assistant"),
            ]
        )
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["removed_count"] == 2
        assert self._roles(mock_checkpointer) == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_orphan_result_without_any_call_is_removed(
        self, graph_service, mock_checkpointer
    ):
        state = AgentState(
            context=[
                self._user(),
                self._result("ghost"),
                Message.text_message("x", role="assistant"),
            ]
        )
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["removed_count"] == 1
        assert self._roles(mock_checkpointer) == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_tool_call_content_blocks_are_handled(self, graph_service, mock_checkpointer):
        answered = Message(
            role="assistant", content=[ToolCallBlock(id="b1", name="lookup", args={})]
        )
        dangling = Message(
            role="assistant", content=[ToolCallBlock(id="b2", name="lookup", args={})]
        )
        state = AgentState(
            context=[
                self._user(),
                answered,
                self._result("b1"),
                dangling,
                Message.text_message("end", role="assistant"),
            ]
        )
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["removed_count"] == 1
        assert self._roles(mock_checkpointer) == ["user", "assistant", "tool", "assistant"]

    @pytest.mark.asyncio
    async def test_paused_thread_keeps_pending_trailing_call(
        self, graph_service, mock_checkpointer
    ):
        state = AgentState(context=[self._user(), self._assistant("c1")])
        state.execution_meta.set_interrupt(
            "TOOL", "interrupt_before", ExecutionStatus.INTERRUPTED_BEFORE
        )
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["removed_count"] == 0
        mock_checkpointer.aput_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_paused_thread_still_removes_earlier_dangling_call(
        self, graph_service, mock_checkpointer
    ):
        state = AgentState(
            context=[
                self._user("a"),
                self._assistant("old"),
                self._user("b"),
                self._assistant("c1"),
            ]
        )
        state.execution_meta.set_interrupt("TOOL", "interrupt", ExecutionStatus.INTERRUPTED_AFTER)
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["removed_count"] == 1
        assert self._roles(mock_checkpointer) == ["user", "user", "assistant"]
        saved = mock_checkpointer.aput_state.call_args[0][1]
        assert saved.context[-1].tools_calls[0]["id"] == "c1"

    @pytest.mark.asyncio
    async def test_pending_remote_tool_call_is_kept(
        self, graph_service, mock_checkpointer, monkeypatch
    ):
        """A client tool call awaiting its result is pending work even without an interrupt."""
        monkeypatch.setattr(graph_service, "_remote_tool_names", lambda: {"lookup"})
        state = AgentState(context=[self._user(), self._assistant("c1")])
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["removed_count"] == 0
        mock_checkpointer.aput_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_unpaused_trailing_server_call_is_removed(self, graph_service, mock_checkpointer):
        state = AgentState(context=[self._user(), self._assistant("c1")])
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert result["removed_count"] == 1
        assert self._roles(mock_checkpointer) == ["user"]

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
    async def test_fix_graph_returns_state_as_object(self, graph_service, mock_checkpointer):
        """The response carries ``state`` as a JSON object, not a pre-encoded JSON string."""
        state = AgentState(context=[Message.text_message("hi", role="user")])
        mock_checkpointer.aget_state.return_value = state

        result = await graph_service.fix_graph("thread1", {"user_id": "user1"})

        assert isinstance(result["state"], dict)
        assert result["state"]["context"][0]["role"] == "user"
