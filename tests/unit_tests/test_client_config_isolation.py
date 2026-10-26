"""Regression tests: client-supplied request config must not override server-owned keys.

A request's ``config`` is an open dict so frontends can pass their own values to the graph.
These tests pin the rule that keeps it safe: ``authz``, ``user``, ``user_id`` and ``_``-prefixed
keys always come from the server, and the thread the permission check approved is the thread
the service runs on.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.auth.request_config import (
    client_config,
    normalize_thread_id,
)
from tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service import (
    CheckpointerService,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
    WsGraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.routers.store.services.store_service import StoreService


FORGED = {
    "authz": {"user_id": "attacker", "scope": "none", "scopes": ["*"]},
    "user": {"user_id": "victim"},
    "user_id": "victim",
    "_skip_interrupt_at": "GUARD",
    "plan": "pro",  # an ordinary app value: must survive
}
TRUSTED_USER = {"user_id": "attacker", "authz": {"user_id": "attacker", "scope": "owner"}}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_client_config_drops_only_server_owned_keys():
    cleaned = client_config({**FORGED, "thread_id": "t1", "locale": "bn"})
    assert cleaned == {"plan": "pro", "thread_id": "t1", "locale": "bn"}


@pytest.mark.parametrize(
    "key, value",
    [
        # The core file tools would read and write under this directory
        ("file_tool_root", "/"),
        ("workspace_root", "/"),
        # 0 and None turn the core time limits off
        ("tool_timeout", 0),
        ("node_timeout", None),
        # Core caps parallel tool calls with it; the operator sets it in runs
        ("max_parallel_tool_calls", 10_000),
    ],
)
def test_client_config_drops_core_security_keys(key, value):
    assert client_config({key: value, "plan": "pro"}) == {"plan": "pro"}


@pytest.mark.parametrize("value", [None, "text", ["authz"], 5])
def test_client_config_non_dict_is_empty(value):
    assert client_config(value) == {}


def test_client_config_does_not_mutate_input():
    raw = dict(FORGED)
    client_config(raw)
    assert raw == FORGED


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  t1 ", "t1"), ("t1", "t1"), (42, "42"), ("", None), ("   ", None), (None, None)],
)
def test_normalize_thread_id(raw, expected):
    assert normalize_thread_id(raw) == expected


def test_normalize_thread_id_rejects_bool():
    assert normalize_thread_id(True) is None


# ---------------------------------------------------------------------------
# permission check reads the same thread the service uses
# ---------------------------------------------------------------------------


def _request(body: object, content_type: str | None = "application/json") -> Request:
    raw = json.dumps(body).encode()
    headers = [] if content_type is None else [(b"content-type", content_type.encode())]
    scope = {"type": "http", "method": "POST", "path": "/v1/graph/invoke", "headers": headers}
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


async def _body_resource_id(body: object, content_type: str | None = "application/json"):
    return await RequirePermission("graph", "invoke")._extract_resource_id_from_body(
        _request(body, content_type)
    )


@pytest.mark.asyncio
async def test_body_thread_id_mismatch_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await _body_resource_id({"thread_id": "fresh-1", "config": {"thread_id": "victim"}})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_body_thread_id_is_normalized_like_the_service():
    assert await _body_resource_id({"config": {"thread_id": "victim "}}) == "victim"
    assert await _body_resource_id({"thread_id": " t1", "config": {"thread_id": "t1 "}}) == "t1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type", ["application/vnd.x+json", None, "application/json; charset=utf-8"]
)
async def test_body_is_parsed_for_every_content_type_fastapi_parses(content_type):
    assert await _body_resource_id({"config": {"thread_id": "victim"}}, content_type) == "victim"


@pytest.mark.asyncio
async def test_body_non_json_content_type_is_ignored():
    assert await _body_resource_id({"thread_id": "t1"}, "text/plain") is None


# ---------------------------------------------------------------------------
# services never pass client authz/user downstream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpointer_service_config_strips_forged_keys():
    service = CheckpointerService.__new__(CheckpointerService)
    service.checkpointer = MagicMock()
    cfg = service._config({**FORGED, "thread_id": "t1"}, TRUSTED_USER)
    assert "authz" not in cfg
    assert "_skip_interrupt_at" not in cfg
    assert cfg["user"] is TRUSTED_USER
    assert cfg["user_id"] == "attacker"
    assert cfg["plan"] == "pro"
    assert cfg["thread_id"] == "t1"


def test_store_service_config_strips_forged_keys():
    service = StoreService(store=AsyncMock())
    cfg = service._config(dict(FORGED), TRUSTED_USER)
    assert "authz" not in cfg
    assert cfg["user"] is TRUSTED_USER
    assert cfg["user_id"] == "attacker"
    assert cfg["plan"] == "pro"


@pytest.fixture
def graph_service():
    srv = GraphService.__new__(GraphService)
    srv._graph = MagicMock()
    srv._graph.astop = AsyncMock(return_value={"status": "stopped"})
    srv.checkpointer = MagicMock()
    srv.config = MagicMock()
    srv.thread_name_generator = None
    srv._media_service = None
    srv._telemetry = None
    return srv


@pytest.mark.asyncio
async def test_graph_prepare_input_strips_forged_keys(graph_service):
    gi = GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        config={**FORGED, "thread_id": " t1 "},
    )
    _, config, _ = await graph_service._prepare_input(gi)
    assert "authz" not in config
    assert "user" not in config
    assert "_skip_interrupt_at" not in config
    assert config["thread_id"] == "t1"
    assert config["plan"] == "pro"


@pytest.mark.asyncio
async def test_graph_stop_strips_forged_keys(graph_service):
    await graph_service.stop_graph("t1", TRUSTED_USER, {**FORGED, "thread_id": "victim"})
    sent = graph_service._graph.astop.call_args[0][0]
    assert "authz" not in sent
    assert sent["thread_id"] == "t1"
    assert sent["user"] is TRUSTED_USER
    assert sent["plan"] == "pro"


# ---------------------------------------------------------------------------
# checkpointer routes run on the thread from the path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpointer_routes_pin_path_thread_id(monkeypatch):
    from tenxgraph_api.src.app.routers.checkpointer import router as ck
    from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
        ConfigSchema,
        PutMessagesSchema,
        StateSchema,
    )

    monkeypatch.setattr(ck, "success_response", lambda res, request: res)
    service = AsyncMock()
    forged = {**FORGED, "thread_id": "victim"}
    request = MagicMock()

    await ck.put_state(
        request=request,
        thread_id="own",
        payload=StateSchema(state={}, config=forged),
        service=service,
        user=TRUSTED_USER,
    )
    await ck.put_messages(
        request=request,
        thread_id="own",
        payload=PutMessagesSchema(
            messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            config=forged,
        ),
        service=service,
        user=TRUSTED_USER,
    )
    await ck.delete_message(
        request=request,
        thread_id="own",
        message_id="m1",
        payload=ConfigSchema(config=forged),
        service=service,
        user=TRUSTED_USER,
    )
    await ck.delete_thread(
        request=request,
        thread_id="own",
        payload=ConfigSchema(config=forged),
        service=service,
        user=TRUSTED_USER,
    )

    for call in (
        service.put_state.call_args,
        service.put_messages.call_args,
        service.delete_message.call_args,
        service.delete_thread.call_args,
    ):
        config = call[0][0]
        assert config["thread_id"] == "own"
        assert "authz" not in config
        assert config["plan"] == "pro"


# ---------------------------------------------------------------------------
# WebSocket: the checked thread is the thread that runs
# ---------------------------------------------------------------------------


def _ws_input(thread_id):
    return WsGraphInputSchema(
        invoke_type="fresh",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        config={"thread_id": thread_id, "plan": "pro"},
    )


@pytest.mark.parametrize(("raw", "expected"), [(" t1 ", "t1"), ("new", None), ("  ", None)])
def test_ws_thread_id_is_normalized_before_the_check(raw, expected):
    from tenxgraph_api.src.app.routers.graph.router import _ws_run_thread_id

    ws_input = _ws_input(raw)
    assert _ws_run_thread_id(ws_input) == expected
    config = ws_input.to_graph_input().config
    assert config.get("thread_id") == expected
    assert config["plan"] == "pro"


# ---------------------------------------------------------------------------
# runs.config_keys / runs.state_keys allow-lists
# ---------------------------------------------------------------------------


T1 = {"thread_id": "t1"}


def _input(**kwargs):
    return GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}], **kwargs
    )


def test_reject_unlisted_keys():
    from tenxgraph_api.src.app.core.auth.request_config import reject_unlisted_keys

    reject_unlisted_keys({"plan": "pro"}, None, "config")  # no allow-list: anything goes
    reject_unlisted_keys(None, frozenset(), "config")  # nothing sent
    reject_unlisted_keys({"locale": "en"}, frozenset({"locale"}), "config")
    reject_unlisted_keys({"thread_id": "t"}, frozenset(), "config", frozenset({"thread_id"}))
    with pytest.raises(ValueError, match="config keys not allowed by this server: plan, role"):
        reject_unlisted_keys({"role": "x", "plan": "y", "locale": "en"}, {"locale"}, "config")


@pytest.mark.asyncio
async def test_unlisted_config_key_is_rejected(graph_service):
    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig

    graph_service.config.runs = RunsConfig(config_keys=frozenset({"locale"}))
    with pytest.raises(ValueError, match="plan"):
        await graph_service._prepare_input(_input(config={"locale": "en", "plan": "pro"}))


@pytest.mark.asyncio
async def test_listed_and_always_allowed_config_keys_pass(graph_service):
    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig

    graph_service.config.runs = RunsConfig(config_keys=frozenset({"locale"}))
    _, config, _ = await graph_service._prepare_input(
        _input(config={"locale": "en", "thread_id": "t1", "run_id": "r1"})
    )
    assert (config["locale"], config["thread_id"], config["run_id"]) == ("en", "t1", "r1")


@pytest.mark.asyncio
async def test_reserved_keys_are_stripped_not_counted(graph_service):
    """Server-owned keys are dropped silently as before; the allow-list sees the rest."""
    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig

    graph_service.config.runs = RunsConfig(config_keys=frozenset())
    _, config, _ = await graph_service._prepare_input(
        _input(config={"user_id": "forged", "authz": {}, "_node_name": "x", "thread_id": "t1"})
    )
    assert "user_id" not in config and "authz" not in config


@pytest.mark.asyncio
async def test_unlisted_initial_state_key_is_rejected(graph_service):
    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig

    graph_service.config.runs = RunsConfig(state_keys=frozenset({"preferences"}))
    with pytest.raises(ValueError, match="initial_state keys not allowed by this server: role"):
        await graph_service._prepare_input(
            _input(config=T1, initial_state={"preferences": {}, "role": "admin"})
        )
    input_data, _, _ = await graph_service._prepare_input(
        _input(config=T1, initial_state={"preferences": {"tone": "brief"}})
    )
    assert input_data["state"] == {"preferences": {"tone": "brief"}}


@pytest.mark.asyncio
async def test_no_allow_list_keeps_the_open_dict(graph_service):
    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig

    graph_service.config.runs = RunsConfig()
    input_data, config, _ = await graph_service._prepare_input(
        _input(config={"plan": "pro", **T1}, initial_state={"role": "admin"})
    )
    assert config["plan"] == "pro" and input_data["state"] == {"role": "admin"}


@pytest.mark.asyncio
async def test_invoke_turns_an_unlisted_key_into_422(graph_service):
    from fastapi import HTTPException

    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig

    graph_service.config.runs = RunsConfig(config_keys=frozenset())
    with pytest.raises(HTTPException) as exc_info:
        await graph_service.invoke_graph(_input(config={"plan": "pro"}), {"user_id": "u1"})
    assert exc_info.value.status_code == 422
    graph_service._graph.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_put_state_honours_the_state_allow_list(monkeypatch):
    from fastapi import HTTPException
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RunsConfig

    graph_config = MagicMock(spec=GraphConfig)
    graph_config.runs = RunsConfig(state_keys=frozenset({"context"}))
    container = InjectQ()
    container.bind_instance(GraphConfig, graph_config)
    monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: container))

    service = CheckpointerService.__new__(CheckpointerService)
    service.checkpointer = MagicMock()
    service.checkpointer.aput_state = AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await service.put_state({"thread_id": "t1"}, TRUSTED_USER, {"role": "admin"})
    assert exc_info.value.status_code == 422
    assert "role" in exc_info.value.detail
    service.checkpointer.aput_state.assert_not_called()


# ---------------------------------------------------------------------------
# 422 details: ours are kept in production, arbitrary ValueErrors are not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "exc", "expected"),
    [
        ("production", ValueError("dsn=pg://secret"), "Invalid input provided."),
        ("development", ValueError("dsn=pg://secret"), "dsn=pg://secret"),
        ("production", None, "config keys not allowed by this server: role"),
    ],
)
def test_client_error_detail(monkeypatch, mode, exc, expected):
    from tenxgraph_api.src.app.core.auth.request_config import (
        ClientInputError,
        client_error_detail,
    )
    from tenxgraph_api.src.app.core.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: type("S", (), {"MODE": mode})())
    exc = exc or ClientInputError("config keys not allowed by this server: role")
    assert client_error_detail(exc) == expected


@pytest.mark.asyncio
async def test_our_validation_messages_survive_production(graph_service, monkeypatch):
    from fastapi import HTTPException

    from tenxgraph_api.src.app.core.config import settings as settings_mod
    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig

    monkeypatch.setattr(
        settings_mod, "get_settings", lambda: type("S", (), {"MODE": "production"})()
    )
    graph_service.config.runs = RunsConfig(config_keys=frozenset())
    with pytest.raises(HTTPException) as exc_info:
        await graph_service.invoke_graph(_input(config={"plan": "pro"}), {"user_id": "u1"})
    assert exc_info.value.detail == "config keys not allowed by this server: plan"
