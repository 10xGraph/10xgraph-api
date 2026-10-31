"""Group 3a (storage) API fixes: G-97 write guard, G-123 partial delete, G-40 keys, G-42 boot."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from tenxgraph.core.state import ImageBlock, MediaRef, Message

from tenxgraph_api.src.app import main as app_main
from tenxgraph_api.src.app.core import redis_client
from tenxgraph_api.src.app.core.auth import revocation
from tenxgraph_api.src.app.core.auth.ownership_resolver import ThreadOwnershipResolver
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore, RedisCacheStore
from tenxgraph_api.src.app.core.leases import LeaseManager
from tenxgraph_api.src.app.routers.checkpointer import router as cp_router
from tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service import (
    CheckpointerService,
)
from tenxgraph_api.src.app.routers.graph import thread_lock
from tenxgraph_api.src.app.routers.graph.router import fix_graph
from tenxgraph_api.src.app.routers.graph.run_limits import run_slot_key
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import FixGraphRequestSchema


USER = {"user_id": "alice"}


@pytest.fixture(autouse=True)
def fresh_locks(monkeypatch):
    limiter = LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(thread_lock, "lease_manager", lambda: limiter)
    return limiter


async def _hold(limiter, thread_id):
    """What a running graph holds on the thread."""
    return await limiter.acquire(thread_lock.thread_lock_key(thread_id), 1)


# ---------------------------------------------------------------------------
# G-97: state-changing REST routes take the run lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_refuses_with_409_while_a_run_holds_the_thread(fresh_locks):
    lease = await _hold(fresh_locks, "t1")
    with pytest.raises(HTTPException) as exc:
        async with thread_lock.thread_write_guard("t1"):
            pytest.fail("the body must not run while a run holds the thread")
    assert exc.value.status_code == 409
    lease.release()
    async with thread_lock.thread_write_guard("t1"):
        pass
    # and it lets go afterwards, so a run can start
    again = await _hold(fresh_locks, "t1")
    assert again is not None


@pytest.mark.asyncio
async def test_guard_is_released_when_the_body_raises(fresh_locks):
    with pytest.raises(RuntimeError):
        async with thread_lock.thread_write_guard("t1"):
            raise RuntimeError("boom")
    assert await _hold(fresh_locks, "t1") is not None


def _routes(service):
    from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
        ConfigSchema,
        PutMessagesSchema,
        StateSchema,
    )

    request = MagicMock()
    return {
        "put_state": lambda: cp_router.put_state(
            request, "t1", StateSchema(state={}), service, USER
        ),
        "clear_state": lambda: cp_router.clear_state(request, "t1", service, USER),
        "put_messages": lambda: cp_router.put_messages(
            request,
            "t1",
            PutMessagesSchema(messages=[Message.text_message("x")]),
            service,
            USER,
        ),
        "delete_message": lambda: cp_router.delete_message(
            request, "m1", "t1", ConfigSchema(), service, USER
        ),
        "delete_thread": lambda: cp_router.delete_thread(
            request, "t1", ConfigSchema(), service, USER
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["put_state", "clear_state", "put_messages", "delete_message", "delete_thread"]
)
async def test_checkpointer_write_routes_answer_409_during_a_run(fresh_locks, name):
    service = MagicMock()
    lease = await _hold(fresh_locks, "t1")
    with pytest.raises(HTTPException) as exc:
        await _routes(service)[name]()
    assert exc.value.status_code == 409
    for method in ("put_state", "clear_state", "put_messages", "delete_message", "delete_thread"):
        getattr(service, method).assert_not_called()
    lease.release()


@pytest.mark.asyncio
async def test_fix_route_answers_409_during_a_run(fresh_locks):
    service = MagicMock()
    service.fix_graph = AsyncMock(return_value={})
    lease = await _hold(fresh_locks, "t1")
    with pytest.raises(HTTPException) as exc:
        await fix_graph(MagicMock(), FixGraphRequestSchema(thread_id="t1"), service, USER)
    assert exc.value.status_code == 409
    service.fix_graph.assert_not_called()
    lease.release()


# ---------------------------------------------------------------------------
# G-40: deployment prefix and colon-safe keys
# ---------------------------------------------------------------------------


def test_key_part_keeps_plain_ids_and_separates_colons():
    assert redis_client.key_part("abc-123_x@y.com") == "abc-123_x@y.com"
    assert redis_client.key_part("a:b") != redis_client.key_part("a%3Ab")
    assert ":" not in redis_client.key_part("a:b")


def test_run_slot_key_is_unambiguous():
    assert run_slot_key("a:b") != run_slot_key("a") + ":b"
    assert thread_lock.thread_lock_key("a:b") == "runs:thread:a%3Ab"


@pytest.fixture
def key_prefix(monkeypatch):
    from tenxgraph_api.src.app.core.config.settings import get_settings

    monkeypatch.setattr(get_settings(), "REDIS_KEY_PREFIX", "prod-eu")


def test_prefix_is_applied_to_every_api_redis_key(key_prefix):
    store = RedisCacheStore(client=MagicMock())
    owner_key = store.redis_key(ThreadOwnershipResolver._owner_key("t:1"))
    assert owner_key.startswith("prod-eu:af:authz:owner:id:")
    assert ":" not in owner_key.split(":id:", 1)[1]
    tomb_key = store.redis_key(ThreadOwnershipResolver._tombstone_key("t1"))
    assert tomb_key.startswith("prod-eu:af:authz:owner:tomb:")
    assert store.redis_key(revocation.jti_key("j")).startswith("prod-eu:10xgraph:revoked:jti:")
    assert store.redis_key(revocation.user_key("u:1")) == "prod-eu:10xgraph:revoked:user:u%3A1"
    lease_key = "concurrency:" + thread_lock.thread_lock_key("t1")
    assert store.redis_key(lease_key).startswith("prod-eu:10xgraph:concurrency:")


def test_no_prefix_keeps_the_existing_keys():
    store = RedisCacheStore(client=MagicMock())
    assert store.redis_key(ThreadOwnershipResolver._owner_key("t1")) == "af:authz:owner:id:t1"
    assert store.redis_key(revocation.jti_key("j")) == "10xgraph:revoked:jti:j"


# ---------------------------------------------------------------------------
# G-123: partial failure on thread delete, media linked through PUT state
# ---------------------------------------------------------------------------


def _service(state_context=None, messages=None):
    service = CheckpointerService.__new__(CheckpointerService)
    cp = MagicMock()
    cp.aget_state = AsyncMock(return_value=SimpleNamespace(context=state_context or []))
    cp.alist_messages = AsyncMock(return_value=messages or [])
    cp.aclean_thread = AsyncMock(return_value=None)
    cp.adelete_message = AsyncMock(return_value=None)
    cp.aget_message = AsyncMock(return_value=None)
    service.checkpointer = cp
    service.settings = MagicMock()
    return service


def _img(key, message_id="m1"):
    return Message(
        message_id=message_id,
        role="user",
        content=[ImageBlock(media=MediaRef(kind="url", url=f"graph://media/{key}"))],
    )


KEY = "a" * 32


@pytest.mark.asyncio
async def test_thread_delete_releases_media_linked_through_put_state(monkeypatch):
    media = MagicMock(supports_links=True)
    media.unlink_threads = AsyncMock(return_value=[])
    media.release_thread = AsyncMock(return_value=[])
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: media))
    service = _service(state_context=[_img(KEY)])
    res = await service.delete_thread({"thread_id": "t1"}, USER, "t1")
    media.unlink_threads.assert_awaited_once_with([KEY], "t1")
    assert res.success is True


@pytest.mark.asyncio
async def test_thread_delete_reports_a_release_that_failed(monkeypatch):
    media = MagicMock(supports_links=True)
    media.unlink_threads = AsyncMock(return_value=[KEY])
    media.release_thread = AsyncMock(return_value=[])
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: media))
    service = _service(state_context=[_img(KEY)])
    res = await service.delete_thread({"thread_id": "t1"}, USER, "t1")
    assert res.success is False
    assert res.data["deleted"] is True and res.data["cleanup_failed"] == ["files"]
    assert "files" in res.message


@pytest.mark.asyncio
async def test_thread_delete_reports_a_trace_cleanup_failure(monkeypatch):
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: None))
    store = MagicMock()
    store.delete_thread.side_effect = RuntimeError("disk full")
    container = MagicMock()
    container.try_get.return_value = store
    monkeypatch.setattr("injectq.InjectQ.get_instance", lambda: container)
    res = await _service().delete_thread({"thread_id": "t1"}, USER, "t1")
    assert res.success is False and "traces" in res.data["cleanup_failed"]


@pytest.mark.asyncio
async def test_clean_thread_delete_is_still_a_plain_success(monkeypatch):
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: None))
    container = MagicMock()
    container.try_get.return_value = None
    monkeypatch.setattr("injectq.InjectQ.get_instance", lambda: container)
    res = await _service().delete_thread({"thread_id": "t1"}, USER, "t1")
    assert res.success is True


# ---------------------------------------------------------------------------
# G-11: files go only after the core delete has removed the message from state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_media_is_released_only_after_the_core_delete(monkeypatch):
    order: list[str] = []
    message = _img(KEY, "m1")
    media = MagicMock(supports_links=True)
    media.unlink_threads = AsyncMock(side_effect=lambda *a: order.append("unlink") or [])
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: media))
    service = _service(state_context=[message], messages=[message])
    service.checkpointer.aget_message = AsyncMock(return_value=message)
    service.checkpointer.adelete_message = AsyncMock(side_effect=lambda *a: order.append("delete"))
    await service.delete_message({"thread_id": "t1"}, USER, "m1")
    assert order == ["delete", "unlink"]
    service.checkpointer.adelete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_media_still_in_state_through_another_message_is_kept(monkeypatch):
    gone, other = _img(KEY, "m1"), _img(KEY, "m2")
    media = MagicMock(supports_links=True)
    media.unlink_threads = AsyncMock(return_value=[])
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: media))
    service = _service(state_context=[gone, other], messages=[])
    service.checkpointer.aget_message = AsyncMock(return_value=gone)
    await service.delete_message({"thread_id": "t1"}, USER, "m1")
    media.unlink_threads.assert_not_awaited()


# ---------------------------------------------------------------------------
# G-42: the checkpointer schema is prepared at boot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_runs_the_checkpointer_setup(monkeypatch):
    checkpointer = MagicMock()
    checkpointer.asetup = AsyncMock(return_value=True)
    container = MagicMock()
    container.try_get.side_effect = lambda name: checkpointer if name == "checkpointer" else None
    monkeypatch.setattr(app_main, "container", container)
    await app_main._prepare_checkpointer()
    checkpointer.asetup.assert_awaited_once()


@pytest.mark.asyncio
async def test_boot_survives_a_failing_setup(monkeypatch):
    checkpointer = MagicMock()
    checkpointer.asetup = AsyncMock(side_effect=ConnectionError("postgres down"))
    container = MagicMock()
    container.try_get.return_value = checkpointer
    monkeypatch.setattr(app_main, "container", container)
    await app_main._prepare_checkpointer()  # logs, does not raise


@pytest.mark.asyncio
async def test_boot_skips_a_checkpointer_without_setup(monkeypatch):
    container = MagicMock()
    container.try_get.return_value = SimpleNamespace()
    monkeypatch.setattr(app_main, "container", container)
    await app_main._prepare_checkpointer()
