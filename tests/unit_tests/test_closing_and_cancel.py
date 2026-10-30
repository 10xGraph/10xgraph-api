"""A run refused while the graph closes (BF3), client disconnect (BF6), cancelled run (BF7)."""

# ruff: noqa: S101

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect
from tenxgraph.core.graph import StateGraph
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils.constants import END

from tenxgraph_api.src.app.core.concurrency import MemoryConcurrencyLimiter
from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.routers.graph import run_limits, thread_lock
from tenxgraph_api.src.app.routers.graph.router import (
    WS_SERVICE_RESTART,
    invoke_graph,
    stream_graph,
    websocket_graph,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService


ALICE = {"user_id": "alice"}
MSG = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


@pytest.fixture(autouse=True)
def limiters(monkeypatch):
    locks, slots = MemoryConcurrencyLimiter(), MemoryConcurrencyLimiter()
    monkeypatch.setattr(thread_lock, "concurrency_limiter", lambda: locks)
    monkeypatch.setattr(run_limits, "concurrency_limiter", lambda: slots)
    monkeypatch.setattr(thread_lock, "THREAD_BUSY_WAIT_SECONDS", 0.3)
    return SimpleNamespace(locks=locks, slots=slots)


def _input(thread_id="t1"):
    return GraphInputSchema(messages=MSG, config={"thread_id": thread_id})


def _graph(node):
    g = StateGraph()
    g.add_node("A", node)
    g.add_edge("A", END)
    g.set_entry_point("A")
    return g.compile(checkpointer=InMemoryCheckpointer())


def _service(graph) -> GraphService:
    service = GraphService.__new__(GraphService)
    service._graph = graph
    service.config = SimpleNamespace(runs=RunsConfig(max_concurrent_per_user=1))
    service.config.thread_name_generator_path = None
    service.checkpointer = graph._checkpointer
    service.thread_name_generator = None
    service._media_service = None
    service._telemetry = None
    return service


def _quick(state: AgentState, config: dict) -> Message:
    return Message.text_message("ok", role="assistant")


async def _closed_service() -> GraphService:
    graph = _graph(_quick)
    await graph.aclose()
    return _service(graph)


def _ws(frames):
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(
        side_effect=[json.dumps(f) if isinstance(f, dict) else f for f in frames]
    )
    return ws


# ---------------------------------------------------------------------------
# BF3: GraphClosedError after the stream headers went out, and on a WebSocket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closing_graph_ends_the_ndjson_stream_with_a_terminal_error_chunk(limiters):
    service = await _closed_service()
    # The route returns the response first (headers are sent); the refusal comes while streaming.
    response = await stream_graph(graph_input=_input(), service=service, user=ALICE)
    lines = [json.loads(line) async for line in response.body_iterator if line.strip()]
    assert len(lines) == 1
    assert lines[0]["event"] == "error"
    assert lines[0]["data"]["code"] == "SERVICE_UNAVAILABLE"
    assert lines[0]["data"]["retry_after_seconds"] >= 1
    assert limiters.locks._held == {}
    assert limiters.slots._held == {}


@pytest.mark.asyncio
async def test_closing_graph_sends_an_error_frame_then_closes_the_websocket(limiters):
    service = await _closed_service()
    frame = {"invoke_type": "fresh", "messages": MSG, "config": {"thread_id": "t1"}}
    ws = _ws([frame, WebSocketDisconnect()])
    await websocket_graph(websocket=ws, service=service, user={})
    sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
    assert [c["event"] for c in sent] == ["error"]  # no "done" frame
    assert sent[0]["data"]["code"] == "SERVICE_UNAVAILABLE"
    ws.close.assert_awaited_once_with(code=WS_SERVICE_RESTART)
    assert limiters.locks._held == {}
    assert limiters.slots._held == {}


# ---------------------------------------------------------------------------
# BF6 and BF7: a client that leaves, or a cancelled request, stops the run
# ---------------------------------------------------------------------------


def _blocking_node():
    probe = SimpleNamespace(started=asyncio.Event(), cancelled=False, block=True)

    async def node(state: AgentState, config: dict) -> Message:
        if probe.block:
            probe.started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                probe.cancelled = True
                raise
        return Message.text_message("ok", role="assistant")

    return node, probe


async def _next_run_works(service) -> None:
    """A fresh run on the same thread starts and ends: the thread is not stuck."""
    response = await stream_graph(graph_input=_input(), service=service, user=ALICE)
    lines = [json.loads(line) async for line in response.body_iterator if line.strip()]
    assert lines[-1]["data"] == {"status": "done"}


@pytest.mark.asyncio
async def test_client_disconnect_during_a_stream_cancels_the_run_and_frees_the_thread(limiters):
    node, probe = _blocking_node()
    service = _service(_graph(node))
    response = await stream_graph(graph_input=_input(), service=service, user=ALICE)
    body = response.body_iterator

    await body.__anext__()  # the client got the first chunk, then leaves
    assert limiters.locks.held(thread_lock.thread_lock_key("t1")) == 1
    await body.aclose()  # what Starlette does when the client goes away

    assert not probe.started.is_set() or probe.cancelled  # the run did not carry on
    assert limiters.locks._held == {}
    assert limiters.slots._held == {}
    probe.block = False
    await _next_run_works(service)


@pytest.mark.asyncio
async def test_cancelled_stream_request_releases_thread_lock_and_run_slot(limiters):
    node, probe = _blocking_node()
    service = _service(_graph(node))
    response = await stream_graph(graph_input=_input(), service=service, user=ALICE)

    async def read():
        async for _ in response.body_iterator:
            pass

    reader = asyncio.create_task(read())
    await asyncio.wait_for(probe.started.wait(), 5)
    assert limiters.slots.held(run_limits.run_slot_key("alice")) == 1
    reader.cancel()  # the server cancels the request task
    await asyncio.gather(reader, return_exceptions=True)
    await asyncio.sleep(0)

    assert probe.cancelled
    assert limiters.locks._held == {}
    assert limiters.slots._held == {}
    probe.block = False
    await _next_run_works(service)


@pytest.mark.asyncio
async def test_cancelled_invoke_request_releases_thread_lock_and_run_slot(limiters):
    node, probe = _blocking_node()
    service = _service(_graph(node))
    request = MagicMock()
    request.state = SimpleNamespace(request_id="r", timestamp="t")
    request.is_disconnected = AsyncMock(return_value=False)

    task = asyncio.create_task(
        invoke_graph(request=request, graph_input=_input(), service=service, user=ALICE)
    )
    await asyncio.wait_for(probe.started.wait(), 5)
    assert limiters.locks.held(thread_lock.thread_lock_key("t1")) == 1
    assert limiters.slots.held(run_limits.run_slot_key("alice")) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert probe.cancelled
    assert limiters.locks._held == {}
    assert limiters.slots._held == {}
    probe.block = False
    await _next_run_works(service)
