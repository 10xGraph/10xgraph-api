"""An empty ``resume`` continues an interrupt_before / interrupt_after pause with no message."""

# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tenxgraph.core.state import AgentState
from tenxgraph.core.state.execution_state import ExecutionStatus
from tenxgraph.utils.interrupt import Interrupt, pause_at

from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema

from .test_run_limits import ALICE, _service


def _static_pause(status: ExecutionStatus) -> AgentState:
    state = AgentState()
    state.set_interrupt("TOOL", "HITL", status)
    return state


async def _prepare(state, **fields):
    service = _service(MagicMock(), timeout=None)
    service.checkpointer.aget_state.return_value = state
    graph_input = GraphInputSchema(config={"thread_id": "t1"}, **fields)
    input_data, _, _ = await service._prepare_input(graph_input, ALICE["user_id"])
    return input_data


@pytest.mark.parametrize(
    "status", [ExecutionStatus.INTERRUPTED_BEFORE, ExecutionStatus.INTERRUPTED_AFTER]
)
@pytest.mark.asyncio
async def test_empty_resume_continues_a_static_pause_without_a_message(status):
    input_data = await _prepare(_static_pause(status), resume=None)
    assert input_data["messages"] == []
    assert "resume" not in input_data


@pytest.mark.asyncio
async def test_empty_resume_on_an_unpaused_thread_still_reaches_core():
    # Core answers "Nothing to resume"; the API must not hide it.
    input_data = await _prepare(AgentState(), resume=None)
    assert "resume" in input_data
    input_data = await _prepare(None, resume=None)
    assert "resume" in input_data


@pytest.mark.asyncio
async def test_interrupt_resume_is_unchanged():
    state = AgentState()
    pause_at(state, Interrupt(id="i1", key="k", node="ASK", value={"amount": 50}))
    input_data = await _prepare(state, resume={"approved": True}, interrupt_id="i1")
    assert input_data["resume"] == {"approved": True}
    assert input_data["resume_interrupt_id"] == "i1"


@pytest.mark.asyncio
async def test_remote_tool_pause_keeps_the_resume_key():
    state = _static_pause(ExecutionStatus.INTERRUPTED_AFTER)
    state.execution_meta.interrupt_reason = "remote_tool_call"
    input_data = await _prepare(state, resume=None)
    assert "resume" in input_data
