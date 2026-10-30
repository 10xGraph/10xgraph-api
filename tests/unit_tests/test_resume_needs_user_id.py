"""Resume at ``interrupt()`` must work on a checkpointer that requires ``config['user_id']``.

PgCheckpointer raises ``user_id must be provided in config`` on every read without it, so the
service has to put the authenticated user into the run config before its first state read.
"""

# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from tenxgraph.core.state import AgentState
from tenxgraph.utils.interrupt import Interrupt, pause_at

from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema

from .test_run_limits import ALICE, _service


def _strict_service(graph):
    service = _service(graph, timeout=None)
    state = AgentState()
    pause_at(state, Interrupt(id="i1", key="k", node="ASK", value={"amount": 50}))

    async def aget_state(config):
        if not config.get("user_id"):
            raise ValueError("user_id must be provided in config")
        return state

    service.checkpointer.aget_state = AsyncMock(side_effect=aget_state)
    return service


def _resume(**fields):
    return GraphInputSchema(config={"thread_id": "t1"}, resume={"ok": True}, **fields)


@pytest.mark.parametrize("interrupt_id", [None, "i1"])
@pytest.mark.asyncio
async def test_invoke_resume_reads_state_with_user_id(interrupt_id):
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={"messages": [], "state": AgentState()})
    service = _strict_service(graph)
    await service.invoke_graph(_resume(interrupt_id=interrupt_id), ALICE)
    input_data = graph.ainvoke.call_args.args[0]
    assert input_data["resume"] == {"ok": True}


@pytest.mark.asyncio
async def test_stream_resume_reads_state_with_user_id():
    graph = MagicMock()
    seen = {}

    async def astream(input_data, **kwargs):
        seen.update(input_data)
        return
        yield

    graph.astream = astream
    service = _strict_service(graph)
    chunks = [c async for c in service.stream_chunks(_resume(interrupt_id="i1"), ALICE)]
    assert not any(getattr(c, "is_error", False) for c in chunks)
    assert seen["resume"] == {"ok": True}


@pytest.mark.asyncio
async def test_websocket_path_shares_stream_chunks():
    # /v1/graph/ws drives GraphService.stream_graph, which wraps stream_chunks.
    graph = MagicMock()
    seen = {}

    async def astream(input_data, **kwargs):
        seen.update(input_data)
        return
        yield

    graph.astream = astream
    service = _strict_service(graph)
    out = [c async for c in service.stream_graph(_resume(), ALICE)]
    assert not any('"error"' in c for c in out)
    assert seen["resume"] == {"ok": True}


@pytest.mark.asyncio
async def test_client_cannot_supply_user_id():
    service = _strict_service(MagicMock())
    graph_input = GraphInputSchema(config={"thread_id": "t1", "user_id": "mallory"}, resume={})
    try:
        _, config, _ = await service._prepare_input(graph_input, ALICE["user_id"])
    except Exception:  # rejected outright is also fine
        return
    assert config["user_id"] == "alice"
