import asyncio


"""Unit tests for GraphService."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import BaseModel
from tenxgraph.core.exceptions.media_exceptions import UnsupportedMediaInputError
from tenxgraph.core.state import AgentState, Message, StreamChunk, StreamEvent
from tenxgraph.storage.checkpointer import BaseCheckpointer

from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.utils.thread_name_generator import ThreadNameGenerator


class MockStateModel(BaseModel):
    context: list = []
    context_summary: str = ""


class TestGraphServiceMethods:
    """Test cases for GraphService methods."""

    @pytest.fixture
    def mock_graph(self):
        graph = MagicMock()
        graph.ainvoke = AsyncMock()
        graph.astream = MagicMock()  # Will be configured per test
        graph.astop = AsyncMock()
        graph.generate_graph = MagicMock()

        # for get_state_schema
        class FakeState(BaseModel):
            a: int

        graph._state = FakeState
        return graph

    @pytest.fixture
    def mock_checkpointer(self):
        checkpointer = MagicMock(spec=BaseCheckpointer)
        checkpointer.aput_thread = AsyncMock(return_value=True)
        checkpointer.aget_thread_owner = AsyncMock(return_value=None)
        return checkpointer

    @pytest.fixture
    def mock_config(self):
        config = MagicMock(spec=GraphConfig)
        config.thread_name_generator_path = None
        return config

    @pytest.fixture
    def mock_thread_name_generator(self):
        generator = MagicMock(spec=ThreadNameGenerator)
        generator.generate_name = AsyncMock(return_value="custom_thread_name")
        return generator

    @pytest.fixture
    def service(self, mock_graph, mock_checkpointer, mock_config, mock_thread_name_generator):
        srv = GraphService.__new__(GraphService)
        srv._graph = mock_graph
        srv.checkpointer = mock_checkpointer
        srv.config = mock_config
        srv.thread_name_generator = mock_thread_name_generator
        srv._media_service = None
        srv._telemetry = None
        return srv

    def test_media_service_property(self, service):
        # By default InjectQ is not active or try_get fails, so media_service is None
        assert service.media_service is None

        # Mock InjectQ
        mock_container = MagicMock()
        mock_media = MagicMock()
        mock_container.try_get.return_value = mock_media
        with patch("injectq.InjectQ.get_instance", return_value=mock_container):
            # reset property cache
            service._media_service = None
            assert service.media_service == mock_media

    @pytest.mark.asyncio
    async def test_save_thread_name(self, service, mock_checkpointer, mock_thread_name_generator):
        # Generator configured
        name = await service._save_thread_name({"thread_id": "1"}, 1, ["msg"])
        assert name == "custom_thread_name"
        mock_thread_name_generator.generate_name.assert_called_once_with(["msg"])
        mock_checkpointer.aput_thread.assert_called_once()

        # No generator configured
        service.thread_name_generator = None
        mock_checkpointer.aput_thread.reset_mock()
        name = await service._save_thread_name({"thread_id": "1"}, 1, ["msg"])
        assert isinstance(name, str)
        assert len(name) > 0
        mock_checkpointer.aput_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_graph_success(self, service, mock_graph):
        mock_graph.astop.return_value = {"status": "stopped"}
        user = {"user_id": "123"}
        result = await service.stop_graph("thread-123", user, {"extra": "val"})
        assert result == {"status": "stopped"}
        mock_graph.astop.assert_called_once_with(
            {"thread_id": "thread-123", "user": user, "extra": "val", "user_id": "123"}
        )

    @pytest.mark.asyncio
    async def test_stop_graph_validation_error(self, service, mock_graph, monkeypatch):
        from tenxgraph_api.src.app.core.config import settings as settings_mod

        monkeypatch.setattr(
            settings_mod, "get_settings", lambda: type("S", (), {"MODE": "development"})()
        )
        mock_graph.astop.side_effect = ValueError("invalid input")
        with pytest.raises(HTTPException) as exc:
            await service.stop_graph("t", {})
        assert exc.value.status_code == 422
        assert "invalid input" in exc.value.detail

    @pytest.mark.asyncio
    async def test_stop_graph_hides_an_arbitrary_value_error_in_production(
        self, service, mock_graph, monkeypatch
    ):
        """A ValueError from inside the graph may carry internals."""
        from tenxgraph_api.src.app.core.config import settings as settings_mod

        monkeypatch.setattr(
            settings_mod, "get_settings", lambda: type("S", (), {"MODE": "production"})()
        )
        mock_graph.astop.side_effect = ValueError("pg://user:secret@db/internal")
        with pytest.raises(HTTPException) as exc:
            await service.stop_graph("t", {})
        assert exc.value.status_code == 422
        assert exc.value.detail == "Invalid input provided."

    @pytest.mark.asyncio
    async def test_stop_graph_general_error(self, service, mock_graph):
        mock_graph.astop.side_effect = Exception("db crash")
        with pytest.raises(HTTPException) as exc:
            await service.stop_graph("t", {})
        assert exc.value.status_code == 500
        assert "db crash" in exc.value.detail

    @pytest.mark.asyncio
    async def test_prepare_input_sets_server_max_parallel_tool_calls(self, service):
        from tenxgraph_api.src.app.core.config.graph_config import RunsConfig

        gi = GraphInputSchema(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        )
        # Unset: the key is left out so core's default applies
        gi.config = {"thread_id": "t1"}
        _, config, _ = await service._prepare_input(gi)
        assert "max_parallel_tool_calls" not in config

        service.config.runs = RunsConfig(max_parallel_tool_calls=3)
        # A client value is dropped and the server value wins
        gi.config = {"thread_id": "t1", "max_parallel_tool_calls": 999}
        _, config, _ = await service._prepare_input(gi)
        assert config["max_parallel_tool_calls"] == 3

        # A trusted caller's server_config cannot raise it past the operator setting
        _, config, _ = await service._prepare_input(gi, None, {"max_parallel_tool_calls": 50})
        assert config["max_parallel_tool_calls"] == 3

    @pytest.mark.asyncio
    async def test_prepare_input(self, service):
        gi = GraphInputSchema(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            recursion_limit=10,
        )

        # Test with thread_id set
        gi.config = {"thread_id": "t1"}
        input_data, config, meta = await service._prepare_input(gi)
        assert config["thread_id"] == "t1"
        assert config["recursion_limit"] == 10
        assert meta["thread_id"] == "t1"
        assert meta["is_new_thread"] is False
        assert len(input_data["messages"]) == 1

        # Test with thread_id empty (generates one)
        gi.config = {}
        input_data, config, meta = await service._prepare_input(gi)
        assert "thread_id" in config
        assert meta["is_new_thread"] is True

    @pytest.mark.asyncio
    async def test_invoke_graph_success(self, service, mock_graph, mock_config, mock_checkpointer):
        gi = GraphInputSchema(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            recursion_limit=10,
            config={"thread_id": "t1"},
        )
        user = {"user_id": "u1"}

        mock_state = MagicMock(spec=AgentState)
        mock_state.model_dump.return_value = {"key": "val"}
        mock_msg = MagicMock(spec=Message)
        mock_msg.text.return_value = "msg_text"

        mock_graph.ainvoke.return_value = {
            "messages": [mock_msg],
            "state": mock_state,
            "context": [mock_msg],
            "context_summary": "summary",
        }

        # Mock thread_name_generator_path to trigger save_thread_name
        mock_config.thread_name_generator_path = "some_path"

        # Since _save_thread returns True, it's considered a new thread
        mock_checkpointer.aput_thread.return_value = True

        from injectq import InjectQ

        with patch("injectq.InjectQ.get_instance", return_value=InjectQ()):  # no task manager
            result = await service.invoke_graph(gi, user)
        assert result.messages == [mock_msg]
        assert result.summary == "summary"
        # The name is generated in the background, not on the response path.
        assert "thread_name" not in result.meta
        await asyncio.gather(*service._naming_tasks)
        saved = [c.args[1] for c in mock_checkpointer.aput_thread.await_args_list]
        assert any(info.thread_name == "custom_thread_name" for info in saved)

    @pytest.mark.asyncio
    async def test_invoke_graph_errors(self, service, mock_graph):
        gi = GraphInputSchema(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        )

        # UnsupportedMediaInputError
        mock_graph.ainvoke.side_effect = UnsupportedMediaInputError(
            "provider", "model", "media_type", "source_kind"
        )
        with pytest.raises(HTTPException) as exc:
            await service.invoke_graph(gi, {})
        assert exc.value.status_code == 422

        # ValueError
        mock_graph.ainvoke.side_effect = ValueError("bad format")
        with pytest.raises(HTTPException) as exc:
            await service.invoke_graph(gi, {})
        assert exc.value.status_code == 422

        # Exception
        mock_graph.ainvoke.side_effect = Exception("failed")
        with pytest.raises(HTTPException) as exc:
            await service.invoke_graph(gi, {})
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_stream_graph_success(self, service, mock_graph, mock_config):
        gi = GraphInputSchema(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            config={"thread_id": "t1"},
        )

        chunk = StreamChunk(event=StreamEvent.MESSAGE, data={"chunk": "x"})

        # Mock generator to yield chunk
        async def mock_stream(*args, **kwargs):
            yield chunk

        mock_graph.astream = mock_stream

        mock_config.thread_name_generator_path = "some_path"

        chunks = []
        async for c in service.stream_graph(gi, {}):
            chunks.append(c)

        # Naming runs in the background after the run: no extra chunk on the stream, only the
        # terminal "done" update.
        assert len(chunks) == 2

        data0 = json.loads(chunks[0])
        assert data0["event"] == "message"
        assert json.loads(chunks[1])["data"] == {"status": "done"}
        await service.drain_naming_tasks()

    @pytest.mark.asyncio
    async def test_stream_graph_exception_handling(self, service, mock_graph, monkeypatch):
        from tenxgraph_api.src.app.core.config import settings as settings_module

        # The reason is only generic in production; do not depend on which test cached
        # the settings first.
        production = settings_module.Settings(
            MODE="production",
            ALLOWED_HOST="api.example.com",
            ORIGINS="https://app.example.com",
            _env_file=None,
        )
        monkeypatch.setattr(settings_module, "get_settings", lambda: production)
        gi = GraphInputSchema(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            config={"thread_id": "t1"},
        )

        async def mock_stream_error(*args, **kwargs):
            raise Exception("stream crash")
            yield  # make it a generator

        mock_graph.astream = mock_stream_error

        chunks = []
        async for c in service.stream_graph(gi, {}):
            chunks.append(c)

        assert len(chunks) == 1
        data = json.loads(chunks[0])
        assert data["event"] == "error"
        # H2: the raw exception text must not stream verbatim. In production it is
        # replaced with a generic message; the internal detail never leaks.
        assert "stream crash" not in data["data"]["reason"]
        assert data["data"]["reason"]  # some non-empty, sanitized reason is present

    @pytest.mark.asyncio
    async def test_graph_details_success_and_errors(self, service, mock_graph):
        mock_graph.generate_graph.return_value = {
            "info": {
                "node_count": 2,
                "edge_count": 1,
                "checkpointer": False,
                "checkpointer_type": None,
                "publisher": False,
                "store": False,
                "interrupt_before": None,
                "interrupt_after": None,
            },
            "nodes": [],
            "edges": [],
        }
        res = await service.graph_details()
        assert res.info.node_count == 2

        # ValueError
        mock_graph.generate_graph.side_effect = ValueError("invalid format")
        with pytest.raises(HTTPException) as exc:
            await service.graph_details()
        assert exc.value.status_code == 422

        # Exception
        mock_graph.generate_graph.side_effect = Exception("failed")
        with pytest.raises(HTTPException) as exc:
            await service.graph_details()
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_get_state_schema_success_and_errors(self, service, mock_graph):
        res = await service.get_state_schema()
        assert "properties" in res

        # Exception
        del mock_graph._state
        with pytest.raises(HTTPException) as exc:
            await service.get_state_schema()
        assert exc.value.status_code == 500

    def test_extract_context_info(self, service):
        # Case 1: Result has values
        c, s = service._extract_context_info(None, {"context": ["msg"], "context_summary": "sum"})
        assert c == ["msg"]
        assert s == "sum"

        # Case 2: Result doesn't have values, reads from state dict
        c, s = service._extract_context_info({"context": ["msg2"], "context_summary": "sum2"}, {})
        assert c == ["msg2"]
        assert s == "sum2"

        # Case 3: Result doesn't have values, reads from state object
        state_obj = MagicMock()
        state_obj.context = ["msg3"]
        state_obj.context_summary = "sum3"
        c, s = service._extract_context_info(state_obj, {})
        assert c == ["msg3"]
        assert s == "sum3"


@pytest.mark.asyncio
async def test_thread_naming_runs_on_the_background_task_manager():
    """When the core task manager is bound, naming runs there (drained on shutdown)."""
    from injectq import InjectQ
    from tenxgraph.utils.background_task_manager import BackgroundTaskManager

    manager = BackgroundTaskManager()
    container = InjectQ()
    container.bind_instance(BackgroundTaskManager, manager)

    srv = GraphService.__new__(GraphService)
    srv.checkpointer = MagicMock()
    srv.checkpointer.aput_thread = AsyncMock(return_value=True)
    srv.thread_name_generator = MagicMock()
    srv.thread_name_generator.generate_name = AsyncMock(return_value="Weather chat")

    with patch("injectq.InjectQ.get_instance", return_value=container):
        srv._name_thread_in_background({"thread_id": "t1", "user_id": "u1"}, "t1", ["hi"])
    assert manager.pending_count == 1
    await manager.wait_for_all(timeout=5)
    info = srv.checkpointer.aput_thread.await_args.args[1]
    assert (info.thread_name, info.user_id) == ("Weather chat", "u1")


@pytest.mark.asyncio
async def test_thread_naming_failure_is_logged_not_raised(caplog):
    from injectq import InjectQ

    srv = GraphService.__new__(GraphService)
    srv.checkpointer = MagicMock()
    srv.thread_name_generator = MagicMock()
    srv.thread_name_generator.generate_name = AsyncMock(side_effect=RuntimeError("llm down"))
    with patch("injectq.InjectQ.get_instance", return_value=InjectQ()):
        srv._name_thread_in_background({"thread_id": "t1"}, "t1", ["hi"])
    await asyncio.gather(*srv._naming_tasks)
    assert "Thread naming failed for thread t1" in caplog.text
