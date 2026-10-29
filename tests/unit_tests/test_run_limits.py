"""Per-user run cap and run timeout for graph runs."""

# ruff: noqa: S101

import asyncio
import gc
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from tenxgraph.core.state import StreamChunk, StreamEvent

from tenxgraph_api.src.app.core.concurrency import MemoryConcurrencyLimiter
from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.routers.graph import run_limits
from tenxgraph_api.src.app.routers.graph.run_limits import (
    acquire_run_slot,
    release_when_done,
    run_slot,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import (
    GraphService,
    RunTimeoutError,
    _bounded_stream,
)


ALICE = {"user_id": "alice"}


class _Slots(MemoryConcurrencyLimiter):
    def held(self, user_id):  # tests count by user id
        return super().held(run_limits.run_slot_key(user_id))


@pytest.fixture(autouse=True)
def fresh_slots(monkeypatch):
    slots = _Slots()
    monkeypatch.setattr(run_limits, "concurrency_limiter", lambda: slots)
    return slots


# ---------------------------------------------------------------------------
# slots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_is_enforced_per_user(fresh_slots):
    runs = RunsConfig(max_concurrent_per_user=2)
    first = await acquire_run_slot(ALICE, runs)
    await acquire_run_slot(ALICE, runs)
    with pytest.raises(HTTPException) as exc:
        await acquire_run_slot(ALICE, runs)
    assert exc.value.status_code == 429
    # Another user has their own budget.
    await acquire_run_slot({"user_id": "bob"}, runs)
    # Releasing frees a slot; releasing twice does not free two.
    first.release()
    first.release()
    assert fresh_slots.held("alice") == 1
    await acquire_run_slot(ALICE, runs)
    with pytest.raises(HTTPException):
        await acquire_run_slot(ALICE, runs)


@pytest.mark.parametrize(("user", "limit"), [({}, 1), ({"user_id": ""}, 1), (ALICE, None)])
@pytest.mark.asyncio
async def test_anonymous_or_unlimited_runs_are_not_counted(fresh_slots, user, limit):
    runs = RunsConfig(max_concurrent_per_user=limit)
    for _ in range(5):
        await acquire_run_slot(user, runs)
    assert fresh_slots._held == {}


@pytest.mark.asyncio
async def test_run_slot_context_releases_on_error(fresh_slots):
    runs = RunsConfig(max_concurrent_per_user=1)
    with pytest.raises(RuntimeError):
        async with run_slot(ALICE, runs):
            assert fresh_slots.held("alice") == 1
            raise RuntimeError("boom")
    assert fresh_slots.held("alice") == 0


async def _items(*values, fail=None):
    for value in values:
        yield value
    if fail:
        raise fail


@pytest.mark.asyncio
async def test_stream_releases_after_normal_end(fresh_slots):
    slot = await acquire_run_slot(ALICE, RunsConfig(max_concurrent_per_user=1))
    assert [x async for x in release_when_done(_items(1, 2), slot)] == [1, 2]
    assert fresh_slots.held("alice") == 0


@pytest.mark.asyncio
async def test_stream_releases_after_error(fresh_slots):
    slot = await acquire_run_slot(ALICE, RunsConfig(max_concurrent_per_user=1))
    with pytest.raises(ValueError):
        async for _ in release_when_done(_items(1, fail=ValueError("x")), slot):
            pass
    assert fresh_slots.held("alice") == 0


@pytest.mark.asyncio
async def test_stream_releases_on_disconnect(fresh_slots):
    slot = await acquire_run_slot(ALICE, RunsConfig(max_concurrent_per_user=1))
    stream = release_when_done(_items(1, 2, 3), slot)
    assert await stream.__anext__() == 1
    await stream.aclose()  # what Starlette does when the client goes away
    assert fresh_slots.held("alice") == 0


@pytest.mark.asyncio
async def test_stream_never_iterated_releases_when_dropped(fresh_slots):
    slot = await acquire_run_slot(ALICE, RunsConfig(max_concurrent_per_user=1))
    stream = release_when_done(_items(1), slot)
    assert fresh_slots.held("alice") == 1
    del stream
    gc.collect()
    assert fresh_slots.held("alice") == 0


# ---------------------------------------------------------------------------
# routes take the slot before running
# ---------------------------------------------------------------------------


def _input():
    return GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )


@pytest.mark.asyncio
async def test_second_concurrent_invoke_gets_429(fresh_slots):
    from tenxgraph_api.src.app.routers.graph.router import invoke_graph

    started, finish = asyncio.Event(), asyncio.Event()

    async def slow_invoke(graph_input, user):
        started.set()
        await finish.wait()
        return {"messages": []}

    service = MagicMock()
    service.runs_policy.return_value = RunsConfig(max_concurrent_per_user=1)
    service.invoke_graph = slow_invoke
    request = MagicMock()
    request.state = SimpleNamespace(request_id="r", timestamp="t")

    first = asyncio.create_task(
        invoke_graph(request=request, graph_input=_input(), service=service, user=ALICE)
    )
    await started.wait()
    with pytest.raises(HTTPException) as exc:
        await invoke_graph(request=request, graph_input=_input(), service=service, user=ALICE)
    assert exc.value.status_code == 429
    finish.set()
    await first
    assert fresh_slots.held("alice") == 0


@pytest.mark.asyncio
async def test_stream_route_refuses_before_streaming(fresh_slots):
    from tenxgraph_api.src.app.routers.graph.router import stream_graph

    service = MagicMock()
    service.runs_policy.return_value = RunsConfig(max_concurrent_per_user=1)
    service.check_resume_target = AsyncMock()
    service.stream_graph = MagicMock(return_value=_items("a"))

    response = await stream_graph(graph_input=_input(), service=service, user=ALICE)
    with pytest.raises(HTTPException) as exc:
        await stream_graph(graph_input=_input(), service=service, user=ALICE)
    assert exc.value.status_code == 429
    # Draining the first stream frees the slot.
    assert [c async for c in response.body_iterator] == ["a"]
    assert fresh_slots.held("alice") == 0


# ---------------------------------------------------------------------------
# timeout
# ---------------------------------------------------------------------------


def _service(graph, timeout):
    service = GraphService.__new__(GraphService)
    service._graph = graph
    service.config = SimpleNamespace(
        runs=RunsConfig(timeout=timeout), thread_name_generator_path=None
    )
    service.checkpointer = MagicMock()
    service.checkpointer.aput_thread = AsyncMock(return_value=True)
    service.checkpointer.aget_state = AsyncMock(return_value=None)
    service.thread_name_generator = None
    service._media_service = None
    service._telemetry = None
    return service


@pytest.mark.asyncio
async def test_invoke_past_the_timeout_answers_504():
    graph = MagicMock()

    async def hang(*args, **kwargs):
        await asyncio.sleep(5)

    graph.ainvoke = hang
    service = _service(graph, timeout=0.05)
    with pytest.raises(HTTPException) as exc:
        await service.invoke_graph(
            GraphInputSchema(messages=_input().messages, config={"thread_id": "t1"}), ALICE
        )
    assert exc.value.status_code == 504
    assert "runs.timeout limit of 0.05s" in exc.value.detail
    assert "10xgraph.json" in exc.value.detail


@pytest.mark.asyncio
async def test_a_timeout_raised_inside_the_graph_is_not_reported_as_ours():
    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=TimeoutError("upstream LLM timed out"))
    service = _service(graph, timeout=30)
    with pytest.raises(HTTPException) as exc:
        await service.invoke_graph(
            GraphInputSchema(messages=_input().messages, config={"thread_id": "t1"}), ALICE
        )
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_no_timeout_runs_the_graph_directly():
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={"messages": [], "state": None})
    service = _service(graph, timeout=None)
    out = await service._ainvoke_bounded({}, {"thread_id": "t1"}, None)
    assert out == {"messages": [], "state": None}


@pytest.mark.asyncio
async def test_stream_past_the_timeout_ends_with_an_error_chunk():
    async def slow_stream(*args, **kwargs):
        yield StreamChunk(event=StreamEvent.UPDATES, data={"step": 1})
        await asyncio.sleep(5)
        yield StreamChunk(event=StreamEvent.UPDATES, data={"step": 2})

    graph = MagicMock()
    graph.astream = slow_stream
    service = _service(graph, timeout=0.05)
    chunks = [
        c
        async for c in service.stream_chunks(
            GraphInputSchema(messages=_input().messages, config={"thread_id": "t1"}), ALICE
        )
    ]
    assert chunks[0].data == {"step": 1}
    assert chunks[-1].event == StreamEvent.ERROR
    assert "runs.timeout limit of 0.05s" in chunks[-1].data["reason"]
    assert "10xgraph.json" in chunks[-1].data["reason"]


class TestBoundedStream:
    @pytest.mark.asyncio
    async def test_yields_everything_in_order(self):
        assert [x async for x in _bounded_stream(_items(1, 2, 3), 5)] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_hands_over_one_item_at_a_time(self):
        """The graph reuses chunk objects, so it must not run ahead of the consumer."""
        log = []

        async def source():
            for i in range(3):
                log.append(f"produce {i}")
                yield i

        async for item in _bounded_stream(source(), 5):
            await asyncio.sleep(0.01)  # give a runaway producer every chance
            log.append(f"consume {item}")
        assert log == [
            "produce 0",
            "consume 0",
            "produce 1",
            "consume 1",
            "produce 2",
            "consume 2",
        ]

    @pytest.mark.asyncio
    async def test_errors_from_the_stream_propagate(self):
        with pytest.raises(ValueError, match="bad"):
            async for _ in _bounded_stream(_items(1, fail=ValueError("bad")), 5):
                pass

    @pytest.mark.asyncio
    async def test_timeout_cancels_and_closes_the_stream(self):
        closed = asyncio.Event()

        async def source():
            try:
                yield 1
                await asyncio.sleep(5)
                yield 2
            finally:
                closed.set()

        got = []
        with pytest.raises(RunTimeoutError):
            async for item in _bounded_stream(source(), 0.05):
                got.append(item)
        assert got == [1]
        assert closed.is_set()

    @pytest.mark.asyncio
    async def test_consumer_leaving_early_stops_the_stream(self):
        closed = asyncio.Event()

        async def source():
            try:
                for i in range(100):
                    yield i
            finally:
                closed.set()

        stream = _bounded_stream(source(), 5)
        assert await stream.__anext__() == 0
        await stream.aclose()
        await asyncio.sleep(0)
        assert closed.is_set()


@pytest.mark.asyncio
async def test_the_inner_stream_is_closed_before_the_slot_is_released(fresh_slots):
    """A disconnected client's run must stop before its slot is reused."""
    events = []

    async def run():
        try:
            yield "a"
            yield "b"
        finally:
            events.append(("closed", fresh_slots.held("alice")))

    slot = await acquire_run_slot(ALICE, RunsConfig(max_concurrent_per_user=1))
    stream = release_when_done(run(), slot)
    assert await stream.__anext__() == "a"
    await stream.aclose()
    assert events == [("closed", 1)]  # closed while the slot was still held
    assert fresh_slots.held("alice") == 0


class TestBoundedStreamTimeouts:
    @pytest.mark.asyncio
    async def test_a_timeout_from_inside_the_graph_is_not_reported_as_ours(self):
        async def source():
            yield 1
            raise TimeoutError("provider timed out")

        with pytest.raises(TimeoutError, match="provider timed out") as exc:
            async for _ in _bounded_stream(source(), 30):
                pass
        assert not isinstance(exc.value, RunTimeoutError)
