"""The caller's credential reaches the graph's tools and nothing else.

A real graph (MAIN asks for the ``whoami`` tool, the tool records the credential it gets) runs
behind the real auth dependency on invoke, stream, ``/v1/graph/ws`` and AG-UI. The token must
reach the tool and must not appear in responses, chunks, thread and state routes, audit lines,
application logs or error responses. Clients cannot set ``config["credential"]``.
"""

# ruff: noqa: S101

from __future__ import annotations

import io
import json
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request, Response
from fastapi.testclient import TestClient
from injectq import InjectQ
from tenxgraph.core.authz import Identity, UserCredential
from tenxgraph.core.graph import CompiledGraph, StateGraph, ToolNode
from tenxgraph.core.state import AgentState, Message, TextBlock, ToolCallBlock
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.utils.constants import END

from tenxgraph_api.src.app.core.audit import set_sinks
from tenxgraph_api.src.app.core.audit.sinks import AUDIT_LOGGER_NAME, LogAuditSink
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.permissions import WS_BEARER_SUBPROTOCOL
from tenxgraph_api.src.app.core.config.graph_config import (
    AgUiConfig,
    GraphConfig,
    RunsConfig,
    WebSocketConfig,
)
from tenxgraph_api.src.app.routers.checkpointer.router import router as threads_router
from tenxgraph_api.src.app.routers.graph.router import router as graph_router
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore

from . import conftest
from .conftest import build_app


try:
    from tenxgraph_api.src.app.routers.ag_ui import router as ag_ui_router
except ImportError:  # the ag-ui extra is not installed
    ag_ui_router = None

needs_ag_ui = pytest.mark.skipif(ag_ui_router is None, reason="needs the ag-ui extra")

SECRET = "s3cr3t-7f2c9a41"
TOKEN = f"alice.{SECRET}"
ALICE = {"Authorization": f"Bearer {TOKEN}"}

# What the tool received on each call: the token, or None for no credential.
TOOL_SAW: list[Any] = []


async def whoami(credential: UserCredential | None = None) -> str:
    """Report whether the caller's credential arrived (never echoes it)."""
    TOOL_SAW.append(credential.token.get_secret_value() if credential else None)
    if credential is not None:
        TOOL_SAW.append(credential.scheme)
    return "checked"


def _call(name: str) -> Message:
    return Message(
        role="assistant",
        content=[ToolCallBlock(id="call-1", name=name, args={})],
        tools_calls=[
            {"id": "call-1", "type": "function", "function": {"name": name, "arguments": "{}"}}
        ],
    )


async def main_node(state: AgentState, config: dict):
    last = state.context[-1]
    if last.role == "tool":
        return Message(role="assistant", content=[TextBlock(text="done")])
    if "fail" in last.text():
        # Error text built from the whole run config: the credential's repr is redacted.
        raise RuntimeError(f"boom {config}")
    return _call("whoami")


def _route(state: AgentState) -> str:
    last = state.context[-1]
    if last.role == "assistant" and last.tools_calls:
        return "TOOL"
    if last.role == "tool":
        return "MAIN"
    return END


def _graph(checkpointer: BaseCheckpointer) -> CompiledGraph:
    graph = StateGraph(AgentState())
    graph.add_node("MAIN", main_node)
    graph.add_node("TOOL", ToolNode([whoami]))
    graph.add_conditional_edges("MAIN", _route, {"TOOL": "TOOL", END: END})
    graph.add_conditional_edges("TOOL", _route, {"MAIN": "MAIN", END: END})
    graph.set_entry_point("MAIN")
    return graph.compile(checkpointer=checkpointer)


class BearerAuth(BaseAuth):
    """The bearer token is ``<user_id>.<SECRET>``; the default ``downstream_credential``."""

    def authenticate(self, request: Request, response: Response | None, credential: Any):
        if credential is None or not credential.credentials.endswith(SECRET):
            raise HTTPException(status_code=401, detail="bad token")
        return {"user_id": credential.credentials.split(".")[0]}


class ApiKeyAuth(BaseAuth):
    """Authenticates an ``X-Api-Key``; the run credential is the key, not a bearer."""

    def authenticate(self, request: Request, response: Response | None, credential: Any):
        key = request.headers.get("X-Api-Key")
        if not key:
            raise HTTPException(status_code=401, detail="no key")
        return {"user_id": "keyuser"}

    async def downstream_credential(self, connection, credential, claims):
        return UserCredential(connection.headers["X-Api-Key"], scheme="ApiKey")


class NoCredentialAuth(BearerAuth):
    async def downstream_credential(self, connection, credential, claims):
        return None


class _Config:
    rate_limit = None
    websocket = WebSocketConfig(max_connections=None, max_connections_per_user=None)
    ag_ui = AgUiConfig(enabled=True)

    def __init__(self, runs: RunsConfig | None = None) -> None:
        self.runs = runs or RunsConfig()

    def auth_config(self) -> str:
        return "custom"


class Harness:
    def __init__(self, client: TestClient, checkpointer: Any) -> None:
        self.client = client
        self.checkpointer = checkpointer

    def invoke(self, headers: dict | None, text: str = "hi", **body: Any):
        payload = {"messages": [_user(text)], **body}
        return self.client.post("/v1/graph/invoke", json=payload, headers=headers or {})

    def stream(self, headers: dict | None, text: str = "hi", **body: Any):
        payload = {"messages": [_user(text)], **body}
        return self.client.post("/v1/graph/stream", json=payload, headers=headers or {})

    def ws_run(self, headers: dict | None = None, **connect: Any) -> list[dict]:
        with self.client.websocket_connect("/v1/graph/ws", headers=headers or {}, **connect) as ws:
            ws.send_json({"messages": [_user()]})
            chunks = []
            while True:
                chunk = ws.receive_json()
                chunks.append(chunk)
                if chunk["event"] == "error" or (chunk.get("data") or {}).get("status") == "done":
                    return chunks

    def ag_ui(self, headers: dict | None):
        body = {
            "threadId": "new",
            "runId": "run-1",
            "messages": [{"id": "u1", "role": "user", "content": "hi"}],
        }
        return self.client.post("/v1/ag-ui", json=body, headers=headers or {})


def _user(text: str = "hi") -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


@pytest.fixture
def make(monkeypatch):
    """``make(auth=...)``: a harness; ``auth=False`` binds no auth (auth off)."""
    seen: list[InjectQ] = []
    clients: list[TestClient] = []
    real_setup = conftest.setup_fastapi
    TOOL_SAW.clear()

    def setup(container, app):
        seen.append(container)
        real_setup(container, app)

    def build(auth: BaseAuth | bool | None = None, runs: RunsConfig | None = None) -> Harness:
        with InjectQ.test_mode():
            checkpointer = InMemoryCheckpointer()
            graph = _graph(checkpointer)
        monkeypatch.setattr(conftest, "setup_fastapi", setup)
        # The WebSocket routes use the global container.
        monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: seen[-1]))
        routers = [graph_router, threads_router]
        routers += [ag_ui_router] if ag_ui_router is not None else []
        app = build_app(
            routers=routers,
            auth=auth or BearerAuth(),
            checkpointer=checkpointer,
            extra_bindings={
                GraphConfig: _Config(runs),
                CompiledGraph: graph,
                TelemetryStore: TelemetryStore(),
            },
        )
        if auth is False:  # auth off, as the loader binds it
            seen[-1].bind_instance(BaseAuth, None, allow_none=True)
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return Harness(client, checkpointer)

    yield build
    for client in clients:
        client.__exit__(None, None, None)


# --------------------------------------------------------------------------- reaches the tool


def test_invoke_gives_the_tool_the_bearer_token_and_never_returns_it(make):
    h = make()
    resp = h.invoke(ALICE)
    assert resp.status_code == 200, resp.text
    assert TOOL_SAW == [TOKEN, "Bearer"]
    assert SECRET not in resp.text


def test_stream_gives_the_tool_the_bearer_token_and_no_chunk_carries_it(make):
    h = make()
    resp = h.stream(ALICE)
    assert resp.status_code == 200
    assert '"status":"done"' in resp.text.replace(" ", "")
    assert TOOL_SAW == [TOKEN, "Bearer"]
    assert SECRET not in resp.text


@pytest.mark.parametrize("transport", ["header", "subprotocol"])
def test_websocket_gives_the_tool_the_token_from_every_transport(make, transport):
    h = make()
    if transport == "header":
        chunks = h.ws_run(ALICE)
    elif transport == "subprotocol":
        chunks = h.ws_run(subprotocols=[WS_BEARER_SUBPROTOCOL, TOKEN])
    else:  # pragma: no cover
        raise AssertionError(transport)
    assert chunks[-1]["data"]["status"] == "done", chunks
    assert TOOL_SAW == [TOKEN, "Bearer"]
    assert SECRET not in json.dumps(chunks)


@needs_ag_ui
def test_ag_ui_gives_the_tool_the_bearer_token_and_no_event_carries_it(make):
    h = make()
    resp = h.ag_ui(ALICE)
    assert resp.status_code == 200, resp.text
    assert "RUN_FINISHED" in resp.text
    assert TOOL_SAW == [TOKEN, "Bearer"]
    assert SECRET not in resp.text


@pytest.mark.asyncio
async def test_live_session_config_carries_the_credential():
    captured: dict[str, Any] = {}

    async def arealtime(queue, config):
        captured.update(config)
        for _ in ():
            yield

    graph = MagicMock()
    graph.arealtime = arealtime
    checkpointer = MagicMock()

    async def aput_thread(config, info):
        return True

    checkpointer.aput_thread = aput_thread
    config = MagicMock()
    config.websocket = WebSocketConfig.from_dict({})
    service = GraphService(graph=graph, checkpointer=checkpointer, config=config)
    credential = UserCredential(TOKEN)
    init = {"thread_id": "t1"}
    async for _ in service.realtime_graph(
        MagicMock(), init, Identity(user_id="alice"), credential=credential
    ):
        pass
    assert captured["credential"] is credential


# --------------------------------------------------------------------------- custom BaseAuth


def test_custom_auth_supplies_its_own_credential(make):
    h = make(auth=ApiKeyAuth())
    resp = h.invoke({"X-Api-Key": "key-123"})
    assert resp.status_code == 200, resp.text
    assert TOOL_SAW == ["key-123", "ApiKey"]
    assert "key-123" not in resp.text


def test_custom_auth_returning_none_gives_no_credential(make):
    h = make(auth=NoCredentialAuth())
    assert h.invoke(ALICE).status_code == 200
    assert TOOL_SAW == [None]


def test_downstream_credential_must_return_a_user_credential(make):
    class RawString(BearerAuth):
        async def downstream_credential(self, connection, credential, claims):
            return credential.credentials

    h = make(auth=RawString())
    resp = h.invoke(ALICE)
    assert resp.status_code == 500
    assert TOOL_SAW == []
    assert SECRET not in resp.text


def test_auth_off_runs_without_a_credential(make):
    h = make(auth=False)
    resp = h.invoke(ALICE)  # a bearer header with auth off is ignored
    assert resp.status_code == 200, resp.text
    assert TOOL_SAW == [None]


# --------------------------------------------------------------------------- clients cannot set it


@pytest.mark.parametrize("auth", [None, False], ids=["auth-on", "auth-off"])
def test_client_config_cannot_set_or_override_the_credential(make, auth):
    h = make(auth=auth)
    resp = h.invoke(ALICE, config={"credential": "forged"})
    assert resp.status_code == 200, resp.text
    resp = h.stream(ALICE, config={"credential": {"token": "forged"}})
    assert resp.status_code == 200
    expected = [TOKEN, "Bearer"] if auth is None else [None]
    assert TOOL_SAW[:] == expected * 2


def test_config_keys_allow_list_cannot_readmit_the_credential(make):
    h = make(runs=RunsConfig(config_keys=frozenset({"credential"})))
    resp = h.invoke(ALICE, config={"credential": "forged"})
    assert resp.status_code == 200, resp.text
    assert TOOL_SAW == [TOKEN, "Bearer"]


def test_initial_state_cannot_carry_a_credential_to_the_tool(make):
    h = make()
    # A state field named ``credential`` is plain state: it never becomes the run credential.
    resp = h.invoke(ALICE, initial_state={"credential": "forged"})
    assert resp.status_code == 200, resp.text
    assert TOOL_SAW == [TOKEN, "Bearer"]


# --------------------------------------------------------------------------- never leaves the API


def test_thread_and_state_routes_never_return_the_credential(make):
    h = make()
    resp = h.invoke(ALICE)
    thread_id = resp.json()["data"]["meta"]["thread_id"]
    for path in (
        "/v1/threads",
        f"/v1/threads/{thread_id}",
        f"/v1/threads/{thread_id}/state",
        f"/v1/threads/{thread_id}/messages",
        f"/v1/observability/{thread_id}",
    ):
        got = h.client.get(path, headers=ALICE)
        assert got.status_code == 200, (path, got.text)
        assert SECRET not in got.text, path


def test_thread_naming_gets_a_config_without_the_credential(make, monkeypatch):
    named: list[dict] = []
    monkeypatch.setattr(GraphService, "names_threads", property(lambda self: True))
    monkeypatch.setattr(
        GraphService,
        "_name_thread_in_background",
        lambda self, config, thread_id, messages: named.append(config),
    )
    h = make()
    assert h.invoke(ALICE).status_code == 200
    assert h.stream(ALICE).status_code == 200
    assert len(named) == 2
    for config in named:
        assert config["user_id"] == "alice"
        assert "credential" not in config


def test_error_responses_never_carry_the_credential(make):
    h = make()
    resp = h.invoke(ALICE, text="fail")
    assert resp.status_code >= 400
    # Development mode echoes the error text, which holds the run config: redacted repr only.
    assert "UserCredential(scheme='Bearer', token='**********')" in resp.text
    assert SECRET not in resp.text
    resp = h.stream(ALICE, text="fail")
    assert '"event":"error"' in resp.text.replace(" ", "")
    assert SECRET not in resp.text


def test_no_log_line_or_audit_line_carries_the_credential(make, caplog):
    set_sinks([LogAuditSink()])
    handler = logging.getLogger(AUDIT_LOGGER_NAME).handlers[0]
    stream = io.StringIO()
    old = handler.setStream(stream)  # type: ignore[attr-defined]
    try:
        h = make()
        with caplog.at_level(logging.DEBUG):
            assert h.invoke(ALICE).status_code == 200
            assert h.stream(ALICE).status_code == 200
            h.ws_run(ALICE)
            assert h.invoke(ALICE, text="fail").status_code >= 400
            if ag_ui_router is not None:
                assert h.ag_ui(ALICE).status_code == 200
    finally:
        handler.setStream(old)  # type: ignore[attr-defined]
        set_sinks([])
    audit = stream.getvalue()
    assert '"event":"access"' in audit.replace(" ", "")
    assert SECRET not in audit
    assert caplog.records
    for record in caplog.records:
        assert SECRET not in record.getMessage(), record.getMessage()
    assert SECRET not in caplog.text
