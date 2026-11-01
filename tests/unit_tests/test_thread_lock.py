"""Per-thread run lock on invoke, stream, WebSocket and AG-UI, and production error chunks."""

import asyncio
import gc
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, WebSocketDisconnect
from pydantic import ValidationError
from tenxgraph.core.state import StreamChunk, StreamEvent

from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.core.leases import LeaseManager
from tenxgraph_api.src.app.routers.graph import thread_lock
from tenxgraph_api.src.app.routers.graph.router import invoke_graph, stream_graph, websocket_graph
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
    WsGraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService


ALICE = {"user_id": "alice"}
SECRET = "redis://:SECRET@10.0.0.6"
MSG = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


@pytest.fixture(autouse=True)
def fresh_locks(monkeypatch):
    limiter = LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(thread_lock, "lease_manager", lambda: limiter)
    monkeypatch.setattr(thread_lock, "THREAD_BUSY_WAIT_SECONDS", 0.3)
    return limiter


def _input(thread_id="t1", **extra):
    return GraphInputSchema(messages=MSG, config={"thread_id": thread_id}, **extra)


def _request():
    request = MagicMock()
    request.state = SimpleNamespace(request_id="r", timestamp="t")
    request.is_disconnected = AsyncMock(return_value=False)
    return request


def _service(invoke):
    service = MagicMock()
    service.runs_policy.return_value = RunsConfig()
    service.invoke_graph = invoke
    service.check_resume_target = AsyncMock()
    service.stop_graph = AsyncMock(return_value={"ok": True})
    return service


def _call(service, graph_input):
    return invoke_graph(request=_request(), graph_input=graph_input, service=service, user=ALICE)


# ---------------------------------------------------------------------------
# invoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_invoke_on_a_thread_waits_for_the_first(fresh_locks):
    order, release_first = [], asyncio.Event()

    async def invoke(graph_input, user):
        order.append("start")
        if len(order) == 1:
            await release_first.wait()
        order.append("end")
        return {"messages": []}

    service = _service(invoke)
    first = asyncio.create_task(_call(service, _input()))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(_call(service, _input()))
    await asyncio.sleep(0.1)
    assert order == ["start"]  # the second one has not started
    release_first.set()
    await asyncio.gather(first, second)
    assert order == ["start", "end", "start", "end"]
    assert fresh_locks._held == {}


@pytest.mark.asyncio
async def test_busy_thread_answers_409_after_the_wait_limit(fresh_locks):
    block = asyncio.Event()

    async def invoke(graph_input, user):
        await block.wait()
        return {"messages": []}

    service = _service(invoke)
    first = asyncio.create_task(_call(service, _input()))
    await asyncio.sleep(0.02)
    with pytest.raises(HTTPException) as exc:
        await _call(service, _input())
    assert exc.value.status_code == 409
    assert exc.value.detail == "thread is busy"
    service.stop_graph.assert_not_awaited()  # wait mode never stops the other run
    block.set()
    await first


@pytest.mark.asyncio
async def test_on_busy_cancel_stops_the_running_run_then_runs(fresh_locks):
    stopped, ran = asyncio.Event(), []

    async def invoke(graph_input, user):
        ran.append(graph_input.on_busy)
        if len(ran) == 1:
            await stopped.wait()
        return {"messages": []}

    service = _service(invoke)

    async def stop(thread_id, user, config=None):
        assert thread_id == "t1"
        stopped.set()
        return {"ok": True}

    service.stop_graph = stop
    first = asyncio.create_task(_call(service, _input()))
    await asyncio.sleep(0.02)
    await _call(service, _input(on_busy="cancel"))
    await first
    assert ran == ["wait", "cancel"]


@pytest.mark.asyncio
async def test_on_busy_cancel_on_an_idle_thread_just_runs(fresh_locks):
    service = _service(AsyncMock(return_value={"messages": []}))
    await _call(service, _input(on_busy="cancel"))
    service.stop_graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_different_threads_run_in_parallel(fresh_locks):
    both_running, running = asyncio.Event(), []

    async def invoke(graph_input, user):
        running.append(graph_input.config["thread_id"])
        if len(running) == 2:
            both_running.set()
        await asyncio.wait_for(both_running.wait(), 1)
        return {"messages": []}

    service = _service(invoke)
    await asyncio.gather(_call(service, _input("a")), _call(service, _input("b")))
    assert sorted(running) == ["a", "b"]


@pytest.mark.asyncio
async def test_lock_is_released_when_the_run_fails(fresh_locks):
    service = _service(AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        await _call(service, _input())
    assert fresh_locks._held == {}


@pytest.mark.asyncio
async def test_new_thread_request_takes_no_route_lock(fresh_locks):
    seen = []

    async def invoke(graph_input, user):
        seen.append(dict(fresh_locks._held))
        return {"messages": []}

    await _call(_service(invoke), GraphInputSchema(messages=MSG))
    assert seen == [{}]  # the service locks a new thread once it has an id


def test_on_busy_is_validated():
    assert GraphInputSchema(messages=MSG).on_busy == "wait"
    with pytest.raises(ValidationError):
        GraphInputSchema(messages=MSG, on_busy="queue")
    ws = WsGraphInputSchema(messages=MSG, on_busy="cancel")
    assert ws.to_graph_input().on_busy == "cancel"


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------


async def _items(*values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_stream_route_answers_409_before_the_response_starts(fresh_locks):
    service = _service(None)
    service.stream_graph = MagicMock(side_effect=lambda *a, **k: _items("a"))
    response = await stream_graph(graph_input=_input(), service=service, user=ALICE)
    with pytest.raises(HTTPException) as exc:
        await stream_graph(graph_input=_input(), service=service, user=ALICE)
    assert exc.value.status_code == 409
    assert [c async for c in response.body_iterator] == ["a"]
    assert fresh_locks._held == {}


@pytest.mark.asyncio
async def test_stream_lock_is_released_on_disconnect(fresh_locks):
    service = _service(None)
    service.stream_graph = MagicMock(side_effect=lambda *a, **k: _items("a", "b"))
    response = await stream_graph(graph_input=_input(), service=service, user=ALICE)
    assert await response.body_iterator.__anext__() == "a"
    assert fresh_locks.held(thread_lock.thread_lock_key("t1")) == 1
    await response.body_iterator.aclose()  # what Starlette does when the client goes away
    assert fresh_locks._held == {}


@pytest.mark.asyncio
async def test_stream_slot_refusal_gives_the_thread_back(fresh_locks, monkeypatch):
    from tenxgraph_api.src.app.routers.graph import run_limits

    slots = LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(run_limits, "lease_manager", lambda: slots)
    service = _service(None)
    service.runs_policy.return_value = RunsConfig(max_concurrent_per_user=1)
    service.stream_graph = MagicMock(side_effect=lambda *a, **k: _items("a"))
    first = await stream_graph(graph_input=_input("a"), service=service, user=ALICE)
    with pytest.raises(HTTPException) as exc:
        await stream_graph(graph_input=_input("b"), service=service, user=ALICE)
    assert exc.value.status_code == 429
    assert fresh_locks.held(thread_lock.thread_lock_key("b")) == 0
    [c async for c in first.body_iterator]


@pytest.mark.asyncio
async def test_service_locks_a_new_thread_until_the_stream_ends(fresh_locks):
    held_during = []

    async def astream(*args, **kwargs):
        held_during.append(dict(fresh_locks._held))
        yield StreamChunk(event=StreamEvent.UPDATES, data={"step": 1})

    service = _graph_service(astream)
    service._new_thread_id = AsyncMock(return_value="fresh-1")
    chunks = [c async for c in service.stream_chunks(GraphInputSchema(messages=MSG), ALICE)]
    assert len(chunks) == 1
    assert held_during == [{"runs:thread:fresh-1": 1}]
    assert fresh_locks._held == {}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


def _ws(frames):
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(
        side_effect=[json.dumps(f) if isinstance(f, dict) else f for f in frames]
    )
    return ws


@pytest.mark.asyncio
async def test_ws_busy_thread_sends_an_error_chunk_and_stays_open(fresh_locks):
    held = await thread_lock.acquire_thread_lock("t1")
    service = _service(None)
    service.is_live_agent = False
    service.stream_graph = MagicMock(side_effect=lambda *a, **k: _items("ok\n"))
    frame = {"invoke_type": "fresh", "messages": MSG, "config": {"thread_id": "t1"}}
    ws = _ws([frame, WebSocketDisconnect()])
    await websocket_graph(websocket=ws, service=service, user={})
    sent = [c.args[0] for c in ws.send_text.call_args_list]
    chunk = json.loads(sent[0])
    assert chunk["event"] == "error"
    assert chunk["data"] == {"reason": "thread is busy"}
    assert len(sent) == 1  # no run and no "done" chunk
    ws.close.assert_not_awaited()
    service.stream_graph.assert_not_called()
    held.release()


@pytest.mark.asyncio
async def test_ws_run_releases_the_lock_and_next_run_works(fresh_locks):
    service = _service(None)
    service.is_live_agent = False
    service.stream_graph = MagicMock(side_effect=lambda *a, **k: _items("ok\n"))
    frame = {"invoke_type": "fresh", "messages": MSG, "config": {"thread_id": "t1"}}
    ws = _ws([frame, frame, WebSocketDisconnect()])
    await websocket_graph(websocket=ws, service=service, user={})
    assert service.stream_graph.call_count == 2
    assert fresh_locks._held == {}


# ---------------------------------------------------------------------------
# AG-UI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ag_ui_busy_thread_answers_409_and_waits_by_default(fresh_locks):
    pytest.importorskip("ag_ui")
    from tenxgraph_api.src.app.routers.ag_ui.router import run_ag_ui

    body = {
        "threadId": "t1",
        "runId": "r1",
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
    }
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    request.headers = {}
    config = SimpleNamespace(
        auth_config=lambda: None, ag_ui=SimpleNamespace(allow_client_tools=True)
    )
    service = _service(None)
    service.check_media_urls = MagicMock()
    held = await thread_lock.acquire_thread_lock("t1")
    with pytest.raises(HTTPException) as exc:
        await run_ag_ui(request, service, ALICE, MagicMock(), config)
    assert exc.value.status_code == 409
    assert exc.value.detail == "thread is busy"
    service.stop_graph.assert_not_awaited()
    held.release()
    response = await run_ag_ui(request, service, ALICE, MagicMock(), config)
    assert fresh_locks.held(thread_lock.thread_lock_key("t1")) == 1
    del response  # never iterated: the finalizer gives the lock back
    gc.collect()
    assert fresh_locks._held == {}


# ---------------------------------------------------------------------------
# Lock shared through the cache store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_is_shared_between_workers_through_redis(monkeypatch):
    shared = InMemoryCacheStore()  # one store behind two workers, as Redis is
    worker_a = LeaseManager(shared, lease_ttl=60)
    worker_b = LeaseManager(shared, lease_ttl=60)
    monkeypatch.setattr(thread_lock, "THREAD_BUSY_WAIT_SECONDS", 0.2)

    monkeypatch.setattr(thread_lock, "lease_manager", lambda: worker_a)
    lease = await thread_lock.acquire_thread_lock("t1")
    assert list(shared._slots) == ["concurrency:runs:thread:t1"]

    monkeypatch.setattr(thread_lock, "lease_manager", lambda: worker_b)
    with pytest.raises(HTTPException) as exc:
        await thread_lock.acquire_thread_lock("t1")
    assert exc.value.status_code == 409
    other = await thread_lock.acquire_thread_lock("t2")  # another thread is free
    lease.release()
    await asyncio.sleep(0.01)
    again = await thread_lock.acquire_thread_lock("t1")
    for held in (other, again):
        held.release()
    await worker_a.aclose()
    await worker_b.aclose()


@pytest.mark.asyncio
async def test_crashed_worker_lock_expires(monkeypatch):
    limiter = LeaseManager(InMemoryCacheStore(), lease_ttl=0.2)
    monkeypatch.setattr(thread_lock, "lease_manager", lambda: limiter)
    monkeypatch.setattr(thread_lock, "THREAD_BUSY_WAIT_SECONDS", 2)
    lease = await thread_lock.acquire_thread_lock("t1")
    for task in list(limiter._renewers):  # the worker dies: nothing renews the lease
        task.cancel()
    again = await thread_lock.acquire_thread_lock("t1")
    again.release()
    lease.release()
    await limiter.aclose()


# ---------------------------------------------------------------------------
# Production error chunks
# ---------------------------------------------------------------------------


def _graph_service(astream):
    graph = MagicMock()
    graph.astream = astream
    graph.is_realtime = lambda: False
    service = GraphService.__new__(GraphService)
    service._graph = graph
    service.config = SimpleNamespace(runs=RunsConfig())
    service.checkpointer = MagicMock()
    service.checkpointer.aput_thread = AsyncMock(return_value=True)
    service.checkpointer.aget_state = AsyncMock(return_value=None)
    service.thread_name_generator = None
    service._media_service = None
    service._telemetry = None
    return service


def _failing_astream(*args, **kwargs):
    async def gen():
        yield StreamChunk(
            event=StreamEvent.ERROR,
            data={
                "status": "invoked_graph",
                "node": "N",
                "reason": f"[NODE_001] Error in node 'N': {SECRET} refused",
            },
        )
        yield StreamChunk(
            event=StreamEvent.ERROR,
            data={
                "status": "tool_failed",
                "tool_call_id": "c1",
                "reason": "Tool execution resulted in error",
                "error": f"ConnectionError: {SECRET}",
                "message": f"tool said {SECRET}",
            },
        )

    return gen()


def _mode(monkeypatch, mode):
    from tenxgraph_api.src.app.core.config import settings

    monkeypatch.setattr(
        settings, "get_settings", lambda: SimpleNamespace(MODE=mode, MAX_REQUEST_SIZE=10_000_000)
    )


async def _ws_output(service):
    ws = _ws(
        [
            {"invoke_type": "fresh", "messages": MSG, "config": {"thread_id": "t1"}},
            WebSocketDisconnect(),
        ]
    )
    await websocket_graph(websocket=ws, service=service, user={})
    return " ".join(c.args[0] for c in ws.send_text.call_args_list)


async def _ag_ui_output(service):
    pytest.importorskip("ag_ui")
    from ag_ui.core import RunAgentInput
    from ag_ui.encoder import EventEncoder

    from tenxgraph_api.src.app.routers.ag_ui.service import stream_ag_ui

    run_input = RunAgentInput(
        thread_id="t1",
        run_id="r1",
        state={},
        messages=[{"id": "m1", "role": "user", "content": "hi"}],
        tools=[],
        context=[],
        forwarded_props={},
    )
    events = stream_ag_ui(service, run_input, ALICE, EventEncoder())
    return "".join([e async for e in events])


@pytest.mark.asyncio
async def test_production_hides_error_text_on_stream_ws_and_ag_ui(monkeypatch):
    _mode(monkeypatch, "production")
    service = _graph_service(_failing_astream)

    stream = "".join([c async for c in service.stream_graph(_input(), ALICE)])
    assert "SECRET" not in stream
    assert "An error occurred in a graph node." in stream

    assert "SECRET" not in await _ws_output(service)
    assert "SECRET" not in await _ag_ui_output(service)


@pytest.mark.asyncio
async def test_production_scrubs_the_error_in_a_chunk_state(monkeypatch):
    from tenxgraph.core.state import AgentState

    from tenxgraph_api.src.app.routers.graph.services.graph_service import sanitize_error_chunk

    _mode(monkeypatch, "production")
    state = AgentState()
    state.error(f"boom {SECRET}")
    chunk = StreamChunk(event=StreamEvent.ERROR, state=state, data={"reason": f"x {SECRET}"})
    clean = sanitize_error_chunk(chunk)
    assert SECRET not in clean.model_dump_json()
    assert SECRET in state.execution_meta.internal_data["error"]  # the run's own state is intact


@pytest.mark.asyncio
async def test_production_keeps_core_generic_tool_failure_reference(monkeypatch):
    from tenxgraph_api.src.app.routers.graph.services.graph_service import sanitize_error_chunk

    _mode(monkeypatch, "production")
    text = "Tool 'db' failed with an internal error (reference ab12cd34)."
    chunk = StreamChunk(event=StreamEvent.ERROR, data={"error": text})
    assert sanitize_error_chunk(chunk).data["error"] == text


@pytest.mark.asyncio
async def test_development_still_shows_error_text(monkeypatch):
    _mode(monkeypatch, "development")
    service = _graph_service(_failing_astream)
    stream = "".join([c async for c in service.stream_graph(_input(), ALICE)])
    assert SECRET in stream
    assert SECRET in await _ws_output(service)
    assert SECRET in await _ag_ui_output(service)


# ---------------------------------------------------------------------------
# G-105: Retry-After and a waiter whose client left
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_busy_409_and_run_cap_429_carry_retry_after(fresh_locks, monkeypatch):
    held = await thread_lock.acquire_thread_lock("t-retry")
    with pytest.raises(HTTPException) as busy:
        await thread_lock.acquire_thread_lock("t-retry")
    assert busy.value.status_code == 409
    assert busy.value.headers["Retry-After"]
    held.release()

    from tenxgraph_api.src.app.routers.graph import run_limits

    limiter = LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(run_limits, "lease_manager", lambda: limiter)
    slot = await run_limits.acquire_run_slot(ALICE, RunsConfig(max_concurrent_per_user=1))
    with pytest.raises(HTTPException) as capped:
        await run_limits.acquire_run_slot(ALICE, RunsConfig(max_concurrent_per_user=1))
    assert capped.value.status_code == 429
    assert capped.value.headers["Retry-After"]
    slot.release()


@pytest.mark.asyncio
async def test_a_waiter_whose_client_left_gives_up_and_takes_nothing(fresh_locks):
    held = await thread_lock.acquire_thread_lock("t-gone")
    gone = AsyncMock(return_value=True)
    with pytest.raises(HTTPException) as exc:
        await thread_lock.acquire_thread_lock("t-gone", is_disconnected=gone)
    assert exc.value.status_code == thread_lock.CLIENT_CLOSED_REQUEST
    held.release()
    # The lock is free again: the abandoned waiter did not queue up behind it.
    again = await thread_lock.acquire_thread_lock("t-gone")
    again.release()


@pytest.mark.asyncio
async def test_a_client_that_left_just_before_the_lock_freed_does_not_start_a_run(fresh_locks):
    held = await thread_lock.acquire_thread_lock("t-late")
    calls = {"n": 0}

    async def disconnected():
        calls["n"] += 1
        return calls["n"] > 1  # present while waiting, gone once the lock is free

    waiter = asyncio.create_task(
        thread_lock.acquire_thread_lock("t-late", is_disconnected=disconnected)
    )
    await asyncio.sleep(0.1)
    held.release()
    with pytest.raises(HTTPException):
        await waiter
    free = await thread_lock.acquire_thread_lock("t-late")  # not left held by the waiter
    free.release()
