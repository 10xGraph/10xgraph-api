"""Audit ``change`` events: one after a successful write or delete, none after a failure."""

# ruff: noqa: S101

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from tenxgraph.core.authz import Identity

from tenxgraph_api import AuditEvent, BaseAuditSink
from tenxgraph_api.src.app.core.audit import set_sinks
from tenxgraph_api.src.app.core.auth import revocation
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.routers.checkpointer import router as cp
from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
    ConfigSchema,
    FeedbackSchema,
    StateSchema,
)
from tenxgraph_api.src.app.routers.media import router as media
from tenxgraph_api.src.app.routers.store.schemas.store_schemas import (
    DeleteMemorySchema,
    ForgetMemorySchema,
    StoreMemorySchema,
    UpdateMemorySchema,
)
from tests.unit_tests.jobs.test_routes import ADD, Env


store = importlib.import_module("tenxgraph_api.src.app.routers.store.router")


class _Recorder(BaseAuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def sink():
    recorder = _Recorder()
    set_sinks([recorder])
    yield recorder
    set_sinks([])


ME = Identity(user_id="alice")


def _changes(sink: _Recorder) -> list[AuditEvent]:
    return [e for e in sink.events if e.event == "change"]


def _seen(sink: _Recorder) -> list[tuple]:
    for e in _changes(sink):
        assert e.result == "success"
        assert e.user_id == "alice"
    return [(e.action, e.resource) for e in _changes(sink)]


@pytest.mark.asyncio
async def test_checkpointer_changes(sink):
    service = AsyncMock()
    request = MagicMock()
    kw = {"request": request, "service": service, "identity": ME}
    await cp.put_state(thread_id="t1", payload=StateSchema(state={"a": 1}), **kw)
    await cp.clear_state(thread_id="t1", **kw)
    await cp.put_message_feedback(
        thread_id="t1", message_id="m1", payload=FeedbackSchema(feedback={"up": True}), **kw
    )
    await cp.delete_message(thread_id="t1", message_id="m1", payload=ConfigSchema(), **kw)
    await cp.delete_thread(thread_id="t1", payload=ConfigSchema(), **kw)
    assert _seen(sink) == [
        ("threads:write", "thread/t1"),
        ("threads:delete", "thread/t1"),
        ("threads:write", "message/m1"),
        ("threads:delete", "message/m1"),
        ("threads:delete", "thread/t1"),
    ]


@pytest.mark.asyncio
async def test_checkpointer_failure_emits_nothing(sink):
    service = AsyncMock()
    service.delete_thread.side_effect = RuntimeError("down")
    with pytest.raises(RuntimeError):
        await cp.delete_thread(
            request=MagicMock(), thread_id="t1", payload=ConfigSchema(), service=service,
            identity=ME,
        )  # fmt: skip
    with pytest.raises(HTTPException):
        await cp.delete_thread(
            request=MagicMock(), thread_id="  ", payload=ConfigSchema(), service=service,
            identity=ME,
        )  # fmt: skip
    assert sink.events == []


@pytest.mark.asyncio
async def test_store_changes(sink):
    service = AsyncMock()
    service.store_memory.return_value = MagicMock(memory_id="mem1")
    kw = {"request": MagicMock(), "service": service, "identity": ME}
    await store.create_memory(payload=StoreMemorySchema(content="secret text"), **kw)
    await store.update_memory(memory_id="mem1", payload=UpdateMemorySchema(content="x"), **kw)
    await store.delete_memory(memory_id="mem1", payload=DeleteMemorySchema(), **kw)
    await store.forget_memory(payload=ForgetMemorySchema(), **kw)
    assert _seen(sink) == [
        ("memory:write", "memory/mem1"),
        ("memory:write", "memory/mem1"),
        ("memory:delete", "memory/mem1"),
        ("memory:delete", "memory"),
    ]
    assert "secret" not in "".join(e.model_dump_json() for e in sink.events)


@pytest.mark.asyncio
async def test_store_failure_emits_nothing(sink):
    service = AsyncMock()
    service.delete_memory.side_effect = RuntimeError("down")
    with pytest.raises(RuntimeError):
        await store.delete_memory(
            request=MagicMock(), memory_id="m", payload=None, service=service, identity=ME
        )
    with pytest.raises(HTTPException):
        await store.delete_memory(
            request=MagicMock(), memory_id=" ", payload=None, service=service, identity=ME
        )
    assert sink.events == []


@pytest.mark.asyncio
async def test_media_changes(sink):
    service = AsyncMock()
    service.upload_file.return_value = {
        "file_id": "f1",
        "filename": "private-name.txt",
        "mime_type": "text/plain",
        "size_bytes": 4,
        "extracted_text": None,
        "url": "graph://media/f1",
    }
    service.delete_file.return_value = True
    upload = MagicMock(spec=UploadFile)
    upload.filename = "private-name.txt"
    upload.content_type = "text/plain"
    upload.read = AsyncMock(side_effect=[b"data", b""])
    await media.upload_file(request=MagicMock(), file=upload, service=service, identity=ME)
    await media.delete_file(request=MagicMock(), file_id="f1", service=service, identity=ME)
    seen = _seen(sink)
    assert seen == [("files:upload", "file/f1"), ("files:delete", "file/f1")]
    assert "private-name" not in "".join(e.model_dump_json() for e in sink.events)


@pytest.mark.asyncio
async def test_media_failure_emits_nothing(sink):
    service = AsyncMock()
    service.delete_file.return_value = False
    with pytest.raises(HTTPException):
        await media.delete_file(request=MagicMock(), file_id="f1", service=service, identity=ME)
    service.upload_file.side_effect = ValueError("too large")
    upload = MagicMock(spec=UploadFile)
    upload.filename = "a.txt"
    upload.content_type = "text/plain"
    upload.read = AsyncMock(side_effect=[b"data", b""])
    with pytest.raises(HTTPException):
        await media.upload_file(request=MagicMock(), file=upload, service=service, identity=ME)
    assert sink.events == []


def test_task_changes(sink, monkeypatch):
    env = Env(monkeypatch)
    run = env.call("POST", f"/v1/tasks/{ADD}", user="alice", json={"args": [1, 2]})
    task_id = run.json()["data"]["task_id"]
    sched = env.call(
        "POST", f"/v1/tasks/{ADD}", user="alice", json={"args": [1, 2], "schedule": {"every": 3}}
    )
    schedule_id = sched.json()["data"]["schedule_id"]
    env.call("PATCH", f"/v1/tasks/{schedule_id}", user="alice", json={"schedule": {"every": 5}})
    env.call("POST", f"/v1/tasks/{schedule_id}/cancel", user="alice")
    assert _seen(sink) == [
        ("tasks:trigger", f"task/{task_id}"),
        ("tasks:trigger", f"schedule/{schedule_id}"),
        ("tasks:trigger", f"schedule/{schedule_id}"),
        ("tasks:cancel", f"schedule/{schedule_id}"),
    ]


def test_task_failures_emit_nothing(sink, monkeypatch):
    env = Env(monkeypatch)
    assert (
        env.call(
            "POST", f"/v1/tasks/{ADD}", user="alice", json={"kwargs": {"config": 1}}
        ).status_code
        == 422
    )
    assert env.call("POST", "/v1/tasks/missing/cancel", user="alice").status_code == 404
    assert (
        env.call(
            "PATCH", "/v1/tasks/missing", user="alice", json={"schedule": {"every": 5}}
        ).status_code
        == 404
    )
    assert _changes(sink) == []


@pytest.mark.asyncio
async def test_revocation_events(sink):
    cache = InMemoryCacheStore()
    await revocation.revoke_token(cache, "jti-1")
    await revocation.revoke_user(cache, "bob")
    assert [(e.event, e.result, e.action, e.resource) for e in sink.events] == [
        ("change", "success", "tokens:revoke", "token/jti-1"),
        ("change", "success", "users:revoke", "user/bob"),
    ]
    # Outside a request there is no context: those fields are null.
    assert sink.events[0].request_id is None
    assert sink.events[0].transport is None


@pytest.mark.asyncio
async def test_revocation_failure_emits_nothing(sink):
    cache = AsyncMock()
    cache.set.side_effect = RuntimeError("down")
    with pytest.raises(RuntimeError):
        await revocation.revoke_token(cache, "jti-1")
    assert sink.events == []
