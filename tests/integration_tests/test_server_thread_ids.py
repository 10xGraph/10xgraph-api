"""``runs.thread_ids`` defaults to ``"server"``: the server makes every thread id.

REST invoke/stream and the checkpointer routes refuse an unknown client-named id (the same
403 as a taken id, so existence is not revealed); omitting the id still starts a thread.
AG-UI and ``/v1/graph/live`` pass ``client_thread_ids`` and keep accepting frontend ids.
"""

# ruff: noqa: S101, PLR2004

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils.thread_info import ThreadInfo

from tenxgraph_api.src.app.core.auth.authorization import OwnershipAuthorizationBackend
from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router
from tenxgraph_api.src.app.routers.graph.router import router as graph_router
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInvokeOutputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

from .conftest import build_app, make_client, user_headers


graph_router_module = importlib.import_module("tenxgraph_api.src.app.routers.graph.router")

HTTP_OK = 200
HTTP_FORBIDDEN = 403
MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


async def _seed(checkpointer: InMemoryCheckpointer, owners: dict[str, str]) -> None:
    for thread_id, owner in owners.items():
        await checkpointer.aput_thread(
            {"thread_id": thread_id, "user_id": owner},
            ThreadInfo(thread_id=thread_id, user_id=owner),
        )


def _checkpointer_with(**owners: str) -> InMemoryCheckpointer:
    checkpointer = InMemoryCheckpointer()
    anyio.run(_seed, checkpointer, owners)
    return checkpointer


def _backend(checkpointer, *, thread_ids=None) -> OwnershipAuthorizationBackend:
    """The ownership backend as the loader builds it from ``runs`` (default or explicit)."""
    runs = RunsConfig() if thread_ids is None else RunsConfig(thread_ids=thread_ids)
    return OwnershipAuthorizationBackend(
        checkpointer=checkpointer, server_thread_ids=runs.server_thread_ids
    )


def _graph_client(checkpointer, authz):
    async def _stream(*_args, **_kwargs):
        if False:  # pragma: no cover - an empty async generator
            yield None

    service = MagicMock(spec=GraphService)
    service.check_media_urls = MagicMock()
    service.runs_policy = MagicMock(return_value=RunsConfig())
    service.invoke_graph = AsyncMock(return_value=GraphInvokeOutputSchema(messages=[]))
    service.stream_graph = MagicMock(side_effect=_stream)
    service.stop_graph = AsyncMock()
    app = build_app(
        routers=[graph_router],
        authz=authz,
        checkpointer=checkpointer,
        extra_bindings={GraphService: service},
    )
    return make_client(app)


def test_the_default_is_server_thread_ids():
    assert RunsConfig().thread_ids == "server"
    assert RunsConfig.from_dict({}).thread_ids == "server"
    assert RunsConfig().server_thread_ids is True


@pytest.mark.parametrize("path", ["/v1/graph/invoke", "/v1/graph/stream"])
def test_rest_run_on_an_unknown_id_is_refused_like_a_taken_id(path):
    checkpointer = _checkpointer_with(taken="alice")
    client = _graph_client(checkpointer, _backend(checkpointer))
    unknown = client.post(
        path,
        json={"messages": MESSAGES, "config": {"thread_id": "default"}},
        headers=user_headers("mallory"),
    )
    taken = client.post(
        path,
        json={"messages": MESSAGES, "config": {"thread_id": "taken"}},
        headers=user_headers("mallory"),
    )
    assert unknown.status_code == taken.status_code == HTTP_FORBIDDEN


@pytest.mark.parametrize("path", ["/v1/graph/invoke", "/v1/graph/stream"])
def test_rest_run_without_an_id_and_on_an_owned_id_still_works(path):
    checkpointer = _checkpointer_with(mine="alice")
    client = _graph_client(checkpointer, _backend(checkpointer))
    fresh = client.post("/v1/graph/invoke", json={"messages": MESSAGES}, headers=user_headers("a"))
    assert fresh.status_code == HTTP_OK, fresh.text
    own = client.post(
        path,
        json={"messages": MESSAGES, "config": {"thread_id": "mine"}},
        headers=user_headers("alice"),
    )
    assert own.status_code == HTTP_OK, own.text


@pytest.mark.parametrize("path", ["/v1/graph/invoke", "/v1/graph/stream"])
def test_client_opt_in_restores_client_named_threads(path):
    checkpointer = _checkpointer_with()
    client = _graph_client(checkpointer, _backend(checkpointer, thread_ids="client"))
    resp = client.post(
        path,
        json={"messages": MESSAGES, "config": {"thread_id": "my-own-id"}},
        headers=user_headers("alice"),
    )
    assert resp.status_code == HTTP_OK, resp.text


def test_put_state_on_an_unknown_id_is_refused_by_default():
    checkpointer = _checkpointer_with()
    app = build_app(
        routers=[checkpointer_router],
        authz=_backend(checkpointer),
        checkpointer=checkpointer,
    )
    client = make_client(app)
    body = {"state": {"context": []}, "config": {}}
    resp = client.put("/v1/threads/victim/state", json=body, headers=user_headers("mallory"))
    assert resp.status_code == HTTP_FORBIDDEN
    # Client mode, opted in, restores the old behaviour.
    opted = build_app(
        routers=[checkpointer_router],
        authz=_backend(checkpointer, thread_ids="client"),
        checkpointer=checkpointer,
    )
    resp = make_client(opted).put(
        "/v1/threads/victim/state", json=body, headers=user_headers("mallory")
    )
    assert resp.status_code == HTTP_OK, resp.text


@pytest.mark.asyncio
async def test_ws_run_refuses_an_unknown_id_but_live_accepts_it(monkeypatch):
    """/v1/graph/ws follows the server mode; /v1/graph/live passes client_thread_ids."""
    from injectq import InjectQ

    container = MagicMock()
    container.get.return_value = SimpleNamespace(auth_config=lambda: "jwt")
    monkeypatch.setattr(InjectQ, "get_instance", staticmethod(lambda: container))

    checkpointer = InMemoryCheckpointer()
    await _seed(checkpointer, {"taken": "alice"})
    authz = _backend(checkpointer)
    alice = {"user_id": "alice"}
    check = graph_router_module._ws_thread_authorized

    assert await check(authz, alice, "frontend-id", "stream") is False
    assert await check(authz, alice, "frontend-id", "stream", client_thread_ids=True) is True
    # Fresh threads and owned threads are unchanged; a foreign thread stays refused on live.
    assert await check(authz, alice, None, "stream") is True
    assert await check(authz, alice, "taken", "stream") is True
    assert (
        await check(authz, {"user_id": "mallory"}, "taken", "stream", client_thread_ids=True)
        is False
    )


@pytest.mark.asyncio
async def test_the_caller_owns_a_frontend_id_after_claiming_it():
    checkpointer = InMemoryCheckpointer()
    authz = _backend(checkpointer)
    alice, mallory = {"user_id": "alice"}, {"user_id": "mallory"}
    kw = {"resource_id": "copilot-1", "client_thread_ids": True}
    assert await authz.authorize(alice, "graph", "stream", **kw) is True
    await checkpointer.aput_thread(
        {"thread_id": "copilot-1", "user_id": "alice"},
        ThreadInfo(thread_id="copilot-1", user_id="alice"),
    )
    authz._get_resolver()._l1.clear()
    assert await authz.authorize(alice, "graph", "stream", **kw) is True
    assert await authz.authorize(mallory, "graph", "stream", **kw) is False
    # The exemption is only for stream-create on these routes; reads of a ghost stay refused.
    assert (
        await authz.authorize(alice, "graph", "read", resource_id="ghost", client_thread_ids=True)
        is False
    )
    # Other routes (no flag) still refuse a new id.
    assert await authz.authorize(alice, "graph", "stream", resource_id="other-new") is False


@pytest.mark.asyncio
async def test_ag_ui_asks_the_backend_with_client_thread_ids():
    pytest.importorskip("ag_ui")
    from tenxgraph_api.src.app.routers.ag_ui.router import run_ag_ui

    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "threadId": "copilot-9",
            "runId": "r1",
            "state": {},
            "messages": [{"id": "m1", "role": "user", "content": "hi"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )
    request.headers = {}
    authz = MagicMock()
    authz.authorize = AsyncMock(return_value=False)
    config = SimpleNamespace(
        auth_config=lambda: "jwt", ag_ui=SimpleNamespace(allow_client_tools=True)
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await run_ag_ui(request, MagicMock(), {"user_id": "alice"}, authz, config)
    assert exc.value.status_code == HTTP_FORBIDDEN
    authz.authorize.assert_awaited_once_with(
        {"user_id": "alice"}, "graph", "stream", resource_id="copilot-9", client_thread_ids=True
    )
