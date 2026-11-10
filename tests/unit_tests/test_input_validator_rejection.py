"""An input validator rejection is the client's mistake: 422 on invoke, an error chunk with
code VALIDATION_ERROR on stream (and so on the WebSocket, which shares the stream), logged at
WARNING."""

# ruff: noqa: S101

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tenxgraph.core.state import StreamEvent
from tenxgraph.utils.validators import ValidationError as InputValidationError

from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema

from .test_run_limits import ALICE, _input, _service


def _rejecting_service():
    graph = MagicMock()
    error = InputValidationError("Validation violation: injection_pattern", "injection_pattern")

    async def fail(*args, **kwargs):
        raise error

    async def fail_stream(*args, **kwargs):
        raise error
        yield  # pragma: no cover - makes this an async generator

    graph.ainvoke = fail
    graph.astream = fail_stream
    return _service(graph, timeout=None)


def _graph_input():
    return GraphInputSchema(messages=_input().messages, config={"thread_id": "t1"})


def test_invoke_answers_422_not_500(caplog):
    service = _rejecting_service()
    app = FastAPI()
    init_errors_handler(app)

    @app.post("/invoke")
    async def invoke():
        return await service.invoke_graph(_graph_input(), ALICE)

    with caplog.at_level(logging.WARNING), TestClient(app, raise_server_exceptions=False) as c:
        response = c.post("/invoke")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_stream_sends_a_validation_error_chunk_and_logs_a_warning(caplog):
    service = _rejecting_service()
    with caplog.at_level(logging.WARNING):
        chunks = [c async for c in service.stream_chunks(_graph_input(), ALICE)]

    assert chunks[-1].event == StreamEvent.ERROR
    assert chunks[-1].data["code"] == "VALIDATION_ERROR"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r for r in caplog.records if r.levelno == logging.WARNING]
