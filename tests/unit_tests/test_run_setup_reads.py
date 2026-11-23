"""One run setup for every transport: each check once, at most one state read before the run."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core.utils.container import lookup
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services import run_checks
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService, RunChecks
from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore


USER = Identity(user_id="u1")


@pytest.fixture(autouse=True)
def _no_telemetry(monkeypatch):
    monkeypatch.setattr(GraphService, "telemetry", None)


@pytest.fixture
def paused(monkeypatch):
    """The thread is paused at interrupt ``i-1`` (an ``interrupt()`` pause, not a static one)."""
    monkeypatch.setattr(run_checks, "pending_interrupt", lambda state: SimpleNamespace(id="i-1"))


def _service() -> tuple[GraphService, list[int]]:
    """A GraphService over stubs; the list gets the state-read count when the run starts."""
    srv = GraphService.__new__(GraphService)
    srv._graph = MagicMock()
    srv.checkpointer = MagicMock()
    srv.checkpointer.aget_state = AsyncMock(return_value=MagicMock())
    srv.config = MagicMock()
    srv.thread_name_generator = None
    srv._media_service = None
    srv._telemetry = None
    srv._save_thread = AsyncMock(return_value=False)
    reads_at_start: list[int] = []

    async def astream(*args, **kwargs):
        reads_at_start.append(srv.checkpointer.aget_state.await_count)
        return
        yield

    async def ainvoke(*args, **kwargs):
        reads_at_start.append(srv.checkpointer.aget_state.await_count)
        return {"messages": [], "state": None}

    srv._astream_bounded = astream
    srv._ainvoke_bounded = ainvoke
    return srv, reads_at_start


def _resume() -> GraphInputSchema:
    return GraphInputSchema(resume={"ok": True}, interrupt_id="i-1", config={"thread_id": "t1"})


@pytest.mark.asyncio
async def test_stream_resume_reads_the_state_once_before_the_run(paused):
    srv, reads_at_start = _service()
    graph_input = _resume()
    # What the stream router does: input checks, then the 409 check under the thread lock.
    checks = srv.check_run_input(graph_input.messages, graph_input.config)
    await srv.check_resume_target(graph_input, USER, checks)
    [_ async for _ in srv.stream_chunks(graph_input, USER, checks=checks)]
    # Router 409 check, static-pause check and the run's resume check share one read.
    assert reads_at_start == [1]


@pytest.mark.asyncio
async def test_invoke_resume_reads_the_state_once_before_the_run(paused):
    srv, reads_at_start = _service()
    await srv.invoke_graph(_resume(), USER)
    assert reads_at_start == [1]


@pytest.mark.asyncio
async def test_a_state_read_by_the_caller_is_not_read_again(paused):
    srv, reads_at_start = _service()
    # AG-UI reads the checkpoint itself (under the lock) and hands it over.
    checks = RunChecks(state=MagicMock())
    [_ async for _ in srv.stream_chunks(_resume(), USER, checks=checks)]
    assert reads_at_start == [0]


@pytest.mark.asyncio
async def test_router_checked_input_is_not_checked_again():
    srv, _ = _service()
    srv.check_media_urls = MagicMock()
    srv.check_model_choice = MagicMock()
    graph_input = GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        config={"thread_id": "t1"},
    )
    checks = srv.check_run_input(graph_input.messages, graph_input.config)
    await srv.invoke_graph(graph_input, USER, checks=checks)
    srv.check_media_urls.assert_called_once()
    srv.check_model_choice.assert_called_once()

    # Without the router's result (a direct service call) the run checks itself.
    await srv.invoke_graph(graph_input, USER)
    assert srv.check_media_urls.call_count == 2
    assert srv.check_model_choice.call_count == 2


def test_an_embedded_app_on_its_own_container_finds_its_services():
    """Optional services come from the app's container, not the global one (HTTP and WS)."""
    own = InjectQ()
    store = TelemetryStore()
    own.bind_instance(TelemetryStore, store)
    assert lookup(TelemetryStore) is not store  # outside a request: the global container

    app = FastAPI()
    setup_fastapi(own, app)

    @app.get("/check")
    async def check() -> dict:
        return {"own": lookup(TelemetryStore) is store}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"own": lookup(TelemetryStore) is store})
        await websocket.close()

    client = TestClient(app)
    assert client.get("/check").json() == {"own": True}
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json() == {"own": True}
