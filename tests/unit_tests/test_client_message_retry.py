"""A client retrying a failed run does not store its user message twice.

The server replaces every client ``message_id`` and keeps the client's id in
``metadata["client_message_id"]``. Before a run on an existing thread, a user message whose
client id the thread already stores is dropped. When all are dropped the run status decides: a
turn that did not finish (running, error, cut off) continues with no new messages, a completed
one is a 409 ``MESSAGE_ALREADY_PROCESSED``, a paused thread must be resumed (409).
"""

# ruff: noqa: S101

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from tenxgraph.core.authz import Identity
from tenxgraph.core.state import AgentState, Message
from tenxgraph.core.state.execution_state import ExecutionStatus
from tenxgraph.core.state.message_block import TextBlock

from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.core.exceptions import MessageAlreadyProcessedError
from tenxgraph_api.src.app.core.exceptions.client_errors import as_http_error, client_error
from tenxgraph_api.src.app.core.validation.request_config import with_fresh_message_ids
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
    WsGraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.routers.graph.services.run_models import RunChecks


U1 = Identity(user_id="u1")
TOOL_RESULT = {
    "role": "tool",
    "content": [{"type": "tool_result", "call_id": "c1", "output": "Dhaka"}],
}


def _user(text: str = "hi", **extra) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}], **extra}


def _stored(role: str, message_id, client_id: str | None = None) -> Message:
    metadata = {"client_message_id": client_id} if client_id else {}
    return Message(
        message_id=message_id, role=role, content=[TextBlock(text="x")], metadata=metadata
    )


def _state(
    context: list[Message], status: ExecutionStatus, reason: str | None = None
) -> AgentState:
    state = AgentState(context=context)
    state.execution_meta.status = status
    state.execution_meta.interrupt_reason = reason
    state.execution_meta.interrupted_node = "MAIN" if reason else None
    return state


def _service(
    context: list[Message] | None = None,
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    reason: str | None = None,
    state: object | None = None,
) -> GraphService:
    srv = GraphService.__new__(GraphService)
    srv._model_choices = {}
    srv._graph = SimpleNamespace(_state_graph=SimpleNamespace(nodes={}, _state=AgentState()))
    srv.checkpointer = MagicMock()
    srv.checkpointer.aget_thread_owner = AsyncMock(return_value=None)
    if state is None and context is not None:
        state = _state(context, status, reason)
    srv.checkpointer.aget_state = AsyncMock(return_value=state)
    srv.config = SimpleNamespace(runs=RunsConfig())
    srv.thread_name_generator = None
    srv._media_service = None
    srv._telemetry = None
    return srv


def _on_thread(*messages: dict, **extra) -> GraphInputSchema:
    return GraphInputSchema(messages=list(messages), config={"thread_id": "t1"}, **extra)


# ---------------------------------------------------------------------------
# the client id is kept, never trusted, never stored as the id
# ---------------------------------------------------------------------------


def test_client_id_is_kept_in_metadata():
    [out] = GraphInputSchema(messages=[_user(message_id="client-1")]).messages
    assert out.message_id != "client-1"
    assert out.metadata["client_message_id"] == "client-1"


def test_ws_input_keeps_the_client_id_too():
    ws = WsGraphInputSchema(invoke_type="fresh", messages=[_user(message_id="client-1")])
    [out] = ws.to_graph_input().messages
    assert out.message_id != "client-1"
    assert out.metadata["client_message_id"] == "client-1"


def test_client_written_client_message_id_is_not_trusted():
    forged = _user(metadata={"client_message_id": "assistant-7", "note": "kept"})
    [out] = GraphInputSchema(messages=[forged]).messages
    assert "client_message_id" not in out.metadata
    assert out.metadata["note"] == "kept"

    both = _user(message_id="real-1", metadata={"client_message_id": "assistant-7"})
    [out] = GraphInputSchema(messages=[both]).messages
    assert out.metadata["client_message_id"] == "real-1"


@pytest.mark.parametrize("placeholder", ["0", "", 0, None])
def test_placeholder_ids_record_no_client_id(placeholder):
    # The TypeScript client sends "0" for a message without an id.
    out = with_fresh_message_ids([_user(message_id=placeholder)])
    assert "client_message_id" not in out[0]["metadata"]


def test_message_without_an_id_records_none():
    [out] = GraphInputSchema(messages=[_user()]).messages
    assert "client_message_id" not in out.metadata


@pytest.mark.asyncio
async def test_reused_assistant_id_cannot_overwrite_it():
    # The client names a stored assistant message's id: the stored id stays the server's own.
    srv = _service([_stored("user", "s1", "c1"), _stored("assistant", "s2")])
    graph_input = _on_thread(_user("new", message_id="c-new"), _user("forged", message_id="s2"))
    input_data, _, _ = await srv._prepare_input(graph_input, U1)
    assert all(m.message_id not in ("s1", "s2", "c-new") for m in input_data["messages"])
    assert [m.text() for m in input_data["messages"]] == ["new", "forged"]


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_of_an_unanswered_message_runs_with_no_new_messages():
    # The failed attempt saved the user message but no reply.
    srv = _service(
        [_stored("user", "s1", "c1"), _stored("assistant", "s2"), _stored("user", "s3", "c2")],
        ExecutionStatus.ERROR,
    )
    input_data, _, _ = await srv._prepare_input(_on_thread(_user(message_id="c2")), U1)
    assert input_data["messages"] == []


def _tool_call(message_id: str) -> Message:
    return Message(
        message_id=message_id,
        role="assistant",
        content=[TextBlock(text="")],
        tools_calls=[{"id": "call1", "type": "function", "function": {"name": "f"}}],
    )


def _tool_result(message_id: str) -> Message:
    return Message.tool_message(content=[], message_id=message_id)


@pytest.mark.asyncio
async def test_crash_after_the_tool_call_continues_the_turn():
    # The run died after saving the assistant tool call: still RUNNING in the store.
    srv = _service([_stored("user", "s1", "c1"), _tool_call("s2")], ExecutionStatus.RUNNING)
    input_data, _, _ = await srv._prepare_input(_on_thread(_user(message_id="c1")), U1)
    assert input_data["messages"] == []


@pytest.mark.asyncio
async def test_error_after_the_tool_result_continues_the_turn():
    context = [_stored("user", "s1", "c1"), _tool_call("s2"), _tool_result("s3")]
    srv = _service(context, ExecutionStatus.ERROR)
    input_data, _, _ = await srv._prepare_input(_on_thread(_user(message_id="c1")), U1)
    assert input_data["messages"] == []


@pytest.mark.parametrize("reason", ["cancelled", "shutdown", "crashed", "stop_requested"])
@pytest.mark.asyncio
async def test_run_cut_off_mid_turn_continues_the_turn(reason):
    # Cancel, shutdown, crash recovery and stop save the run INTERRUPTED_AFTER.
    context = [_stored("user", "s1", "c1"), _tool_call("s2"), _tool_result("s3")]
    srv = _service(context, ExecutionStatus.INTERRUPTED_AFTER, reason)
    input_data, _, _ = await srv._prepare_input(_on_thread(_user(message_id="c1")), U1)
    assert input_data["messages"] == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (ExecutionStatus.INTERRUPTED_BEFORE, "interrupt_before: MAIN"),
        (ExecutionStatus.INTERRUPTED_AFTER, "interrupt"),
        (ExecutionStatus.INTERRUPTED_AFTER, "remote_tool_call"),
    ],
)
@pytest.mark.asyncio
async def test_retry_on_a_paused_thread_is_a_409_and_never_resumes(status, reason):
    srv = _service([_stored("user", "s1", "c1"), _tool_call("s2")], status, reason)
    with pytest.raises(HTTPException) as exc:
        await srv._prepare_input(_on_thread(_user(message_id="c1")), U1)
    assert exc.value.status_code == 409
    assert "paused" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_completed_turn_ending_on_the_user_message_is_a_409():
    # Completed is final even when no reply message was stored.
    srv = _service([_stored("user", "s1", "c1")], ExecutionStatus.COMPLETED)
    with pytest.raises(MessageAlreadyProcessedError):
        await srv._prepare_input(_on_thread(_user(message_id="c1")), U1)


@pytest.mark.asyncio
async def test_old_state_without_execution_meta_falls_back_to_the_last_message():
    unanswered = SimpleNamespace(context=[_stored("assistant", "s0"), _stored("user", "s1", "c1")])
    srv = _service(state=unanswered)
    input_data, _, _ = await srv._prepare_input(_on_thread(_user(message_id="c1")), U1)
    assert input_data["messages"] == []

    answered = SimpleNamespace(context=[_stored("user", "s1", "c1"), _stored("assistant", "s2")])
    srv = _service(state=answered)
    with pytest.raises(MessageAlreadyProcessedError):
        await srv._prepare_input(_on_thread(_user(message_id="c1")), U1)


@pytest.mark.asyncio
async def test_retry_of_an_answered_turn_is_a_409():
    srv = _service([_stored("user", "s1", "c1"), _stored("assistant", "s2")])
    with pytest.raises(MessageAlreadyProcessedError) as exc:
        await srv._prepare_input(_on_thread(_user(message_id="c1")), U1)
    answer = client_error(exc.value)
    assert (answer.status_code, answer.error_code) == (409, "MESSAGE_ALREADY_PROCESSED")
    assert "Idempotency-Key" in answer.message
    assert as_http_error(exc.value) is exc.value  # its own handler answers it


@pytest.mark.asyncio
async def test_older_message_does_not_trigger_a_run_for_another_unanswered_one():
    # Not the latest user message: 409 even though the turn after it is unfinished.
    srv = _service(
        [_stored("user", "s1", "c1"), _stored("assistant", "s2"), _stored("user", "s3", "c2")],
        ExecutionStatus.RUNNING,
    )
    with pytest.raises(MessageAlreadyProcessedError):
        await srv._prepare_input(_on_thread(_user(message_id="c1")), U1)


@pytest.mark.asyncio
async def test_new_message_with_a_new_id_runs():
    srv = _service([_stored("user", "s1", "c1"), _stored("assistant", "s2")])
    input_data, _, _ = await srv._prepare_input(_on_thread(_user("next", message_id="c2")), U1)
    [message] = input_data["messages"]
    assert message.text() == "next"
    assert message.metadata["client_message_id"] == "c2"


@pytest.mark.asyncio
async def test_client_id_equal_to_a_stored_server_id_is_a_new_message():
    # Only stored client ids count as repeats: reusing the assistant reply's server id is new.
    srv = _service([_stored("user", "s1", "c1"), _stored("assistant", "s2")])
    input_data, _, _ = await srv._prepare_input(_on_thread(_user("next", message_id="s2")), U1)
    [message] = input_data["messages"]
    assert message.text() == "next"
    assert message.metadata["client_message_id"] == "s2"


@pytest.mark.asyncio
async def test_no_client_ids_means_no_extra_state_read():
    srv = _service([_stored("user", "s1"), _stored("assistant", "s2")])
    input_data, _, _ = await srv._prepare_input(_on_thread(_user(message_id="0")), U1)
    assert len(input_data["messages"]) == 1
    srv.checkpointer.aget_state.assert_not_called()


@pytest.mark.asyncio
async def test_new_thread_is_not_checked():
    srv = _service([_stored("user", "s1", "c1"), _stored("assistant", "s2")])
    input_data, _, _ = await srv._prepare_input(
        GraphInputSchema(messages=[_user(message_id="c1")]), U1
    )
    assert len(input_data["messages"]) == 1
    srv.checkpointer.aget_state.assert_not_called()


@pytest.mark.asyncio
async def test_resume_is_left_alone():
    srv = _service([_stored("user", "s1", "c1"), _stored("assistant", "s2")])
    srv._is_static_pause = AsyncMock(return_value=False)
    graph_input = _on_thread(_user(message_id="c1"), resume={"ok": True})
    input_data, _, _ = await srv._prepare_input(graph_input, U1)
    assert len(input_data["messages"]) == 1
    assert input_data["resume"] == {"ok": True}


@pytest.mark.asyncio
async def test_tool_results_are_never_dropped():
    srv = _service([_stored("user", "s1", "c1"), _stored("assistant", "s2")])
    graph_input = _on_thread({**TOOL_RESULT, "message_id": "s2"}, _user(message_id="c1"))
    input_data, _, _ = await srv._prepare_input(graph_input, U1)
    assert [m.role for m in input_data["messages"]] == ["tool"]


@pytest.mark.asyncio
async def test_stream_precheck_raises_409_and_shares_the_run_state_read():
    srv = _service([_stored("user", "s1", "c1"), _stored("assistant", "s2")])
    checks = RunChecks(input_checked=True)
    with pytest.raises(MessageAlreadyProcessedError):
        await srv.check_repeated_messages(_on_thread(_user(message_id="c1")), U1, checks)

    retry = _on_thread(_user(message_id="c2"))
    await srv.check_repeated_messages(retry, U1, checks)
    await srv._prepare_input(retry, U1, checks=checks)
    assert srv.checkpointer.aget_state.await_count == 1  # one read for both calls


@pytest.mark.asyncio
async def test_stream_precheck_skips_a_new_thread():
    srv = _service([_stored("user", "s1", "c1"), _stored("assistant", "s2")])
    await srv.check_repeated_messages(
        GraphInputSchema(messages=[_user(message_id="c1")]), U1, RunChecks()
    )
    srv.checkpointer.aget_state.assert_not_called()
