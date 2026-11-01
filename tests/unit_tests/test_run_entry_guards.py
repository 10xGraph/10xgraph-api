"""One guard step for every run entry point, and the WebSocket run lifecycle (G-98 to G-100, G-158).

``/v1/graph/stream``, ``/v1/graph/ws``, ``/v1/graph/live`` and AG-UI all take the same thread
lock and per-user run slot, hold them until the run is really over, and cancel cleanly.
"""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, WebSocketDisconnect

from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.core.leases import LeaseManager
from tenxgraph_api.src.app.routers.graph import run_limits, thread_lock
from tenxgraph_api.src.app.routers.graph.router import (
    realtime_graph_ws,
    stream_graph,
    websocket_graph,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema


ALICE = {"user_id": "alice"}
MSG = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
FRAME = {"invoke_type": "fresh", "messages": MSG, "config": {"thread_id": "t1"}}
THREAD_KEY = "runs:thread:t1"
SLOT_KEY = "runs:user:alice"


def _router():
    return sys.modules[websocket_graph.__module__]


@pytest.fixture(autouse=True)
def limiter(monkeypatch):
    """One in-process limiter behind both the thread lock and the run slot."""
    shared = LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(thread_lock, "lease_manager", lambda: shared)
    monkeypatch.setattr(run_limits, "lease_manager", lambda: shared)
    monkeypatch.setattr(thread_lock, "THREAD_BUSY_WAIT_SECONDS", 0.2)
    router = _router()
    monkeypatch.setattr(router, "ws_identity_still_valid", lambda ws, user: True)
    monkeypatch.setattr(router, "_ws_token_revoked", AsyncMock(return_value=False))
    return shared


def _production(monkeypatch):
    from tenxgraph_api.src.app.core.config import settings

    monkeypatch.setattr(
        settings,
        "get_settings",
        lambda: SimpleNamespace(MODE="production", MAX_REQUEST_SIZE=10_000_000),
    )


def _service(runs=None):
    service = MagicMock()
    service.is_live_agent = False
    service.runs_policy.return_value = runs or RunsConfig(max_concurrent_per_user=1)
    service.stop_graph = AsyncMock(return_value={"ok": True})
    service.check_media_urls = MagicMock()
    service.check_resume_target = AsyncMock()
    return service


def _ws(frames):
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(
        side_effect=[json.dumps(f) if isinstance(f, dict) else f for f in frames]
    )
    return ws


def _sent(ws):
    return [json.loads(c.args[0]) for c in ws.send_text.call_args_list]


async def _until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# /v1/graph/ws: run slot, error text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_run_holds_a_run_slot_and_the_thread_lock(limiter):
    seen = {}

    def stream(*args, **kwargs):
        async def gen():
            seen.update(limiter._held)
            yield "chunk\n"

        return gen()

    service = _service()
    service.stream_graph = stream
    ws = _ws([FRAME, WebSocketDisconnect()])
    await websocket_graph(websocket=ws, service=service, user=ALICE)
    assert seen == {THREAD_KEY: 1, SLOT_KEY: 1}
    assert limiter._held == {}


@pytest.mark.asyncio
async def test_ws_over_the_run_cap_sends_an_error_chunk_and_stays_open(limiter):
    held = await run_limits.acquire_run_slot(ALICE, RunsConfig(max_concurrent_per_user=1))
    service = _service()
    service.stream_graph = MagicMock()
    ws = _ws([FRAME, WebSocketDisconnect()])
    await websocket_graph(websocket=ws, service=service, user=ALICE)
    chunk = _sent(ws)[0]
    assert chunk["event"] == "error"
    assert "Too many runs" in chunk["data"]["reason"]
    service.stream_graph.assert_not_called()
    ws.close.assert_not_awaited()
    assert limiter._held == {SLOT_KEY: 1}  # the thread lock was given back
    held.release()


@pytest.mark.asyncio
async def test_ws_invalid_input_does_not_echo_the_validation_text_in_production(monkeypatch):
    _production(monkeypatch)
    bad = {"invoke_type": "fresh", "messages": [], "config": {"api_key": "hunter2-secret"}}
    service = _service()
    ws = _ws([bad, WebSocketDisconnect()])
    await websocket_graph(websocket=ws, service=service, user=ALICE)
    reason = _sent(ws)[0]["data"]["reason"]
    assert "messages must not be empty" not in reason
    assert "hunter2" not in reason


# ---------------------------------------------------------------------------
# /v1/graph/ws: run lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_lock_is_released_only_after_the_run_generator_closed(limiter):
    held_when_closed = []

    def stream(*args, **kwargs):
        async def gen():
            try:
                yield "one\n"
                yield "two\n"
            finally:
                await asyncio.sleep(0)  # the generator's own async cleanup
                held_when_closed.append(dict(limiter._held))

        return gen()

    service = _service()
    service.stream_graph = stream
    ws = _ws([FRAME, WebSocketDisconnect()])
    ws.send_text = AsyncMock(side_effect=[RuntimeError("socket gone")])
    await websocket_graph(websocket=ws, service=service, user=ALICE)
    await asyncio.sleep(0.05)
    assert held_when_closed == [{THREAD_KEY: 1, SLOT_KEY: 1}]
    assert limiter._held == {}


@pytest.mark.asyncio
async def test_ws_client_leaving_mid_run_cancels_the_run(limiter):
    started, closed = asyncio.Event(), asyncio.Event()

    def stream(*args, **kwargs):
        async def gen():
            try:
                started.set()
                await asyncio.sleep(30)  # a long tool call: nothing to send
                yield "never\n"
            finally:
                closed.set()

        return gen()

    service = _service()
    service.stream_graph = stream
    ws = _ws([FRAME])

    async def disconnect_after_start():
        await started.wait()
        await asyncio.sleep(0.02)
        raise WebSocketDisconnect

    ws.receive_text.side_effect = _frames_then(json.dumps(FRAME), disconnect_after_start)
    await asyncio.wait_for(websocket_graph(websocket=ws, service=service, user=ALICE), 3)
    assert closed.is_set()
    assert limiter._held == {}


def _frames_then(first, later):
    calls = []

    async def receive():
        calls.append(1)
        if len(calls) == 1:
            return first
        return await later()

    return receive


@pytest.mark.asyncio
async def test_ws_slow_consumer_is_cut_off_and_frees_the_lock(limiter, monkeypatch):
    monkeypatch.setattr(_router(), "WS_SEND_TIMEOUT_SECONDS", 0.05)

    def stream(*args, **kwargs):
        async def gen():
            yield "chunk\n"

        return gen()

    service = _service()
    service.stream_graph = stream
    ws = _ws([FRAME])
    ws.receive_text.side_effect = _frames_then(json.dumps(FRAME), _forever)

    async def stalled(text):
        await asyncio.sleep(30)

    ws.send_text = AsyncMock(side_effect=stalled)
    await asyncio.wait_for(websocket_graph(websocket=ws, service=service, user=ALICE), 3)
    ws.close.assert_awaited()
    assert limiter._held == {}


@pytest.mark.asyncio
async def test_ws_handler_cancel_propagates_and_frees_everything(limiter):
    started = asyncio.Event()

    def stream(*args, **kwargs):
        async def gen():
            started.set()
            await asyncio.sleep(30)
            yield "never\n"

        return gen()

    service = _service()
    service.stream_graph = stream
    ws = _ws([FRAME])
    ws.receive_text.side_effect = _frames_then(json.dumps(FRAME), _forever)
    before = asyncio.all_tasks()
    task = asyncio.create_task(websocket_graph(websocket=ws, service=service, user=ALICE))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert limiter._held == {}
    assert asyncio.all_tasks() - before - {task} == set()


async def _forever():
    await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# /v1/graph/live
# ---------------------------------------------------------------------------


def _live_ws(init):
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.receive_text = AsyncMock(return_value=json.dumps(init))
    ws.receive = AsyncMock(side_effect=_forever)
    ws.send_bytes = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


def _live_service(events=None, hold=None):
    service = _service()
    service.is_live_agent = True
    captured = {}

    async def realtime(queue, init, user):
        captured["started"] = True
        if hold is not None:
            captured.update(held=dict(hold._held))
        for event in events or []:
            yield event
        await asyncio.sleep(30)

    service.realtime_graph = realtime
    service._captured = captured
    return service


@pytest.mark.asyncio
async def test_live_session_takes_the_thread_lock_and_a_run_slot(limiter):
    service = _live_service(hold=limiter)
    ws = _live_ws({"model": "m", "thread_id": "t1"})
    task = asyncio.create_task(realtime_graph_ws(websocket=ws, service=service, user=ALICE))
    await _until(lambda: service._captured.get("started"))
    assert service._captured["held"] == {THREAD_KEY: 1, SLOT_KEY: 1}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert limiter._held == {}


@pytest.mark.asyncio
async def test_live_session_on_a_busy_thread_is_refused(limiter):
    held = await thread_lock.acquire_thread_lock("t1")
    service = _live_service()
    ws = _live_ws({"model": "m", "thread_id": "t1"})
    await realtime_graph_ws(websocket=ws, service=service, user=ALICE)
    assert "started" not in service._captured
    sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
    assert sent[0]["code"] == "thread_busy"
    ws.close.assert_awaited()
    assert limiter._held == {THREAD_KEY: 1}
    held.release()


@pytest.mark.asyncio
async def test_live_session_over_the_run_cap_is_refused(limiter):
    held = await run_limits.acquire_run_slot(ALICE, RunsConfig(max_concurrent_per_user=1))
    service = _live_service()
    ws = _live_ws({"model": "m", "thread_id": "t1"})
    await realtime_graph_ws(websocket=ws, service=service, user=ALICE)
    assert "started" not in service._captured
    sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
    assert sent[0]["code"] == "too_many_runs"
    assert limiter._held == {SLOT_KEY: 1}
    held.release()


@pytest.mark.asyncio
async def test_live_handler_cancel_propagates_and_leaves_no_child_tasks(limiter):
    service = _live_service()
    ws = _live_ws({"model": "m", "thread_id": "t1"})
    before = asyncio.all_tasks()
    task = asyncio.create_task(realtime_graph_ws(websocket=ws, service=service, user=ALICE))
    await _until(lambda: service._captured.get("started"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert asyncio.all_tasks() - before - {task} == set()
    assert limiter._held == {}


# ---------------------------------------------------------------------------
# POST /v1/graph/stream: interrupt_id mismatch is a 409 before the stream starts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_interrupt_mismatch_is_a_real_409_and_frees_the_guard(limiter):
    service = _service()
    service.check_resume_target = AsyncMock(side_effect=HTTPException(409, "wrong interrupt"))
    service.stream_graph = MagicMock()
    graph_input = GraphInputSchema(
        config={"thread_id": "t1"}, resume={"ok": True}, interrupt_id="i-1"
    )
    with pytest.raises(HTTPException) as exc:
        await stream_graph(graph_input=graph_input, service=service, user=ALICE)
    assert exc.value.status_code == 409
    service.stream_graph.assert_not_called()
    assert limiter._held == {}


@pytest.mark.asyncio
async def test_check_resume_target_answers_409_for_the_wrong_interrupt(monkeypatch):
    from .test_resume_binding_and_limits import USER, _paused_at, _resume
    from .test_resume_binding_and_limits import _service as real

    _paused_at(monkeypatch, "i-2")
    srv = real(state=object())
    with pytest.raises(HTTPException) as exc:
        await srv.check_resume_target(_resume(interrupt_id="i-1", config={"thread_id": "t1"}), USER)
    assert exc.value.status_code == 409
    assert "does not match the open interrupt 'i-2'" in exc.value.detail

    await srv.check_resume_target(_resume(interrupt_id="i-2", config={"thread_id": "t1"}), USER)
    with pytest.raises(HTTPException) as new_thread:  # no thread named: nothing to resume
        await srv.check_resume_target(_resume(interrupt_id="i-2"), USER)
    assert new_thread.value.status_code == 409


@pytest.mark.asyncio
async def test_check_resume_target_reads_nothing_without_an_interrupt_id():
    from .test_resume_binding_and_limits import USER, _resume
    from .test_resume_binding_and_limits import _service as real

    srv = real(state=object())
    await srv.check_resume_target(_resume(config={"thread_id": "t1"}), USER)
    srv.checkpointer.aget_state.assert_not_awaited()
