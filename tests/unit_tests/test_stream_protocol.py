"""HTTP run stream: content type (G-104), keep-alive (G-104), terminal chunk (G-165)."""

# ruff: noqa: S101

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from tenxgraph.core.state import StreamChunk, StreamEvent

from tenxgraph_api.src.app.core import shutdown
from tenxgraph_api.src.app.routers.graph.router import stream_graph
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService


@pytest.fixture(autouse=True)
def _reset_shutdown():
    shutdown.reset()
    yield
    shutdown.reset()


def _service(chunks) -> GraphService:
    async def stream_chunks(self, graph_input, user, server_config=None):
        for chunk in chunks:
            yield chunk

    service = object.__new__(GraphService)
    service.stream_chunks = stream_chunks.__get__(service)
    return service


async def _lines(service) -> list[dict]:
    return [json.loads(line) async for line in service.stream_graph(None, {})]


@pytest.mark.asyncio
async def test_stream_ends_with_the_done_chunk():
    last = StreamChunk(event=StreamEvent.MESSAGE, data={}, thread_id="t1", run_id="r1")
    out = await _lines(_service([StreamChunk(event=StreamEvent.UPDATES, data={"a": 1}), last]))
    assert out[-1]["event"] == "updates"
    assert out[-1]["data"] == {"status": "done"}
    assert out[-1]["thread_id"] == "t1"
    assert len(out) == 3


@pytest.mark.asyncio
async def test_a_stream_that_errored_has_no_done_chunk():
    out = await _lines(_service([StreamChunk(event=StreamEvent.ERROR, data={"reason": "x"})]))
    assert [c["event"] for c in out] == ["error"]


@pytest.mark.asyncio
async def test_done_chunk_follows_a_pause_update():
    pause = StreamChunk(event=StreamEvent.UPDATES, data={"status": "interrupted"})
    out = await _lines(_service([pause]))
    assert out[-1]["data"] == {"status": "done"}


@pytest.mark.asyncio
async def test_router_labels_the_stream_ndjson():
    graph_input = GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )
    service = MagicMock()
    guard = MagicMock()
    with (
        patch(
            "tenxgraph_api.src.app.routers.graph.router._http_run_guard",
            new=MagicMock(side_effect=lambda *a, **k: _done(guard)),
        ),
        patch(
            "tenxgraph_api.src.app.routers.graph.router.release_when_done",
            side_effect=lambda gen, g: gen,
        ),
    ):
        service.check_resume_target = MagicMock(side_effect=lambda *a, **k: _done(None))
        response = await stream_graph(graph_input=graph_input, service=service, user={})
    assert response.media_type == "application/x-ndjson"


async def _done(value):
    return value


@pytest.mark.asyncio
async def test_idle_stream_gets_keep_alive_lines():
    async def stream():
        yield "a\n"
        await asyncio.sleep(0.35)
        yield "b\n"

    lines = [line async for line in shutdown.end_on_shutdown(stream(), keepalive_seconds=0.1)]
    assert lines[0] == "a\n"
    assert lines[-1] == "b\n"
    assert shutdown.KEEPALIVE_LINE in lines[1:-1]
    assert len([line for line in lines if line == shutdown.KEEPALIVE_LINE]) >= 2


@pytest.mark.asyncio
async def test_keep_alive_does_not_lose_chunks_or_delay_shutdown():
    async def stream():
        yield "a\n"
        await asyncio.sleep(30)

    lines: list[str] = []

    async def consume():
        async for line in shutdown.end_on_shutdown(stream(), keepalive_seconds=0.05):
            lines.append(line)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    shutdown.interrupt_streams()
    await asyncio.wait_for(task, 2)
    assert json.loads(lines[-1])["data"]["interrupted"] is True


def test_keep_alive_line_is_a_blank_line():
    assert shutdown.KEEPALIVE_LINE == "\n"
