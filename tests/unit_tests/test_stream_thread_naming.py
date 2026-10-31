"""Thread naming on the stream path stays off the critical path (G-102)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from injectq import InjectQ
from tenxgraph.core.state import Message, StreamChunk, StreamEvent
from tenxgraph.core.state.message_block import TextBlock

from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.leases import LeaseManager
from tenxgraph_api.src.app.routers.graph import thread_lock
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services import graph_service

from .test_run_limits import ALICE, _input, _service


@pytest.fixture(autouse=True)
def no_task_manager(monkeypatch):
    """No core task manager bound: naming runs on the service's own tasks."""
    container = InjectQ()
    monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: container))


@pytest.fixture
def locks(monkeypatch):
    limiter = LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(thread_lock, "lease_manager", lambda: limiter)
    return limiter


def _naming_service(generate_name):
    graph = MagicMock()

    async def astream(*args, **kwargs):
        yield StreamChunk(
            event=StreamEvent.MESSAGE,
            message=Message(role="assistant", content=[TextBlock(text="hello")]),
        )

    graph.astream = astream
    service = _service(graph, timeout=None)
    service.config.thread_name_generator_path = "pkg:namer"
    service.checkpointer.aget_thread_owner = AsyncMock(return_value=None)
    service.thread_name_generator = MagicMock()
    service.thread_name_generator.generate_name = generate_name
    service.check_media_urls = MagicMock()
    return service


async def _drain(service):
    tasks = list(service.__dict__.get("_naming_tasks", ()))
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_a_slow_namer_does_not_hold_the_stream_or_the_lock(locks):
    release_name = asyncio.Event()

    async def slow_name(messages):
        await release_name.wait()
        return "named"

    service = _naming_service(slow_name)
    graph_input = GraphInputSchema(messages=_input().messages)  # no thread id: a new thread
    chunks = await asyncio.wait_for(_collect(service.stream_chunks(graph_input, ALICE)), timeout=2)
    assert [c.event for c in chunks] == [StreamEvent.MESSAGE]
    assert not locks._held  # the thread lock is already free
    release_name.set()
    await _drain(service)
    saved = [c.args[1] for c in service.checkpointer.aput_thread.await_args_list]
    assert any(info.thread_name == "named" for info in saved)


@pytest.mark.asyncio
async def test_a_naming_failure_does_not_turn_a_good_run_into_an_error(locks):
    service = _naming_service(AsyncMock(side_effect=RuntimeError("llm down")))
    graph_input = GraphInputSchema(messages=_input().messages)
    chunks = await _collect(service.stream_chunks(graph_input, ALICE))
    await _drain(service)
    assert StreamEvent.ERROR not in [c.event for c in chunks]


@pytest.mark.asyncio
async def test_naming_tasks_are_capped(locks, monkeypatch):
    monkeypatch.setattr(graph_service, "MAX_PENDING_THREAD_NAMES", 2)
    gate = asyncio.Event()

    async def blocked(messages):
        await gate.wait()
        return "n"

    service = _naming_service(blocked)
    for i in range(5):
        service._name_thread_in_background({"thread_id": f"t{i}"}, f"t{i}", ["hi"])
    await asyncio.sleep(0)
    assert service.thread_name_generator.generate_name is blocked
    assert len(service.__dict__["_naming_tasks"]) == 2
    gate.set()
    await service.drain_naming_tasks()
    assert not service.__dict__["_naming_tasks"]


async def _collect(stream):
    return [chunk async for chunk in stream]
