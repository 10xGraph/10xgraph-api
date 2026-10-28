"""The ``interrupt()`` pause update reaches clients at every granularity (old core included)."""

# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tenxgraph.core.state import AgentState, StreamChunk, StreamEvent
from tenxgraph.utils import ResponseGranularity
from tenxgraph.utils.interrupt import Interrupt, pause_at

from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

from .test_run_limits import ALICE, _service


def _paused_state() -> AgentState:
    state = AgentState()
    pause_at(state, Interrupt(id="i1", key="k", node="ASK", value={"amount": 50}))
    return state


def _stream_without_pause():
    """What core older than the fix yields at low granularity: messages only."""

    async def astream(*args, **kwargs):
        yield StreamChunk(event=StreamEvent.MESSAGE)

    return astream


def _input(granularity):
    return GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        config={"thread_id": "t1"},
        response_granularity=granularity,
    )


async def _run(service, granularity):
    return [c async for c in service.stream_chunks(_input(granularity), ALICE)]


@pytest.mark.parametrize("granularity", [ResponseGranularity.LOW, ResponseGranularity.PARTIAL])
@pytest.mark.asyncio
async def test_pause_rebuilt_from_checkpoint_when_core_dropped_it(granularity):
    graph = MagicMock()
    graph.astream = _stream_without_pause()
    service = _service(graph, timeout=None)
    service.checkpointer.aget_state.return_value = _paused_state()

    chunks = await _run(service, granularity)
    pause = chunks[-1]
    assert pause.event == StreamEvent.UPDATES
    assert pause.data["status"] == "interrupted"
    assert pause.data["node"] == "ASK"
    assert pause.data["interrupt"]["id"] == "i1"
    assert pause.data["interrupt"]["value"] == {"amount": 50}


@pytest.mark.asyncio
async def test_no_duplicate_when_core_already_sent_it():
    sent = StreamChunk(
        event=StreamEvent.UPDATES, data={"status": "interrupted", "node": "ASK", "interrupt": {}}
    )

    async def astream(*args, **kwargs):
        yield sent

    graph = MagicMock()
    graph.astream = astream
    service = _service(graph, timeout=None)
    service.checkpointer.aget_state.return_value = _paused_state()

    chunks = await _run(service, ResponseGranularity.LOW)
    assert len([c for c in chunks if c.data and c.data.get("status") == "interrupted"]) == 1


@pytest.mark.parametrize("granularity", list(ResponseGranularity))
@pytest.mark.asyncio
async def test_finished_run_gets_no_pause_chunk(granularity):
    graph = MagicMock()
    graph.astream = _stream_without_pause()
    service = _service(graph, timeout=None)
    service.checkpointer.aget_state.return_value = AgentState()

    chunks = await _run(service, granularity)
    assert all((c.data or {}).get("status") != "interrupted" for c in chunks)


@pytest.mark.asyncio
async def test_full_granularity_relies_on_core_only():
    graph = MagicMock()
    graph.astream = _stream_without_pause()
    service = _service(graph, timeout=None)
    service.checkpointer.aget_state.return_value = _paused_state()

    chunks = await _run(service, ResponseGranularity.FULL)
    assert len(chunks) == 1
    assert isinstance(service, GraphService)


def _static_state(status, reason):
    from tenxgraph.core.state.execution_state import ExecutionStatus

    state = AgentState()
    state.set_interrupt("N1", reason, ExecutionStatus(status))
    return state


@pytest.mark.parametrize("granularity", [ResponseGranularity.LOW, ResponseGranularity.PARTIAL])
@pytest.mark.parametrize(
    ("status", "reason", "pause", "update_status"),
    [
        ("interrupted_before", "interrupt_before: N1", "before", "invoking_node"),
        ("interrupted_after", "interrupt_after: N1", "after", "node_invoked"),
        ("interrupted_after", "remote_tool_call", "remote_tool", "node_invoked"),
    ],
)
@pytest.mark.asyncio
async def test_static_pauses_rebuilt_from_checkpoint(
    granularity, status, reason, pause, update_status
):
    graph = MagicMock()
    graph.astream = _stream_without_pause()
    service = _service(graph, timeout=None)
    service.checkpointer.aget_state.return_value = _static_state(status, reason)

    chunks = await _run(service, granularity)
    last = chunks[-1]
    assert last.event == StreamEvent.UPDATES
    assert last.data["pause"] == pause
    assert last.data["status"] == update_status
    assert last.data["node"] == "N1"


@pytest.mark.parametrize("granularity", [ResponseGranularity.LOW, ResponseGranularity.PARTIAL])
@pytest.mark.parametrize("pause", ["before", "after", "remote_tool"])
@pytest.mark.asyncio
async def test_no_duplicate_static_pause_when_core_already_sent_it(granularity, pause):
    sent = StreamChunk(
        event=StreamEvent.UPDATES, data={"status": "node_invoked", "node": "N1", "pause": pause}
    )

    async def astream(*args, **kwargs):
        yield sent

    graph = MagicMock()
    graph.astream = astream
    service = _service(graph, timeout=None)
    service.checkpointer.aget_state.return_value = _static_state(
        "interrupted_before" if pause == "before" else "interrupted_after",
        "remote_tool_call" if pause == "remote_tool" else f"interrupt_{pause}: N1",
    )

    chunks = await _run(service, granularity)
    assert len([c for c in chunks if (c.data or {}).get("pause") == pause]) == 1
    assert len(chunks) == 1
