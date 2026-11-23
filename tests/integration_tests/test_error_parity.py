"""One error, one answer: invoke, stream, WebSocket and AG-UI give the same message.

Real app wiring and a real ``GraphService`` in ``MODE=production``. Each case makes the run
fail with one exception class (raised where the run's input is prepared, inside every
transport's error handling) and checks the client message on all four transports, and the
code on invoke, stream and WebSocket. AG-UI keeps its documented ``RUN_ERROR`` codes
(``GRAPH_ERROR``, ``INVALID_INPUT``, ``AG_UI_ERROR``) with the same message text.
A second part checks that invoke and stream answers carry no provider ``raw`` payload and no
``internal_data`` key other than ``error``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from injectq import InjectQ
from tenxgraph.core.exceptions import (
    GraphClosedError,
    GraphError,
    GraphRecursionError,
    ModelNotFoundError,
    NodeError,
    StorageError,
    ThreadNotFoundError,
    TransientStorageError,
)
from tenxgraph.core.exceptions.media_exceptions import UnsupportedMediaInputError
from tenxgraph.core.graph import CompiledGraph, StateGraph
from tenxgraph.core.state import AgentState, Message, TextBlock
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils.constants import END
from tenxgraph.utils.validators import ValidationError as InputValidationError

from tenxgraph_api.src.app.core.config import settings
from tenxgraph_api.src.app.core.config.graph_config import (
    AgUiConfig,
    GraphConfig,
    RunsConfig,
    WebSocketConfig,
)
from tenxgraph_api.src.app.core.exceptions.client_errors import HANDLED_TYPES, RunTimeoutError
from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler
from tenxgraph_api.src.app.core.exceptions.user_exception import UserPermissionError
from tenxgraph_api.src.app.core.validation.request_config import (
    ClientInputError,
    InterruptConflictError,
)
from tenxgraph_api.src.app.routers.graph.router import router as graph_router
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

from .conftest import build_app, make_client, user_headers


pytest.importorskip("ag_ui")
from tenxgraph_api.src.app.routers.ag_ui import router as ag_ui_router
from tenxgraph_api.src.app.routers.ag_ui import service as ag_ui_service


SECRET = "postgres://admin:hunter2@10.0.0.5/db"
GENERIC_HTTP = "An error occurred processing your request."
MEDIA = UnsupportedMediaInputError(
    provider="openai", model="gpt-x", media_type="audio", source_kind="url"
)
MODEL = ModelNotFoundError("nope", "MAIN", ["a", "b"])

# (exception, status on invoke, code, message the client sees in production)
CASES = {
    "unsupported_media": (MEDIA, 422, "HTTPException", MEDIA.message),
    "client_input": (
        ClientInputError("unknown node 'X'"),
        422,
        "HTTPException",
        "unknown node 'X'",
    ),
    "model_not_found": (MODEL, 422, "HTTPException", str(MODEL)),
    "resume_conflict": (
        InterruptConflictError("Nothing to resume"),
        409,
        "HTTPException",
        "Nothing to resume",
    ),
    "value_error": (ValueError(SECRET), 422, "HTTPException", "Invalid input provided."),
    "unexpected": (RuntimeError(SECRET), 500, "HTTPException", GENERIC_HTTP),
    "run_timeout": (RunTimeoutError(5), 504, "HTTPException", GENERIC_HTTP),
    "http_4xx": (HTTPException(404, "No such thing"), 404, "HTTPException", "No such thing"),
    "http_5xx": (HTTPException(502, SECRET), 502, "HTTPException", GENERIC_HTTP),
    "input_validator": (
        InputValidationError(SECRET, "injection"),
        422,
        "VALIDATION_ERROR",
        "The request data is invalid. Please check your input.",
    ),
    "graph_error": (GraphError(SECRET), 500, "GRAPH_000", "An error occurred executing the graph."),
    "node_error": (NodeError(SECRET), 500, "NODE_000", "An error occurred in a graph node."),
    "recursion": (
        GraphRecursionError(SECRET),
        422,
        GraphRecursionError(SECRET).error_code,
        "An unexpected error occurred. Please contact support.",
    ),
    "graph_closed": (
        GraphClosedError(),
        503,
        "SERVICE_UNAVAILABLE",
        "The server is shutting down. Retry the request.",
    ),
    "thread_not_found": (ThreadNotFoundError("t-1"), 404, "THREAD_NOT_FOUND", "Thread not found"),
    "storage_error": (
        StorageError(SECRET),
        500,
        "STORAGE_000",
        "An error occurred accessing storage.",
    ),
    "transient_storage": (
        TransientStorageError(SECRET),
        503,
        TransientStorageError(SECRET).error_code,
        "An error occurred accessing storage.",
    ),
    "permission": (
        UserPermissionError("Not allowed to do that"),
        403,
        "PERMISSION_ERROR",
        "Not allowed to do that",
    ),
}


class _Config:
    """GraphConfig stand-in: auth on, AG-UI on, no caps, server-made thread ids."""

    rate_limit = None
    websocket = WebSocketConfig(max_connections=None, max_connections_per_user=None)
    ag_ui = AgUiConfig(enabled=True)
    runs = RunsConfig()

    def auth_config(self) -> str:
        return "custom"


RAW = {"provider_payload": {"id": "resp_1", "secret": SECRET}}


async def _reply(state: AgentState, config: dict):
    return Message(
        role="assistant",
        content=[TextBlock(text="hello")],
        raw=RAW,
    )


def _graph(checkpointer: InMemoryCheckpointer) -> CompiledGraph:
    graph = StateGraph(AgentState())
    graph.add_node("MAIN", _reply)
    graph.add_edge("MAIN", END)
    graph.set_entry_point("MAIN")
    return graph.compile(checkpointer=checkpointer)


@pytest.fixture
def client(monkeypatch):
    # The handlers read the mode when the app is built, so set it first.
    monkeypatch.setattr(settings.get_settings(), "MODE", "production")
    monkeypatch.setattr(settings.get_settings(), "ORIGINS", "http://localhost")
    with InjectQ.test_mode():
        checkpointer = InMemoryCheckpointer()
        graph = _graph(checkpointer)
        app = build_app(
            routers=[graph_router, ag_ui_router],
            checkpointer=checkpointer,
            extra_bindings={CompiledGraph: graph, GraphConfig: _Config()},
        )
        # The WebSocket route binds InjectQ.get_instance() (no HTTP middleware runs for it),
        # so the app's container has to be that instance.
        container = _app_container(app)
        monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: container))
        yield make_client(app)


def _app_container(app) -> InjectQ:
    for middleware in app.user_middleware:
        found = (middleware.kwargs or {}).get("container")
        if isinstance(found, InjectQ):
            return found
    raise AssertionError("no InjectQ container on the app")


def _fail_with(monkeypatch, exc: Exception) -> None:
    async def prepare(self, *args: Any, **kwargs: Any):
        raise exc

    monkeypatch.setattr(GraphService, "_prepare_input", prepare)


USER_MESSAGE = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
HEADERS = user_headers("alice")


def _invoke(client) -> tuple[int, str, str]:
    response = client.post("/v1/graph/invoke", json={"messages": [USER_MESSAGE]}, headers=HEADERS)
    error = response.json()["error"]
    return response.status_code, error["code"], error["message"]


def _stream_lines(client) -> list[dict[str, Any]]:
    response = client.post("/v1/graph/stream", json={"messages": [USER_MESSAGE]}, headers=HEADERS)
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _stream(client) -> tuple[str, str]:
    data = [c for c in _stream_lines(client) if c["event"] == "error"][-1]["data"]
    return data["code"], data["reason"]


def _ws(client) -> tuple[str, str]:
    with client.websocket_connect("/v1/graph/ws", headers=HEADERS) as ws:
        ws.send_json({"invoke_type": "fresh", "messages": [USER_MESSAGE]})
        while True:
            chunk = ws.receive_json()
            if chunk["event"] == "error":
                return chunk["data"]["code"], chunk["data"]["reason"]


def _ag_ui(client) -> tuple[str, str]:
    body = {
        "threadId": "new",
        "runId": "run-1",
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    response = client.post("/v1/ag-ui", json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    for frame in response.text.split("\n\n"):
        if frame.startswith("data: "):
            event = json.loads(frame[len("data: ") :])
            if event["type"] == "RUN_ERROR":
                return event["code"], event["message"]
    raise AssertionError(f"no RUN_ERROR event in {response.text}")


@pytest.mark.parametrize("case", list(CASES))
def test_every_transport_gives_the_same_code_and_message(case, client, monkeypatch):
    exc, status, code, message = CASES[case]
    _fail_with(monkeypatch, exc)

    assert _invoke(client) == (status, code, message)
    assert _stream(client) == (code, message)
    assert _ws(client) == (code, message)
    # A failure inside the graph run is a GRAPH_ERROR on AG-UI (documented code).
    assert _ag_ui(client) == ("GRAPH_ERROR", message)


def test_raw_driver_error_is_classified_on_invoke_and_stream(client, monkeypatch):
    class DataError(Exception):
        sqlstate = "22021"

    DataError.__module__ = "asyncpg.exceptions"
    exc = RuntimeError("write failed")
    exc.__cause__ = DataError(SECRET)
    _fail_with(monkeypatch, exc)

    status, code, message = _invoke(client)
    assert (status, code) == (422, "INVALID_REQUEST_DATA")
    assert _stream(client) == (code, message)
    assert SECRET not in message


def _assert_client_safe(item: Any) -> None:
    """No ``raw`` payload and no ``internal_data`` key but ``error``, anywhere in ``item``."""
    if isinstance(item, dict):
        assert item.get("raw") is None, item
        internal = item.get("internal_data")
        if isinstance(internal, dict):
            assert set(internal) <= {"error"}, internal
        for value in item.values():
            _assert_client_safe(value)
    elif isinstance(item, list):
        for value in item:
            _assert_client_safe(value)


def _add_internal_data(monkeypatch) -> None:
    """Make every saved state carry framework bookkeeping in ``internal_data``."""
    original = InMemoryCheckpointer.aput_state

    async def put_state(self, config, state):
        state.execution_meta.internal_data["provider_debug"] = {"secret": SECRET}
        return await original(self, config, state)

    monkeypatch.setattr(InMemoryCheckpointer, "aput_state", put_state)


def test_invoke_answer_has_no_raw_or_internal_data(client, monkeypatch):
    _add_internal_data(monkeypatch)
    response = client.post(
        "/v1/graph/invoke",
        json={"messages": [USER_MESSAGE], "response_granularity": "full"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert any(m.get("role") == "assistant" for m in data["messages"])
    assert data["state"] is not None
    _assert_client_safe(data)
    assert SECRET not in response.text


def test_stream_chunks_have_no_raw_or_internal_data(client, monkeypatch):
    _add_internal_data(monkeypatch)
    response = client.post(
        "/v1/graph/stream",
        json={"messages": [USER_MESSAGE], "response_granularity": "full"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    chunks = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert any((c.get("message") or {}).get("role") == "assistant" for c in chunks)
    _assert_client_safe(chunks)
    assert SECRET not in response.text


def test_mapping_covers_every_registered_handler():
    """``client_error`` mirrors ``handle_errors.py``: a new handler needs a mapping too."""
    app = FastAPI()
    init_errors_handler(app)
    # ValueError: the graph routes answer it themselves; the request validation errors are
    # FastAPI's own (request bodies), which never reach a run.
    framework = {ValueError, RequestValidationError, WebSocketRequestValidationError}
    registered = set(app.exception_handlers) - framework
    assert registered == set(HANDLED_TYPES)


@pytest.mark.parametrize(
    ("exc", "code", "message"),
    [
        (
            ClientInputError("resume does not answer the open interrupt"),
            "INVALID_INPUT",
            "resume does not answer the open interrupt",
        ),
        (ValueError(SECRET), "INVALID_INPUT", "Invalid input provided."),
        (RuntimeError(SECRET), "AG_UI_ERROR", GENERIC_HTTP),
    ],
)
def test_ag_ui_failure_before_the_run_keeps_its_documented_code(
    exc, code, message, client, monkeypatch
):
    """A failure before the graph runs: INVALID_INPUT for refused input, AG_UI_ERROR for the
    rest, never the exception text in production."""

    async def checkpoint_state(*args: Any, **kwargs: Any):
        raise exc

    monkeypatch.setattr(ag_ui_service, "_checkpoint_state", checkpoint_state)
    assert _ag_ui(client) == (code, message)
