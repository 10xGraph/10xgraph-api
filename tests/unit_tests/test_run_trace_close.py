"""The run-trace store closes every run it opens, and evicts threads consistently (G-134)."""

# ruff: noqa: S101

import asyncio
from unittest.mock import MagicMock

import pytest
from tenxgraph.core.state import StreamChunk, StreamEvent

from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.utils import telemetry_store
from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore

from .test_run_limits import ALICE, _input, _service


def test_eviction_keeps_the_dict_and_the_order_in_step(monkeypatch):
    monkeypatch.setattr(telemetry_store, "MAX_THREADS", 5)
    store = TelemetryStore()
    for i in range(12):
        store.start_run(f"t{i}", "r", 1.0)
    assert len(store._threads) == 5
    assert set(store._threads) == set(store._thread_order)
    assert set(store._threads) == {f"t{i}" for i in range(7, 12)}


def _hanging_graph(started: asyncio.Event):
    graph = MagicMock()

    async def astream(*args, **kwargs):
        yield StreamChunk(event=StreamEvent.UPDATES, data={"status": "node_invoked"})
        started.set()
        await asyncio.sleep(30)

    async def ainvoke(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)

    graph.astream = astream
    graph.ainvoke = ainvoke
    return graph


def _traced_service(started):
    service = _service(_hanging_graph(started), timeout=None)
    service._telemetry = TelemetryStore()
    return service


def _graph_input():
    return GraphInputSchema(messages=_input().messages, config={"thread_id": "t1"})


@pytest.mark.asyncio
async def test_a_cancelled_stream_closes_its_run():
    started = asyncio.Event()
    service = _traced_service(started)

    async def consume():
        async for _ in service.stream_chunks(_graph_input(), ALICE):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = service._telemetry.get_latest_run("t1")
    assert run.status == "stopped"
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_a_stream_the_client_closes_closes_its_run():
    started = asyncio.Event()
    service = _traced_service(started)
    stream = service.stream_chunks(_graph_input(), ALICE)
    await anext(stream)
    await stream.aclose()
    assert service._telemetry.get_latest_run("t1").status == "stopped"


@pytest.mark.asyncio
async def test_a_cancelled_invoke_closes_its_run():
    started = asyncio.Event()
    service = _traced_service(started)
    task = asyncio.create_task(service.invoke_graph(_graph_input(), ALICE))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service._telemetry.get_latest_run("t1").status == "stopped"
