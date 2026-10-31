"""G-107: opt-in Idempotency-Key on invoke, stream and WebSocket runs."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from tenxgraph.core.state import StreamChunk, StreamEvent

from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.core.leases import LeaseManager
from tenxgraph_api.src.app.routers.graph import idempotency
from tenxgraph_api.src.app.routers.graph.router import router as graph_router
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInvokeOutputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tests.unit_tests.test_concurrency_limiter import RecordingStore

from .conftest import build_app, make_client, user_headers


MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
OTHER = [{"role": "user", "content": [{"type": "text", "text": "different"}]}]
KEY = {"Idempotency-Key": "k-1"}


@pytest.fixture(autouse=True)
def _app_logs_reach_caplog(monkeypatch):
    """The app's log setup (run by any test that builds the app) stops the app loggers
    from propagating; caplog listens on the root logger."""
    for name in ("tenxgraph_api", "10xgraph-api"):
        monkeypatch.setattr(logging.getLogger(name), "propagate", True)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Fresh in-process records and leases for every test."""
    leases = LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(idempotency, "lease_manager", lambda: leases)
    return leases


def _client():
    runs = {"count": 0}

    async def _stream(*_a, **_k):
        runs["count"] += 1
        yield json.dumps({"event": "message", "thread_id": "t1"}, separators=(",", ":")) + "\n"
        state = StreamChunk(event=StreamEvent.STATE, thread_id="t1").model_dump_json()
        yield state + "\n"
        done = StreamChunk(event=StreamEvent.UPDATES, data={"status": "done"}, thread_id="t1")
        yield done.model_dump_json() + "\n"

    async def _invoke(*_a, **_k):
        runs["count"] += 1
        return GraphInvokeOutputSchema(messages=[], meta={"thread_id": "t1"})

    service = MagicMock(spec=GraphService)
    service.check_media_urls = MagicMock()
    service.check_resume_target = AsyncMock()
    service.runs_policy = MagicMock(return_value=RunsConfig())
    service.invoke_graph = AsyncMock(side_effect=_invoke)
    service.stream_graph = MagicMock(side_effect=_stream)
    app = build_app(routers=[graph_router], extra_bindings={GraphService: service})
    return make_client(app), runs, service


def _post(client, path, key=KEY, messages=MESSAGES, user="alice"):
    headers = {**user_headers(user), **(key or {})}
    return client.post(path, json={"messages": messages}, headers=headers)


def test_invoke_same_key_returns_stored_result_without_running_again():
    client, runs, _ = _client()
    first = _post(client, "/v1/graph/invoke")
    second = _post(client, "/v1/graph/invoke")
    assert first.status_code == second.status_code == 200
    assert runs["count"] == 1
    assert second.json()["data"]["meta"] == first.json()["data"]["meta"]


def test_no_header_runs_every_time():
    client, runs, _ = _client()
    _post(client, "/v1/graph/invoke", key=None)
    _post(client, "/v1/graph/invoke", key=None)
    assert runs["count"] == 2


def test_different_body_under_the_same_key_is_422():
    client, runs, _ = _client()
    _post(client, "/v1/graph/invoke")
    resp = _post(client, "/v1/graph/invoke", messages=OTHER)
    assert resp.status_code == 422
    assert runs["count"] == 1


def test_key_is_per_user():
    client, runs, _ = _client()
    _post(client, "/v1/graph/invoke", user="alice")
    _post(client, "/v1/graph/invoke", user="bob")
    assert runs["count"] == 2


def test_malformed_key_is_422():
    client, _, _ = _client()
    resp = _post(client, "/v1/graph/invoke", key={"Idempotency-Key": "a" * 300})
    assert resp.status_code == 422


def test_failed_run_frees_the_key():
    client, runs, service = _client()
    service.invoke_graph = AsyncMock(side_effect=RuntimeError("boom"))
    assert _post(client, "/v1/graph/invoke").status_code == 500
    service.invoke_graph = AsyncMock(return_value=GraphInvokeOutputSchema(messages=[]))
    assert _post(client, "/v1/graph/invoke").status_code == 200


def test_stream_replays_final_state_and_done():
    client, runs, _ = _client()
    first = _post(client, "/v1/graph/stream")
    second = _post(client, "/v1/graph/stream")
    assert runs["count"] == 1
    lines = [json.loads(x) for x in second.text.splitlines()]
    assert [x["event"] for x in lines] == ["state", "updates"]
    assert lines[-1]["data"] == {"status": "done"}
    assert first.text.count("\n") == 3


@pytest.mark.asyncio
async def test_running_key_is_409_with_retry_after():
    user = {"user_id": "alice"}
    run, record = await idempotency.check(user, "k", "invoke", {"a": 1})
    assert run is not None and record is None
    with pytest.raises(idempotency.IdempotencyConflict) as exc:
        await idempotency.check(user, "k", "invoke", {"a": 1})
    assert exc.value.status_code == 409
    assert exc.value.headers["Retry-After"]
    await run.complete("{}")
    run.release()
    again, stored = await idempotency.check(user, "k", "invoke", {"a": 1})
    assert again is None and stored["stored"] is True


@pytest.mark.asyncio
async def test_oversized_result_is_marked_not_stored():
    user = {"user_id": "alice"}
    run, _ = await idempotency.check(user, "big", "invoke", {})
    await run.complete("x" * (idempotency.MAX_STORED_BYTES + 1))
    run.release()
    _, record = await idempotency.check(user, "big", "invoke", {})
    assert record["stored"] is False


@pytest.mark.asyncio
async def test_records_live_in_the_cache_store(monkeypatch):
    store = RecordingStore()
    monkeypatch.setattr(idempotency, "lease_manager", lambda: LeaseManager(store))
    user = {"user_id": "alice"}
    run, _ = await idempotency.check(user, "r", "invoke", {})
    await run.complete("{}")
    run.release()
    sets = [call for call in store.calls if call[0] == "set"]
    assert sets == [("set", "idempotency:alice:r", 86400)]
    assert ("acquire", "concurrency:runs:idem:alice:r") in store.calls
    assert json.loads(await store.get("idempotency:alice:r"))["fp"] == run.fp


@pytest.mark.asyncio
async def test_cache_store_errors_run_without_idempotency(monkeypatch, caplog):
    store = RecordingStore()
    store.fail = True
    monkeypatch.setattr(idempotency, "lease_manager", lambda: LeaseManager(store))
    user = {"user_id": "alice"}
    with caplog.at_level(logging.WARNING):
        run, record = await idempotency.check(user, "down", "invoke", {})
        assert run is not None and record is None  # the request runs
        await run.complete("{}")  # storing fails too: logged, not raised
        run.release()
    assert "Idempotency lookup failed; running without it" in caplog.text
    assert "Idempotency record could not be stored" in caplog.text
    assert "Shared concurrency limit unavailable" in caplog.text


def test_ws_frame_carries_the_key():
    from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import WsGraphInputSchema

    parsed = WsGraphInputSchema(messages=MESSAGES, idempotency_key="abc")
    assert parsed.idempotency_key == "abc"
    assert WsGraphInputSchema(messages=MESSAGES).idempotency_key is None


@pytest.mark.asyncio
async def test_ag_ui_repeat_of_a_finished_key_is_409_and_other_body_422():
    pytest.importorskip("ag_ui")
    from types import SimpleNamespace

    from fastapi import HTTPException

    from tenxgraph_api.src.app.routers.ag_ui.router import run_ag_ui

    body = {
        "threadId": "t1",
        "runId": "r1",
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    user = {"user_id": "alice"}
    run, _ = await idempotency.check(user, "ak", "ag-ui", body)
    await run.complete(None)
    run.release()

    def request(payload):
        req = MagicMock()
        req.json = AsyncMock(return_value=payload)
        req.headers = {"Idempotency-Key": "ak"}
        return req

    config = SimpleNamespace(auth_config=lambda: None, ag_ui=SimpleNamespace())
    with pytest.raises(HTTPException) as same:
        await run_ag_ui(request(body), MagicMock(), user, MagicMock(), config)
    assert same.value.status_code == 409
    with pytest.raises(HTTPException) as other:
        await run_ag_ui(request({**body, "runId": "r2"}), MagicMock(), user, MagicMock(), config)
    assert other.value.status_code == 422
