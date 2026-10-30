"""G-171: the API frees a run's thread only after core saved the cancelled run.

A client that disconnects cancels the response task through a level-triggered anyio scope
(Starlette). The wrappers around the graph stream (``end_on_shutdown``, ``_bounded_stream``)
run the graph in a task of their own; they must wait for that task, not forward a cancel to it
on every loop turn and return early. The store here is slow, so returning early shows up as a
state that is not saved yet.
"""

# ruff: noqa: S101

import asyncio
import time

import anyio
import pytest
from injectq import InjectQ
from injectq.core.context import ContainerContext
from starlette.responses import StreamingResponse
from tenxgraph.core.graph import StateGraph
from tenxgraph.core.state import AgentState, ExecutionStatus, Message
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.storage.checkpointer.base_checkpointer import ACTIVE_RUN_NAMESPACE
from tenxgraph.utils.constants import END

from tenxgraph_api.src.app.core import shutdown
from tenxgraph_api.src.app.core.concurrency import Lease
from tenxgraph_api.src.app.routers.graph.run_limits import release_when_done
from tenxgraph_api.src.app.routers.graph.services.graph_service import _bounded_stream


SAVE_DELAY = 0.3
CFG = {"thread_id": "t1", "user_id": "u1", "run_id": "r1"}


@pytest.fixture(autouse=True)
def _own_container():
    """Each graph here binds its checkpointer; a leftover binding would take the saves."""
    ContainerContext.clear_current()
    InjectQ.reset_instance()
    yield
    ContainerContext.clear_current()
    InjectQ.reset_instance()


class SlowCheckpointer(InMemoryCheckpointer):
    def __init__(self):
        super().__init__()
        self.log: list[str] = []

    async def aput_checkpoint(self, config, state, messages=None, metadata=None):
        status = state.execution_meta.status.value
        await asyncio.sleep(SAVE_DELAY)
        result = await super().aput_checkpoint(config, state, messages, metadata)
        self.log.append(f"saved {status}")
        return result

    async def aend_run(self, config):
        self.log.append("run ended")
        return await super().aend_run(config)


def build():
    cp = SlowCheckpointer()
    started = asyncio.Event()

    async def node(state: AgentState, config: dict):
        started.set()
        await asyncio.sleep(5)
        return [Message.text_message("answer", role="assistant")]

    graph = StateGraph()
    graph.add_node("N", node)
    graph.set_entry_point("N")
    graph.add_edge("N", END)
    return graph.compile(checkpointer=cp), cp, started


def chunks(app):
    return app.astream({"messages": [Message.text_message("hi")]}, dict(CFG))


async def lines(app):
    async for chunk in chunks(app):
        yield chunk.model_dump_json() + "\n"


async def assert_saved(cp):
    state = await cp.aget_state(CFG)
    assert state is not None
    assert state.execution_meta.status == ExecutionStatus.INTERRUPTED_AFTER
    marker = await cp.aget_cache_value(ACTIVE_RUN_NAMESPACE, cp._stop_request_key(CFG))
    assert marker is None


async def cancel_host(host, started) -> tuple[float, float]:
    """Run ``host`` in an anyio task group, cancel it once the run started; (wall, cpu)."""
    async with anyio.create_task_group() as tg:
        tg.start_soon(host)
        await asyncio.wait_for(started.wait(), 5)
        cpu, wall = time.process_time(), time.monotonic()
        tg.cancel_scope.cancel()
    return time.monotonic() - wall, time.process_time() - cpu


@pytest.mark.asyncio
async def test_end_on_shutdown_returns_only_after_the_core_save():
    shutdown.reset()
    app, cp, started = build()

    async def host():
        async for _ in shutdown.end_on_shutdown(lines(app), keepalive_seconds=15):
            pass

    wall, cpu = await cancel_host(host, started)

    await assert_saved(cp)  # at once: nothing left to finish in the background
    assert cp.log[-2:] == ["saved interrupted_after", "run ended"]
    assert wall >= SAVE_DELAY * 0.8
    assert cpu < wall / 2  # no cancel forwarded to the run on every loop turn
    await app.aclose()


@pytest.mark.asyncio
async def test_bounded_stream_returns_only_after_the_core_save():
    app, cp, started = build()

    async def host():
        async for _ in _bounded_stream(chunks(app), 10):
            pass

    wall, cpu = await cancel_host(host, started)

    await assert_saved(cp)
    assert cp.log[-2:] == ["saved interrupted_after", "run ended"]
    assert cpu < wall / 2
    await app.aclose()


@pytest.mark.asyncio
async def test_thread_lock_is_released_only_after_the_save():
    """The REST stream shape: StreamingResponse over release_when_done(end_on_shutdown(...))."""
    shutdown.reset()
    app, cp, started = build()
    at_release: list[list[str]] = []
    released = asyncio.Event()

    def on_release():
        at_release.append(list(cp.log))
        released.set()

    async def stream_graph():
        async for line in shutdown.end_on_shutdown(lines(app), keepalive_seconds=15):
            yield line

    response = StreamingResponse(
        release_when_done(stream_graph(), Lease(on_release)), media_type="application/x-ndjson"
    )

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}
    await response(scope, receive, send)
    await asyncio.wait_for(released.wait(), 5)

    assert len(at_release) == 1
    assert at_release[0][-2:] == ["saved interrupted_after", "run ended"]
    await assert_saved(cp)
    await app.aclose()


@pytest.mark.asyncio
async def test_cancel_and_wait_raises_a_cancel_of_the_caller_once_the_task_is_done():
    job_done = asyncio.Event()

    async def job():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(0.1)  # its own cleanup
            job_done.set()
            raise

    task = asyncio.ensure_future(job())
    await asyncio.sleep(0)
    waiter = asyncio.ensure_future(shutdown.cancel_and_wait(task, timeout=5))
    await asyncio.sleep(0.02)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert job_done.is_set()


@pytest.mark.asyncio
async def test_cancel_and_wait_gives_up_after_the_timeout():
    release = asyncio.Event()

    async def slow_cleanup():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await release.wait()  # cleanup that outlasts the wait
            raise

    task = asyncio.ensure_future(slow_cleanup())
    await asyncio.sleep(0)
    await shutdown.cancel_and_wait(task, timeout=0.05)
    assert not task.done()
    release.set()
    await asyncio.wait({task}, timeout=1)
    assert task.cancelled()
