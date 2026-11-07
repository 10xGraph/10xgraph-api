"""Thread ownership on the run entry points, against a real graph and InMemoryCheckpointer.

Every route that runs on a named thread (invoke, stream, ``/v1/graph/ws``, ``/v1/graph/live``,
AG-UI, fix) reads the thread with the caller's identity first (``require_thread``), before the
thread lock, the run slot and any response header. Another user's thread and an unknown id
under ``runs.thread_ids: "server"`` both answer 404. Runs and fix stay in the caller's own
threads whatever their reach; stop and observability follow the reach. Run caps and
idempotency records are keyed by user and tenant values.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from fastapi import HTTPException, Request, Response, WebSocketDisconnect
from fastapi.testclient import TestClient
from injectq import InjectQ
from tenxgraph.core.graph import CompiledGraph, StateGraph
from tenxgraph.core.state import AgentState, Message, TextBlock
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.utils.constants import END

from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.rbac import RbacConfig
from tenxgraph_api.src.app.core.config.graph_config import (
    AgUiConfig,
    GraphConfig,
    RunsConfig,
    WebSocketConfig,
)
from tenxgraph_api.src.app.routers.graph import thread_lock
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

NOT_FOUND = {"code": "THREAD_NOT_FOUND", "message": "Thread not found"}
# A bearer credential is sent so the WebSocket per-run re-check (which re-verifies the
# credential) sees the same user; ``_Auth`` reads only the test headers.
BEARER = {"Authorization": "Bearer test"}
ALICE = {**BEARER, "X-Test-User": "alice"}
MALLORY = {**BEARER, "X-Test-User": "mallory"}
ADMIN = {**BEARER, "X-Test-User": "root", "X-Test-Role": "admin"}

ROLES = RbacConfig(
    roles={
        "user": {"permissions": ["*"], "reach": "own"},
        "admin": {"permissions": ["*"], "reach": "all"},
    },
    default_role="user",
)


class _Auth(BaseAuth):
    """``X-Test-User`` is the user, ``X-Test-Role`` the role, ``X-Test-Org`` a tenant value."""

    rbac = ROLES

    def authenticate(self, request: Request, response: Response | None, credential: Any):
        user_id = request.headers.get("X-Test-User")
        if not user_id:
            raise HTTPException(status_code=401, detail="X-Test-User header required")
        claims: dict[str, Any] = {"user_id": user_id}
        if role := request.headers.get("X-Test-Role"):
            claims["roles"] = [role]
        if org := request.headers.get("X-Test-Org"):
            claims["org"] = org
        return claims


class _TenantAuth(_Auth):
    rbac = RbacConfig(
        roles={"user": {"permissions": ["*"], "reach": "own"}},
        default_role="user",
        tenant_claims=["org"],
    )


class _Config:
    """GraphConfig stand-in: auth as given, no rate limit or connection caps."""

    rate_limit = None
    websocket = WebSocketConfig(max_connections=None, max_connections_per_user=None)
    ag_ui = AgUiConfig(enabled=True)

    def __init__(self, runs: RunsConfig) -> None:
        self.runs = runs

    def auth_config(self) -> str:
        return "custom"


# The graph: MAIN, then SECOND. A message containing "block" parks MAIN until RELEASE is set,
# so a test can act while a run is in progress. SECOND not running means the run was stopped.
ENTERED = threading.Event()
RELEASE = threading.Event()
MAIN_CALLS: list[str] = []


async def main_node(state: AgentState, config: dict):
    MAIN_CALLS.append(str(config.get("thread_id")))
    if "block" in state.context[-1].text():
        ENTERED.set()
        while not RELEASE.is_set():
            await asyncio.sleep(0.01)
    return Message(role="assistant", content=[TextBlock(text="first")])


async def second_node(state: AgentState, config: dict):
    return Message(role="assistant", content=[TextBlock(text="second")])


def _graph(checkpointer: BaseCheckpointer) -> CompiledGraph:
    graph = StateGraph(AgentState())
    graph.add_node("MAIN", main_node)
    graph.add_node("SECOND", second_node)
    graph.set_entry_point("MAIN")
    graph.add_edge("MAIN", "SECOND")
    graph.add_edge("SECOND", END)
    return graph.compile(checkpointer=checkpointer)


class _FailingCheckpointer(InMemoryCheckpointer):
    """``aget_thread`` fails while ``down`` is set (a database outage)."""

    down = False

    async def aget_thread(self, config: dict[str, Any]):
        if self.down:
            raise ConnectionError("database unreachable")
        return await super().aget_thread(config)


class Harness:
    def __init__(self, client: TestClient, graph: CompiledGraph, checkpointer: Any) -> None:
        self.client = client
        self.graph = graph
        self.checkpointer = checkpointer

    def invoke(self, headers: dict[str, str] | None, thread_id: str | None = None, text="hi"):
        config = {"thread_id": thread_id} if thread_id else {}
        body = {"messages": [_user(text)], "config": config}
        return self.client.post("/v1/graph/invoke", json=body, headers=headers or {})

    def new_thread(self, headers: dict[str, str] | None) -> str:
        resp = self.invoke(headers)
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]["meta"]["thread_id"]

    def stream(self, headers: dict[str, str] | None, thread_id: str):
        body = {"messages": [_user()], "config": {"thread_id": thread_id}}
        return self.client.post("/v1/graph/stream", json=body, headers=headers or {})

    def ag_ui(self, headers: dict[str, str] | None, thread_id: str):
        body = {
            "threadId": thread_id,
            "runId": "run-1",
            "messages": [{"id": "u1", "role": "user", "content": "hi"}],
        }
        return self.client.post("/v1/ag-ui", json=body, headers=headers or {})

    def ws_run(self, headers: dict[str, str] | None, thread_id: str | None) -> list[dict]:
        """One ``/v1/graph/ws`` run; the chunks up to the error or the ``done`` chunk."""
        frame: dict[str, Any] = {"messages": [_user()]}
        if thread_id:
            frame["config"] = {"thread_id": thread_id}
        with self.client.websocket_connect("/v1/graph/ws", headers=headers or {}) as ws:
            ws.send_json(frame)
            return _read_run(ws)


def _user(text: str = "hi") -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _read_run(ws: Any) -> list[dict]:
    chunks = []
    while True:
        chunk = ws.receive_json()
        chunks.append(chunk)
        if chunk["event"] == "error" or (chunk.get("data") or {}).get("status") == "done":
            return chunks


@pytest.fixture
def make(monkeypatch):
    """Build a harness: ``make(auth=..., thread_ids=..., checkpointer=...)``."""
    seen: list[InjectQ] = []
    clients: list[TestClient] = []
    real_setup = conftest.setup_fastapi
    ENTERED.clear()
    RELEASE.clear()
    MAIN_CALLS.clear()

    def setup(container, app):
        seen.append(container)
        real_setup(container, app)

    def build(auth: BaseAuth | None = None, thread_ids: str = "server", checkpointer=None):
        with InjectQ.test_mode():
            checkpointer = checkpointer or InMemoryCheckpointer()
            graph = _graph(checkpointer)
        monkeypatch.setattr(conftest, "setup_fastapi", setup)
        # The WebSocket routes and the telemetry lookup use the global container.
        monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: seen[-1]))
        bindings: dict[type, Any] = {
            GraphConfig: _Config(RunsConfig(thread_ids=thread_ids)),
            CompiledGraph: graph,
            TelemetryStore: TelemetryStore(),
        }
        routers = [graph_router] + ([ag_ui_router] if ag_ui_router is not None else [])
        app = build_app(
            routers=routers,
            auth=auth or _Auth(),
            checkpointer=checkpointer,
            extra_bindings=bindings,
        )
        if auth is False:  # auth off, as the loader binds it
            seen[-1].bind_instance(BaseAuth, None, allow_none=True)
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return Harness(client, graph, checkpointer)

    yield build
    RELEASE.set()
    for client in clients:
        client.__exit__(None, None, None)


def _is_not_found(resp: Any) -> bool:
    if resp.status_code != 404 or not resp.headers["content-type"].startswith("application/json"):
        return False
    error = resp.json()["error"]
    return error["code"] == NOT_FOUND["code"] and error["message"] == NOT_FOUND["message"]


def _ws_not_found(chunks: list[dict]) -> bool:
    data = chunks[-1].get("data") or {}
    return chunks[-1]["event"] == "error" and data == {
        "reason": NOT_FOUND["message"],
        "code": NOT_FOUND["code"],
    }


# --------------------------------------------------------------------------- runs.thread_ids


@pytest.mark.parametrize("auth_on", [True, False], ids=["auth-on", "auth-off"])
def test_server_mode_refuses_an_unknown_thread_id_everywhere(make, auth_on):
    h = make(auth=None if auth_on else False)
    headers = ALICE if auth_on else None
    assert _is_not_found(h.invoke(headers, "made-up"))
    assert _is_not_found(h.stream(headers, "made-up"))
    assert _ws_not_found(h.ws_run(headers, "made-up"))
    if ag_ui_router is not None:
        assert _is_not_found(h.ag_ui(headers, "made-up"))
    assert MAIN_CALLS == []  # nothing ran
    # Starting a new thread (no id) still works, and the server's id can be reused.
    thread_id = h.new_thread(headers)
    assert h.invoke(headers, thread_id).status_code == 200
    assert h.ws_run(headers, thread_id)[-1]["data"]["status"] == "done"


@pytest.mark.parametrize("auth_on", [True, False], ids=["auth-on", "auth-off"])
def test_client_mode_creates_the_named_thread(make, auth_on):
    h = make(auth=None if auth_on else False, thread_ids="client")
    headers = ALICE if auth_on else None
    assert h.invoke(headers, "client-made").status_code == 200
    assert h.stream(headers, "client-streamed").status_code == 200
    assert h.ws_run(headers, "client-ws")[-1]["data"]["status"] == "done"
    if ag_ui_router is not None:
        assert h.ag_ui(headers, "client-ag-ui").status_code == 200
    owner = "alice" if auth_on else "anonymous"
    for thread_id in ("client-made", "client-streamed", "client-ws"):
        assert asyncio.run(h.checkpointer.aget_thread_owner(thread_id)) == owner


def test_client_mode_still_refuses_another_users_thread(make):
    h = make(thread_ids="client")
    assert h.invoke(ALICE, "alices").status_code == 200
    assert _is_not_found(h.invoke(MALLORY, "alices"))
    assert _is_not_found(h.stream(MALLORY, "alices"))  # before the headers, not a chunk
    assert _ws_not_found(h.ws_run(MALLORY, "alices"))
    if ag_ui_router is not None:
        assert _is_not_found(h.ag_ui(MALLORY, "alices"))
    assert asyncio.run(h.checkpointer.aget_thread_owner("alices")) == "alice"


# --------------------------------------------------------------------------- foreign threads


def test_another_users_thread_is_404_on_every_run_route(make):
    h = make()
    thread_id = h.new_thread(ALICE)
    MAIN_CALLS.clear()
    assert _is_not_found(h.invoke(MALLORY, thread_id))
    stream = h.stream(MALLORY, thread_id)
    assert _is_not_found(stream)  # a real 404, not an error chunk after a 200
    assert _ws_not_found(h.ws_run(MALLORY, thread_id))
    if ag_ui_router is not None:
        assert _is_not_found(h.ag_ui(MALLORY, thread_id))
    fix = h.client.post("/v1/graph/fix", json={"thread_id": thread_id}, headers=MALLORY)
    assert _is_not_found(fix)
    stop = h.client.post("/v1/graph/stop", json={"thread_id": thread_id}, headers=MALLORY)
    assert _is_not_found(stop)
    obs = h.client.get(f"/v1/observability/{thread_id}", headers=MALLORY)
    assert _is_not_found(obs)
    assert MAIN_CALLS == []
    # The owner still has every route.
    assert h.invoke(ALICE, thread_id).status_code == 200
    assert h.stream(ALICE, thread_id).status_code == 200
    assert h.client.post("/v1/graph/fix", json={"thread_id": thread_id}, headers=ALICE).json()[
        "data"
    ]["success"]
    obs = h.client.get(f"/v1/observability/{thread_id}", headers=ALICE)
    assert obs.status_code == 200
    assert obs.json()["data"]["run_count"] >= 1


def test_a_foreign_and_an_unknown_thread_answer_the_same(make):
    h = make()
    thread_id = h.new_thread(ALICE)
    foreign = h.invoke(MALLORY, thread_id).json()["error"]
    unknown = h.invoke(MALLORY, "never-made").json()["error"]
    assert {k: foreign[k] for k in ("code", "message")} == {
        k: unknown[k] for k in ("code", "message")
    }


def test_the_ws_socket_stays_open_after_a_refused_thread(make):
    h = make()
    thread_id = h.new_thread(ALICE)
    with h.client.websocket_connect("/v1/graph/ws", headers=MALLORY) as ws:
        ws.send_json({"messages": [_user()], "config": {"thread_id": thread_id}})
        assert _ws_not_found(_read_run(ws))
        ws.send_json({"messages": [_user()]})
        assert _read_run(ws)[-1]["data"]["status"] == "done"


@pytest.fixture
def live_agent(monkeypatch):
    """Let the turn-based test graph through the ``/v1/graph/live`` agent-type check."""
    monkeypatch.setattr(GraphService, "is_live_agent", property(lambda self: True))


def _live_refusal(h: Harness, headers: dict[str, str], thread_id: str) -> tuple[dict, int]:
    with h.client.websocket_connect("/v1/graph/live", headers=headers) as ws:
        ws.send_json({"model": "m", "thread_id": thread_id})
        event = ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    return event, closed.value.code


def test_live_refuses_another_users_thread_and_an_unknown_one(make, live_agent):
    h = make()
    thread_id = h.new_thread(ALICE)
    for target in (thread_id, "never-made"):
        event, code = _live_refusal(h, MALLORY, target)
        assert event["type"] == "error"
        assert event["code"] == NOT_FOUND["code"]
        assert event["message"] == NOT_FOUND["message"]
        assert code == 1008


# --------------------------------------------------------------------------- reach


def test_admin_reach_all_cannot_run_or_fix_in_another_users_thread(make):
    h = make()
    thread_id = h.new_thread(ALICE)
    assert _is_not_found(h.invoke(ADMIN, thread_id))
    assert _is_not_found(h.stream(ADMIN, thread_id))
    assert _ws_not_found(h.ws_run(ADMIN, thread_id))
    fix = h.client.post("/v1/graph/fix", json={"thread_id": thread_id}, headers=ADMIN)
    assert _is_not_found(fix)
    # Reads and stop follow the reach: the admin may look and stop.
    assert h.client.get(f"/v1/observability/{thread_id}", headers=ADMIN).status_code == 200
    stop = h.client.post("/v1/graph/stop", json={"thread_id": thread_id}, headers=ADMIN)
    assert stop.status_code == 200, stop.text
    assert stop.json()["data"]["ok"] is True


# --------------------------------------------------------------------------- stop


def _start_blocked_run(h: Harness, thread_id: str) -> tuple[threading.Thread, list[Any]]:
    """Alice's run on ``thread_id``, parked in MAIN until RELEASE is set."""
    out: list[Any] = []

    def run() -> None:
        out.append(h.invoke(ALICE, thread_id, text="block"))

    worker = threading.Thread(target=run)
    worker.start()
    assert ENTERED.wait(10), "the run never started"
    return worker, out


def _texts(resp: Any) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [
        block.get("text")
        for message in resp.json()["data"]["messages"]
        for block in message["content"]
        if block.get("type") == "text"
    ]


def _handles(h: Harness, thread_id: str) -> list[Any]:
    return list(h.graph._runs.for_thread(thread_id))


def test_a_foreign_stop_or_cancel_does_not_stop_the_owners_run(make):
    h = make()
    thread_id = h.new_thread(ALICE)
    worker, out = _start_blocked_run(h, thread_id)
    try:
        stop = h.client.post("/v1/graph/stop", json={"thread_id": thread_id}, headers=MALLORY)
        assert _is_not_found(stop)
        body = {
            "messages": [_user()],
            "config": {"thread_id": thread_id},
            "on_busy": "cancel",
        }
        cancel = h.client.post("/v1/graph/invoke", json=body, headers=MALLORY)
        assert _is_not_found(cancel)  # refused before it waits for (and cancels) the run
        assert all(not handle.stop_requested for handle in _handles(h, thread_id))
    finally:
        RELEASE.set()
        worker.join(10)
    assert "second" in _texts(out[0])  # the run went on to SECOND: it was not stopped


def test_admin_stop_stops_another_users_run(make):
    h = make()
    thread_id = h.new_thread(ALICE)
    worker, out = _start_blocked_run(h, thread_id)
    try:
        stop = h.client.post("/v1/graph/stop", json={"thread_id": thread_id}, headers=ADMIN)
        assert stop.status_code == 200, stop.text
        assert stop.json()["data"]["running"] is True
    finally:
        RELEASE.set()
        worker.join(10)
    assert "second" not in _texts(out[0])  # stopped in MAIN (the stop may end it at once)


def test_owner_stop_of_an_idle_thread_is_not_a_404(make):
    h = make()
    thread_id = h.new_thread(ALICE)
    stop = h.client.post("/v1/graph/stop", json={"thread_id": thread_id}, headers=ALICE)
    assert stop.status_code == 200, stop.text
    assert stop.json()["data"]["running"] is False


def test_stop_of_an_unknown_thread_is_a_404(make):
    h = make()
    stop = h.client.post("/v1/graph/stop", json={"thread_id": "no-such-thread"}, headers=ALICE)
    assert _is_not_found(stop)


# --------------------------------------------------------------------------- idempotency (P8)


def test_idempotency_key_is_not_shared_across_tenants(make):
    h = make(auth=_TenantAuth())
    org_a = {**ALICE, "X-Test-Org": "org-a", "Idempotency-Key": "k-1"}
    org_b = {**ALICE, "X-Test-Org": "org-b", "Idempotency-Key": "k-1"}
    first = h.invoke(org_a)
    assert first.status_code == 200, first.text
    other_tenant = h.invoke(org_b)
    assert other_tenant.status_code == 200, other_tenant.text
    first_thread = first.json()["data"]["meta"]["thread_id"]
    # A run of its own, not org-a's stored result.
    assert other_tenant.json()["data"]["meta"]["thread_id"] != first_thread
    replay = h.invoke(org_a)
    assert replay.json()["data"]["meta"]["thread_id"] == first_thread


# --------------------------------------------------------------------------- failure modes


def test_a_storage_error_in_the_thread_check_fails_closed(make, monkeypatch):
    monkeypatch.setattr(thread_lock, "THREAD_BUSY_WAIT_SECONDS", 0.2)
    checkpointer = _FailingCheckpointer()
    h = make(checkpointer=checkpointer)
    thread_id = h.new_thread(ALICE)
    MAIN_CALLS.clear()
    checkpointer.down = True
    assert h.invoke(ALICE, thread_id).status_code >= 500
    stream = h.stream(ALICE, thread_id)
    assert stream.status_code >= 500
    assert not stream.headers["content-type"].startswith("application/x-ndjson")
    if ag_ui_router is not None:
        assert h.ag_ui(ALICE, thread_id).status_code >= 500
    with pytest.raises(WebSocketDisconnect) as closed:
        h.ws_run(ALICE, thread_id)
    assert closed.value.code == 1011
    assert MAIN_CALLS == []  # no run started
    checkpointer.down = False
    # Nothing was left held: the thread lock is free (a held one would answer 409).
    assert h.invoke(ALICE, thread_id).status_code == 200
