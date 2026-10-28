"""B4 (resume bound to the open interrupt) and B20 (request size limits).

- ``interrupt_id`` on REST and WS resume: 409 when the thread is not paused at that interrupt,
  checked by the API before anything runs; core's ``InterruptMismatchError`` is 409 and
  ``ResumeValidationError`` is 422 (error chunk on stream and WS). The installed core may not
  have them yet, so the tests install stand-in classes.
- At most 500 messages, 100 content blocks per message and 1_000_000 characters of text.
"""

# ruff: noqa: S101

from __future__ import annotations

import importlib
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, WebSocketDisconnect
from pydantic import ValidationError
from tenxgraph.core import exceptions as core_exceptions
from tenxgraph.core.state import StreamEvent

from tenxgraph_api.src.app.core.auth.request_config import (
    MAX_BLOCKS_PER_MESSAGE,
    MAX_MESSAGES,
    MAX_TEXT_CHARS,
    ClientInputError,
    request_size_error,
)
from tenxgraph_api.src.app.routers.ag_ui.converter import parse_run_input
from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
    PutMessagesSchema,
    StateSchema,
)
from tenxgraph_api.src.app.routers.graph.router import websocket_graph
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
    WsGraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService


def _msg(text: str = "hi") -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _tool_msg(n: int = 1) -> dict[str, Any]:
    return {
        "role": "tool",
        "content": [{"type": "tool_result", "call_id": f"c{i}", "output": "x"} for i in range(n)],
    }


# ---------------------------------------------------------------------------
# core stand-ins (the installed core predates these errors)
# ---------------------------------------------------------------------------


class FakeMismatch(Exception):
    pass


class FakeResumeValidation(Exception):
    pass


@pytest.fixture
def core_errors(monkeypatch):
    monkeypatch.setattr(core_exceptions, "InterruptMismatchError", FakeMismatch, raising=False)
    monkeypatch.setattr(
        core_exceptions, "ResumeValidationError", FakeResumeValidation, raising=False
    )


def _service(state: Any = None) -> GraphService:
    """A GraphService whose collaborators are stubs; ``state`` is the thread's checkpoint."""
    srv = GraphService.__new__(GraphService)
    srv._graph = MagicMock()
    srv.checkpointer = MagicMock()
    srv.checkpointer.aget_state = AsyncMock(return_value=state)
    srv.checkpointer.aget_thread_owner = AsyncMock(return_value=None)
    srv.config = MagicMock()
    srv.config.thread_name_generator_path = None
    srv._media_service = None
    srv._telemetry = None
    srv._prepare_input = AsyncMock(
        return_value=({"messages": []}, {"thread_id": "t1"}, {"is_new_thread": False})
    )
    srv._check_tool_results = AsyncMock()
    srv._save_thread = AsyncMock(return_value=False)
    return srv


@pytest.fixture(autouse=True)
def _no_telemetry(monkeypatch):
    monkeypatch.setattr(GraphService, "telemetry", None)


def _paused_at(monkeypatch, interrupt_id: str | None) -> None:
    # ``tenxgraph.utils.interrupt`` the attribute is the interrupt() function, not the module.
    interrupt_module = importlib.import_module("tenxgraph.utils.interrupt")

    pending = None if interrupt_id is None else SimpleNamespace(id=interrupt_id)
    monkeypatch.setattr(interrupt_module, "pending_interrupt", lambda state: pending)


def _resume(**kwargs: Any) -> GraphInputSchema:
    return GraphInputSchema(resume={"ok": True}, **kwargs)


USER = {"user_id": "u1"}


# ---------------------------------------------------------------------------
# B4: interrupt_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matching_interrupt_id_runs(monkeypatch):
    _paused_at(monkeypatch, "i-1")
    srv = _service(state=object())
    srv._ainvoke_bounded = AsyncMock(return_value={"messages": [], "state": None})
    out = await srv.invoke_graph(_resume(interrupt_id="i-1"), USER)
    assert out.messages == []
    srv._ainvoke_bounded.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrong_interrupt_id_is_409_and_nothing_runs(monkeypatch):
    _paused_at(monkeypatch, "i-2")
    srv = _service(state=object())
    srv._ainvoke_bounded = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await srv.invoke_graph(_resume(interrupt_id="i-1"), USER)
    assert exc.value.status_code == 409
    assert "i-1" in exc.value.detail
    assert "i-2" in exc.value.detail
    srv._ainvoke_bounded.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, object()])
async def test_interrupt_id_while_not_paused_is_409(monkeypatch, state):
    _paused_at(monkeypatch, None)
    srv = _service(state=state)
    srv._ainvoke_bounded = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await srv.invoke_graph(_resume(interrupt_id="i-1"), USER)
    assert exc.value.status_code == 409
    assert "not paused" in exc.value.detail
    srv._ainvoke_bounded.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_without_interrupt_id_still_works(monkeypatch):
    _paused_at(monkeypatch, "i-2")
    srv = _service(state=object())
    srv._ainvoke_bounded = AsyncMock(return_value={"messages": [], "state": None})
    await srv.invoke_graph(_resume(), USER)
    srv._ainvoke_bounded.assert_awaited_once()
    # No id sent: the checkpoint is not even read by the API-side check.
    srv.checkpointer.aget_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_id_on_a_plain_run_is_ignored(monkeypatch):
    srv = _service(state=object())
    srv._ainvoke_bounded = AsyncMock(return_value={"messages": [], "state": None})
    await srv.invoke_graph(GraphInputSchema(messages=[_msg()], interrupt_id="i-1"), USER)
    srv._ainvoke_bounded.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_sends_error_chunk_for_wrong_interrupt_id(monkeypatch):
    _paused_at(monkeypatch, "i-2")
    srv = _service(state=object())
    srv._astream_bounded = MagicMock()
    chunks = [c async for c in srv.stream_chunks(_resume(interrupt_id="i-1"), USER)]
    assert len(chunks) == 1
    assert chunks[0].event == StreamEvent.ERROR
    assert "does not match the open interrupt 'i-2'" in chunks[0].data["reason"]
    srv._astream_bounded.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_input_passes_the_id_to_core():
    srv = GraphService.__new__(GraphService)
    srv._graph = MagicMock()
    srv.checkpointer = MagicMock()
    srv.checkpointer.aget_thread_owner = AsyncMock(return_value=None)
    srv.checkpointer.aget_state = AsyncMock(return_value=None)
    srv.config = MagicMock()
    srv._media_service = None
    srv.check_media_urls = MagicMock()
    input_data, _, _ = await srv._prepare_input(
        _resume(interrupt_id="i-1", config={"thread_id": "t1"})
    )
    assert input_data["resume"] == {"ok": True}
    assert input_data["resume_interrupt_id"] == "i-1"
    input_data, _, _ = await srv._prepare_input(_resume(config={"thread_id": "t1"}))
    assert "resume_interrupt_id" not in input_data


# core errors


@pytest.mark.asyncio
async def test_core_mismatch_is_409_and_validation_is_422_on_invoke(core_errors):
    srv = _service()
    srv._check_resume_target = AsyncMock()
    for error, status in ((FakeMismatch("stale"), 409), (FakeResumeValidation("bad"), 422)):
        srv._ainvoke_bounded = AsyncMock(side_effect=error)
        with pytest.raises(HTTPException) as exc:
            await srv.invoke_graph(_resume(), USER)
        assert exc.value.status_code == status
        assert exc.value.detail == str(error)


@pytest.mark.asyncio
async def test_core_errors_become_error_chunks_on_stream(core_errors):
    srv = _service()
    srv._check_resume_target = AsyncMock()
    for error in (FakeMismatch("stale interrupt"), FakeResumeValidation("not a boolean")):

        def boom(*args, _error=error, **kwargs):
            raise _error

        srv._astream_bounded = boom
        chunks = [c async for c in srv.stream_chunks(_resume(), USER)]
        assert [c.event for c in chunks] == [StreamEvent.ERROR]
        assert chunks[0].data["reason"] == str(error)


@pytest.mark.asyncio
async def test_core_errors_keep_their_message_in_production(core_errors, monkeypatch):
    from tenxgraph_api.src.app.core.config import settings

    monkeypatch.setattr(settings.get_settings(), "MODE", "production")
    srv = _service()
    srv._check_resume_target = AsyncMock()
    srv._astream_bounded = MagicMock(side_effect=FakeResumeValidation("expected a boolean"))
    chunks = [c async for c in srv.stream_chunks(_resume(), USER)]
    assert chunks[0].data["reason"] == "expected a boolean"


# WS


class TestWsInterruptId:
    def test_ws_carries_resume_and_interrupt_id(self):
        ws = WsGraphInputSchema(invoke_type="fresh", resume=False, interrupt_id="i-1")
        gi = ws.to_graph_input()
        assert gi.is_resume
        assert gi.resume is False
        assert gi.interrupt_id == "i-1"

    def test_ws_without_resume_is_not_a_resume(self):
        gi = WsGraphInputSchema(invoke_type="fresh", messages=[_msg()]).to_graph_input()
        assert not gi.is_resume
        assert gi.interrupt_id is None

    def test_ws_fresh_still_needs_messages_without_resume(self):
        with pytest.raises(ValidationError, match="messages must not be empty"):
            WsGraphInputSchema(invoke_type="fresh")

    @pytest.mark.asyncio
    async def test_ws_wrong_interrupt_id_is_an_error_chunk(self, monkeypatch):
        _paused_at(monkeypatch, "i-2")
        srv = _service(state=object())
        srv._astream_bounded = MagicMock()
        monkeypatch.setattr(GraphService, "is_live_agent", False)
        ws = MagicMock()
        ws.accept = AsyncMock()
        ws.close = AsyncMock()
        ws.send_text = AsyncMock()
        frame = {"invoke_type": "fresh", "resume": True, "interrupt_id": "i-1"}
        ws.receive_text = AsyncMock(side_effect=[json.dumps(frame), WebSocketDisconnect()])
        await websocket_graph(websocket=ws, service=srv, user={})
        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        assert sent[0]["event"] == StreamEvent.ERROR
        assert "does not match the open interrupt" in sent[0]["data"]["reason"]
        srv._astream_bounded.assert_not_called()


# ---------------------------------------------------------------------------
# B20: limits
# ---------------------------------------------------------------------------


def test_constants():
    assert (MAX_MESSAGES, MAX_BLOCKS_PER_MESSAGE, MAX_TEXT_CHARS) == (500, 100, 1_000_000)


def test_checker_accepts_values_at_the_limits():
    assert request_size_error([_msg()] * MAX_MESSAGES) is None
    block = {"type": "text", "text": ""}
    assert request_size_error([{"content": [block] * MAX_BLOCKS_PER_MESSAGE}]) is None
    assert request_size_error([_msg("a" * MAX_TEXT_CHARS)]) is None
    assert request_size_error(None) is None


def test_text_limit_counts_characters_not_bytes():
    # 400_000 characters of 3-byte UTF-8 is 1.2 MB of bytes but under the limit.
    assert request_size_error([_msg("€" * 400_000)]) is None


def test_text_limit_is_total_across_messages_and_blocks():
    half = "a" * (MAX_TEXT_CHARS // 2)
    assert request_size_error([_msg(half), _msg(half)]) is None
    error = request_size_error([_msg(half), _msg(half), _msg("a")])
    assert error and "1000000 characters" in error


def test_non_text_blocks_do_not_count():
    big = {"type": "tool_result", "call_id": "c", "output": "a" * (2 * MAX_TEXT_CHARS)}
    assert request_size_error([{"role": "tool", "content": [big]}]) is None


def _too_many_messages() -> dict[str, Any]:
    return {"messages": [_msg()] * (MAX_MESSAGES + 1)}


def _too_many_blocks() -> dict[str, Any]:
    block = {"type": "text", "text": "x"}
    return {"messages": [{"role": "user", "content": [block] * (MAX_BLOCKS_PER_MESSAGE + 1)}]}


def _too_much_text() -> dict[str, Any]:
    return {"messages": [_msg("a" * (MAX_TEXT_CHARS + 1))]}


CASES = [
    (_too_many_messages, "at most 500 messages"),
    (_too_many_blocks, "at most 100 content blocks per message"),
    (_too_much_text, "at most 1000000 characters"),
]


@pytest.mark.parametrize(("make", "needle"), CASES)
def test_invoke_and_stream_input_over_a_limit(make, needle):
    # GraphInputSchema is the body of /v1/graph/invoke and /v1/graph/stream: a ValidationError
    # here is a 422 from FastAPI.
    with pytest.raises(ValidationError, match=needle):
        GraphInputSchema(**make())


@pytest.mark.parametrize(("make", "needle"), CASES)
def test_ws_messages_over_a_limit(make, needle):
    with pytest.raises(ValidationError, match=needle):
        WsGraphInputSchema(invoke_type="fresh", **make())


@pytest.mark.parametrize(("make", "needle"), CASES)
def test_ws_tool_result_over_a_limit(make, needle):
    with pytest.raises(ValidationError, match=needle):
        WsGraphInputSchema(
            invoke_type="resume", tool_result=make()["messages"], config={"thread_id": "t"}
        )


def test_ws_tool_result_blocks_limit_names_the_field():
    blocks = _tool_msg(MAX_BLOCKS_PER_MESSAGE + 1)
    with pytest.raises(ValidationError, match=r"tool_result\[0\]: at most 100 content blocks"):
        WsGraphInputSchema(invoke_type="resume", tool_result=[blocks], config={"thread_id": "t"})


@pytest.mark.parametrize(("make", "needle"), CASES)
def test_put_state_context_over_a_limit(make, needle):
    with pytest.raises(ValidationError, match=needle):
        StateSchema(state={"context": make()["messages"]})
    with pytest.raises(ValidationError, match=r"state\.context"):
        StateSchema(state={"context": make()["messages"]})


def test_put_state_without_context_is_fine():
    assert StateSchema(state={"plan": "pro"}).state == {"plan": "pro"}


@pytest.mark.parametrize(("make", "needle"), CASES)
def test_post_messages_over_a_limit(make, needle):
    with pytest.raises(ValidationError, match=needle):
        PutMessagesSchema(**make())


def _ag_ui_body(messages: list[Any]) -> dict[str, Any]:
    return {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
        "messages": messages,
    }


def _ag_user(text: Any, n: int = 0) -> dict[str, Any]:
    return {"id": f"u{n}", "role": "user", "content": text}


def test_ag_ui_accepts_a_normal_request():
    run_input = parse_run_input(_ag_ui_body([_ag_user("hi")]))
    assert run_input.messages[0].content == "hi"


def test_ag_ui_too_many_messages():
    body = _ag_ui_body([_ag_user("hi", i) for i in range(MAX_MESSAGES + 1)])
    with pytest.raises(ClientInputError, match="at most 500 messages"):
        parse_run_input(body)


def test_ag_ui_too_many_content_parts():
    parts = [{"type": "text", "text": "x"}] * (MAX_BLOCKS_PER_MESSAGE + 1)
    with pytest.raises(ClientInputError, match="at most 100 content blocks per message"):
        parse_run_input(_ag_ui_body([_ag_user(parts)]))


@pytest.mark.parametrize("as_parts", [False, True])
def test_ag_ui_too_much_text(as_parts):
    text = "a" * (MAX_TEXT_CHARS + 1)
    content: Any = [{"type": "text", "text": text}] if as_parts else text
    with pytest.raises(ClientInputError, match="at most 1000000 characters"):
        parse_run_input(_ag_ui_body([_ag_user(content)]))


def _ag_assistant(text: str, n: int = 0) -> dict[str, Any]:
    return {"id": f"a{n}", "role": "assistant", "content": text}


def test_ag_ui_long_history_is_accepted():
    # AG-UI clients resend the whole conversation; only the trailing turn reaches the graph.
    history: list[Any] = []
    for i in range(400):
        history += [_ag_user("q" * 2_000, i), _ag_assistant("a" * 2_000, i)]
    history.append(_ag_user("next", 999))
    assert len(history) > MAX_MESSAGES
    run_input = parse_run_input(_ag_ui_body(history))
    assert run_input.messages[-1].content == "next"


def test_ag_ui_trailing_turn_gets_the_normal_limits():
    history = [_ag_assistant("a"), *[_ag_user("hi", i) for i in range(MAX_MESSAGES + 1)]]
    with pytest.raises(ClientInputError, match="at most 500 messages"):
        parse_run_input(_ag_ui_body(history))


def test_ag_ui_history_has_a_loose_cap():
    from tenxgraph_api.src.app.core.auth.request_config import MAX_AG_UI_HISTORY_MESSAGES

    history = [_ag_assistant("a", i) for i in range(MAX_AG_UI_HISTORY_MESSAGES)]
    history.append(_ag_user("b"))
    with pytest.raises(ClientInputError, match=f"at most {MAX_AG_UI_HISTORY_MESSAGES} messages"):
        parse_run_input(_ag_ui_body(history))


def test_request_just_over_the_block_limit_is_rejected_fast():
    # A 150k-block body took seconds inside core; the limit has to trip before any real work.
    block = {"type": "text", "text": "x"}
    payload = [{"role": "user", "content": [block] * 150_000}]
    start = time.perf_counter()
    with pytest.raises(ValidationError, match="content blocks"):
        GraphInputSchema(messages=payload)
    with pytest.raises(ValidationError, match="content blocks"):
        WsGraphInputSchema(invoke_type="fresh", messages=payload)
    with pytest.raises(ValidationError, match="messages"):
        GraphInputSchema(messages=[_msg()] * 60_000)
    assert time.perf_counter() - start < 0.5


def test_request_at_the_block_limit_is_checked_fast():
    block = {"type": "text", "text": "x"}
    payload = [{"role": "user", "content": [block] * (MAX_BLOCKS_PER_MESSAGE + 1)}] * MAX_MESSAGES
    start = time.perf_counter()
    with pytest.raises(ValidationError, match="content blocks"):
        GraphInputSchema(messages=payload)
    assert time.perf_counter() - start < 0.5
