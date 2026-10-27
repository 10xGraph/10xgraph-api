"""Client mistakes answer 404 or 422, never 500; internal fields stay out of responses.

- A missing message is a 404 on GET and DELETE, whatever the checkpointer raises.
- A wrongly shaped built-in state field on ``PUT state`` is a 422 naming the field.
- State responses carry no ``execution_meta.internal_data``; message ``raw`` is not returned.
"""

# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import InMemoryCheckpointer

from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
    MessagesListResponseSchema,
)
from tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service import (
    CheckpointerService,
)
from tenxgraph_api.src.app.utils.parse_output import parse_message_output, parse_state_output


USER = {"user_id": "u1"}
CFG = {"thread_id": "t1"}


def _service(checkpointer: Any) -> CheckpointerService:
    service = CheckpointerService.__new__(CheckpointerService)
    service.checkpointer = checkpointer
    service.settings = MagicMock()
    return service


@pytest.fixture
def real_service() -> CheckpointerService:
    return _service(InMemoryCheckpointer())


@pytest.fixture
def pg_like_service() -> CheckpointerService:
    """Postgres raises ``ValueError("Message not found: ...")`` and deletes silently."""
    checkpointer = MagicMock()
    checkpointer.aget_message = AsyncMock(side_effect=ValueError("Message not found: nope"))
    checkpointer.adelete_message = AsyncMock(return_value=None)
    return _service(checkpointer)


# ---------------------------------------------------------------------------
# Missing message: 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_missing_message_is_404_in_memory(real_service):
    with pytest.raises(HTTPException) as exc:
        await real_service.get_message(CFG, USER, "nope")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_missing_message_is_404_for_postgres_style_error(pg_like_service):
    with pytest.raises(HTTPException) as exc:
        await pg_like_service.get_message(CFG, USER, "nope")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_missing_message_is_404_when_core_returns_none():
    checkpointer = MagicMock()
    checkpointer.aget_message = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc:
        await _service(checkpointer).get_message(CFG, USER, "nope")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_missing_message_is_404_in_memory(real_service):
    with pytest.raises(HTTPException) as exc:
        await real_service.delete_message(CFG, USER, "nope")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_missing_message_is_404_for_postgres_style_error(pg_like_service):
    with pytest.raises(HTTPException) as exc:
        await pg_like_service.delete_message(CFG, USER, "nope")
    assert exc.value.status_code == 404
    pg_like_service.checkpointer.adelete_message.assert_not_called()


@pytest.mark.asyncio
async def test_existing_message_is_still_returned_and_deleted(real_service):
    msg = Message.text_message("hi", role="user")
    await real_service.checkpointer.aput_messages(CFG, [msg])
    got = await real_service.get_message(CFG, USER, msg.message_id)
    assert got.message_id == msg.message_id
    await real_service.delete_message(CFG, USER, msg.message_id)
    with pytest.raises(HTTPException) as exc:
        await real_service.get_message(CFG, USER, msg.message_id)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_other_value_errors_are_not_turned_into_404():
    checkpointer = MagicMock()
    checkpointer.aget_message = AsyncMock(side_effect=ValueError("thread_id must be provided"))
    with pytest.raises(ValueError, match="thread_id"):
        await _service(checkpointer).get_message(CFG, USER, "m1")


# ---------------------------------------------------------------------------
# PUT state: wrong shape is 422
# ---------------------------------------------------------------------------


@pytest.fixture
def state_service() -> CheckpointerService:
    return _service(InMemoryCheckpointer())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        {"context": "notalist"},
        {"context": 5},
        {"context": {"role": "user"}},
        {"context": [1, "x"]},
        {"context": [{"role": "user", "content": "not blocks"}]},
        {"context": [{"nonsense": True}]},
    ],
)
async def test_put_state_wrong_built_in_shape_is_422(state_service, state):
    with pytest.raises(HTTPException) as exc:
        await state_service.put_state(CFG, USER, state)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_put_state_422_names_the_field_not_the_value(state_service):
    with pytest.raises(HTTPException) as exc:
        await state_service.put_state(CFG, USER, {"context": "secret-looking-value"})
    assert "context" in exc.value.detail
    assert "secret-looking-value" not in exc.value.detail


@pytest.mark.asyncio
async def test_put_state_valid_context_still_works(state_service):
    res = await state_service.put_state(
        CFG, USER, {"context": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    )
    assert len(res.state["context"]) == 1


# ---------------------------------------------------------------------------
# Internal fields
# ---------------------------------------------------------------------------


def _state_with_internals() -> AgentState:
    state = AgentState()
    state.execution_meta.internal_data = {"error": "boom", "trace_id": "x", "active_skills": ["a"]}
    state.context = [Message.text_message("hi", role="assistant")]
    state.context[0].raw = {"provider": "payload"}
    return state


def test_state_output_drops_internal_data_except_error_and_message_raw():
    out = parse_state_output(MagicMock(), _state_with_internals())
    meta = out["execution_meta"]
    assert meta["internal_data"] == {"error": "boom"}
    for kept in ("current_node", "step", "status", "interrupted_node", "interrupt_reason"):
        assert kept in meta
    assert "interrupt_data" in meta
    assert "raw" not in out["context"][0]
    assert out["context"][0]["content"]


def test_message_output_drops_raw_but_keeps_usages():
    msg = Message.text_message("hi", role="assistant")
    msg.raw = {"provider": "payload"}
    out = parse_message_output(MagicMock(), msg)
    assert "raw" not in out
    assert "usages" in out
    assert out["content"]


def test_message_list_output_drops_raw_from_every_message():
    a, b = Message.text_message("a"), Message.text_message("b")
    a.raw = {"x": 1}
    out = parse_message_output(MagicMock(), MessagesListResponseSchema(messages=[a, b]))
    assert all("raw" not in m for m in out["messages"])
    assert [m["message_id"] for m in out["messages"]] == [a.message_id, b.message_id]


def test_non_model_responses_pass_through():
    assert parse_message_output(MagicMock(), {"k": 1}) == {"k": 1}


@pytest.mark.asyncio
async def test_put_state_response_has_no_raw_or_extra_internal_data():
    service = _service(InMemoryCheckpointer())
    res = await service.put_state(
        CFG, USER, {"context": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    )
    assert "raw" not in res.state["context"][0]
    assert res.state["execution_meta"]["internal_data"] == {}
