"""Media security round: external URL pre-check (Q1), upload lifecycle (Q15), magic bytes (Q16)."""

# ruff: noqa: S101, PLR2004, SLF001

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from tenxgraph.core.state import Message
from tenxgraph.core.state.message_block import ImageBlock, MediaRef, TextBlock
from tenxgraph.storage.media.storage.memory_store import InMemoryMediaStore

from tenxgraph_api.src.app.core.auth.authorization import all_scopes
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, MediaConfig
from tenxgraph_api.src.app.core.config.media_settings import MediaSettings
from tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service import (
    CheckpointerService,
)
from tenxgraph_api.src.app.routers.graph.router import (
    _ws_media_urls_allowed,
    invoke_graph,
    stream_graph,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    MediaURLBlockedError,
    check_external_media_urls,
    external_media_urls,
    resolve_media_policy,
)
from tenxgraph_api.src.app.routers.media import MediaService, _BoundedCache
from tenxgraph_api.src.app.routers.media.router import delete_file, upload_file
from tenxgraph_api.src.app.utils.media.signatures import content_matches_mime


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Blocked(ValueError):
    """Stands in for core's MediaURLError (a ValueError)."""


class _Policy:
    """Allows https://ok.example/* and gs://allowed/*, blocks everything else."""

    def check_url(self, url: str) -> None:
        if not url.startswith(("https://ok.example/", "gs://allowed/")):
            raise _Blocked(f"media URL not allowed: {url}")


class LinkStore(InMemoryMediaStore):
    """A media store with the new core ``update_metadata``."""

    async def update_metadata(self, storage_key: str, updates: dict[str, Any]) -> bool:
        if storage_key not in self._data:
            return False
        data, mime, meta = self._data[storage_key]
        meta = dict(meta)
        for key, value in updates.items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        self._data[storage_key] = (data, mime, meta)
        return True


def _media(store: Any = None) -> MediaService:
    svc = MediaService.__new__(MediaService)
    svc._settings = MediaSettings(_env_file=None, MEDIA_REQUIRE_OWNER=True)
    svc._checkpointer = MagicMock(
        aget_cache_value=AsyncMock(return_value=None), aput_cache_value=AsyncMock()
    )
    svc._store = store if store is not None else LinkStore()
    svc._extraction_cache = _BoundedCache(8)
    svc._signed_url_cache = _BoundedCache(8)
    svc._links_notice_logged = False
    return svc


@pytest.fixture
def delete_setting(monkeypatch):
    """Set ``media.delete_with_thread`` as the app container would serve it."""

    def set_to(value: bool) -> None:
        config = MagicMock(spec=GraphConfig)
        config.media = MediaConfig(delete_with_thread=value)
        container = MagicMock()
        container.try_get.return_value = config
        monkeypatch.setattr("injectq.InjectQ.get_instance", lambda: container)

    set_to(True)
    return set_to


async def _upload(media: MediaService, owner: str = "alice") -> str:
    media._pipeline = SimpleNamespace(handling="skip")
    result = await media.upload_file(b"\x89PNG\r\n\x1a\n", "a.png", "image/png", user_id=owner)
    return result["file_id"]


def _img(url: str, message_id: str = "m1") -> Message:
    return Message(
        message_id=message_id,
        role="user",
        content=[ImageBlock(media=MediaRef(kind="url", url=url))],
    )


def _graph_service(policy: Any = None, media: Any = None) -> GraphService:
    srv = GraphService.__new__(GraphService)
    srv._graph = SimpleNamespace(media_policy=policy if policy is not None else _Policy())
    srv.checkpointer = MagicMock()
    srv.checkpointer.aget_thread_owner = AsyncMock(return_value=None)
    srv.config = MagicMock()
    srv.thread_name_generator = None
    srv._media_service = media
    srv._telemetry = None
    return srv


def _bad_input() -> GraphInputSchema:
    return GraphInputSchema(messages=[_img("http://169.254.169.254/latest")])


# ---------------------------------------------------------------------------
# Q1: external URL pre-check
# ---------------------------------------------------------------------------


def test_external_urls_are_found_in_media_blocks_only():
    msg = Message(
        role="user",
        content=[
            TextBlock(text="http://not-media.example"),
            ImageBlock(media=MediaRef(kind="url", url="https://a.example/x.png")),
            ImageBlock(media=MediaRef(kind="url", url="graph://media/abc")),
            ImageBlock(media=MediaRef(kind="url", url="agentflow://media/abc")),
            ImageBlock(media=MediaRef(kind="url", url="data:image/png;base64,AAAA")),
            ImageBlock(media=MediaRef(kind="url", url="gs://bucket/o.png")),
        ],
    )
    assert external_media_urls(msg) == ["https://a.example/x.png", "gs://bucket/o.png"]
    assert external_media_urls({"content": [{"type": "text", "text": "x"}]}) == []


def test_blocked_url_is_400_with_the_policy_message():
    with pytest.raises(MediaURLBlockedError) as exc:
        check_external_media_urls([_img("http://169.254.169.254/x")], _Policy())
    assert exc.value.status_code == 400
    assert "169.254.169.254" in exc.value.detail
    assert isinstance(exc.value, ValueError)


def test_allowed_and_internal_urls_pass():
    check_external_media_urls(
        [_img("https://ok.example/a.png"), _img("graph://media/k"), _img("gs://allowed/o.png")],
        _Policy(),
    )


def test_gs_url_is_checked_and_an_allowlisted_bucket_accepted():
    with pytest.raises(MediaURLBlockedError):
        check_external_media_urls([_img("gs://other/o.png")], _Policy())
    check_external_media_urls([_img("gs://allowed/o.png")], _Policy())


def test_no_policy_skips_the_check():
    check_external_media_urls([_img("http://169.254.169.254/x")], None)


def test_policy_comes_from_the_graph():
    policy = _Policy()
    assert resolve_media_policy(SimpleNamespace(media_policy=policy)) is policy


def test_old_core_falls_back_to_the_default_policy(monkeypatch):
    default = _Policy()
    fake = SimpleNamespace(MediaFetchPolicy=lambda: default)
    monkeypatch.setitem(sys.modules, "tenxgraph.storage.media", fake)
    assert resolve_media_policy(SimpleNamespace()) is default


def test_old_core_without_any_policy_skips(monkeypatch):
    monkeypatch.setitem(sys.modules, "tenxgraph.storage.media", SimpleNamespace())
    assert resolve_media_policy(SimpleNamespace()) is None
    srv = _graph_service()
    srv._graph = SimpleNamespace()
    srv.check_media_urls([_img("http://169.254.169.254/x")])  # no raise


@pytest.mark.asyncio
async def test_invoke_route_answers_400():
    with pytest.raises(HTTPException) as exc:
        await invoke_graph(MagicMock(), _bad_input(), _graph_service(), {"user_id": "u1"})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_invoke_service_backstop_answers_400():
    with pytest.raises(HTTPException) as exc:
        await _graph_service().invoke_graph(_bad_input(), {"user_id": "u1"})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_stream_route_answers_400_before_the_response_starts():
    with pytest.raises(HTTPException) as exc:
        await stream_graph(_bad_input(), _graph_service(), {"user_id": "u1"})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_ws_sends_an_error_chunk_for_a_blocked_url():
    ws = MagicMock()
    ws.send_text = AsyncMock()
    assert await _ws_media_urls_allowed(ws, _graph_service(), _bad_input()) is False
    sent = ws.send_text.await_args.args[0]
    assert '"error"' in sent
    assert "169.254.169.254" in sent
    ok = GraphInputSchema(messages=[_img("https://ok.example/a.png")])
    assert await _ws_media_urls_allowed(ws, _graph_service(), ok) is True


@pytest.mark.asyncio
async def test_ag_ui_route_answers_400():
    pytest.importorskip("ag_ui")
    from tenxgraph_api.src.app.routers.ag_ui.router import run_ag_ui

    body = {
        "threadId": "t1",
        "runId": "r1",
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
        "messages": [
            {
                "id": "m1",
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "value": "http://169.254.169.254/x",
                            "mimeType": "image/png",
                        },
                    }
                ],
            }
        ],
    }
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    config = SimpleNamespace(auth_config=lambda: None)
    with pytest.raises(HTTPException) as exc:
        await run_ag_ui(request, _graph_service(), {"user_id": "u1"}, MagicMock(), config)
    assert exc.value.status_code == 400


@pytest.fixture
def checkpointer_service(monkeypatch):
    service = CheckpointerService.__new__(CheckpointerService)
    cp = MagicMock()
    cp.aget_state = AsyncMock(return_value=None)
    cp.aget_state_cache = AsyncMock(return_value=None)
    cp.aput_state = AsyncMock(side_effect=lambda cfg, state: state)
    cp.aput_state_cache = AsyncMock()
    cp.aput_messages = AsyncMock(return_value=[])
    cp.aget_thread_owner = AsyncMock(return_value="u1")
    service.checkpointer = cp
    service.settings = MagicMock()
    monkeypatch.setattr(CheckpointerService, "_graph_service", staticmethod(_graph_service))
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: None))
    return service


_BLOCKED_CTX = {
    "role": "user",
    "content": [{"type": "image", "media": {"kind": "url", "url": "http://169.254.169.254/x"}}],
}


@pytest.mark.asyncio
async def test_put_state_answers_400(checkpointer_service):
    with pytest.raises(HTTPException) as exc:
        await checkpointer_service.put_state(
            {"thread_id": "t1"}, {"user_id": "u1"}, {"context": [_BLOCKED_CTX]}
        )
    assert exc.value.status_code == 400
    checkpointer_service.checkpointer.aput_state.assert_not_called()


@pytest.mark.asyncio
async def test_post_messages_answers_400(checkpointer_service):
    with pytest.raises(HTTPException) as exc:
        await checkpointer_service.put_messages(
            {"thread_id": "t1"}, {"user_id": "u1"}, [_img("http://169.254.169.254/x")]
        )
    assert exc.value.status_code == 400
    checkpointer_service.checkpointer.aput_messages.assert_not_called()


@pytest.mark.asyncio
async def test_thread_writes_skip_the_check_when_no_policy_exists(
    checkpointer_service, monkeypatch
):
    monkeypatch.setattr(
        CheckpointerService,
        "_graph_service",
        staticmethod(lambda: SimpleNamespace(media_policy=None)),
    )
    await checkpointer_service.put_messages(
        {"thread_id": "t1"}, {"user_id": "u1"}, [_img("http://169.254.169.254/x")]
    )
    checkpointer_service.checkpointer.aput_messages.assert_awaited_once()


# ---------------------------------------------------------------------------
# Q15: links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_starts_with_an_empty_thread_list():
    media = _media()
    key = await _upload(media)
    assert (await media.store.get_metadata(key))["threads"] == []


@pytest.mark.asyncio
async def test_accepted_graph_message_links_the_thread():
    media = _media()
    key = await _upload(media)
    srv = _graph_service(media=media)
    media.ensure_can_access = AsyncMock()
    graph_input = GraphInputSchema(
        messages=[_img(f"graph://media/{key}")], config={"thread_id": "t1"}
    )
    await srv._prepare_input(graph_input, "alice")
    await srv._prepare_input(graph_input, "alice")  # twice: still one link
    assert (await media.store.get_metadata(key))["threads"] == ["t1"]


@pytest.mark.asyncio
async def test_legacy_url_form_is_linked_too():
    media = _media()
    key = await _upload(media)
    srv = _graph_service(media=media)
    graph_input = GraphInputSchema(
        messages=[_img(f"agentflow://media/{key}")], config={"thread_id": "t9"}
    )
    await srv._prepare_input(graph_input, "alice")
    assert (await media.store.get_metadata(key))["threads"] == ["t9"]


@pytest.mark.asyncio
async def test_only_the_owner_gets_a_link():
    media = _media()
    key = await _upload(media, owner="alice")
    await media.link_threads([key], "t1", "mallory")
    assert (await media.store.get_metadata(key))["threads"] == []


@pytest.mark.asyncio
async def test_file_without_threads_key_is_not_tracked():
    media = _media()
    key = await media.store.store(b"x", "image/png", metadata={"owner_id": "alice"})
    await media.link_threads([key], "t1", "alice")
    assert "threads" not in await media.store.get_metadata(key)


@pytest.mark.asyncio
async def test_post_messages_links_the_thread(checkpointer_service, monkeypatch):
    media = _media()
    key = await _upload(media)
    media.ensure_can_access = AsyncMock()
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: media))
    await checkpointer_service.put_messages(
        {"thread_id": "t1"}, {"user_id": "alice"}, [_img(f"graph://media/{key}")]
    )
    assert (await media.store.get_metadata(key))["threads"] == ["t1"]


@pytest.mark.asyncio
async def test_put_state_links_the_thread(checkpointer_service, monkeypatch):
    media = _media()
    key = await _upload(media)
    media.ensure_can_access = AsyncMock()
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: media))
    context = {
        "role": "user",
        "content": [{"type": "image", "media": {"kind": "url", "url": f"graph://media/{key}"}}],
    }
    await checkpointer_service.put_state(
        {"thread_id": "t7"}, {"user_id": "alice"}, {"context": [context]}
    )
    assert (await media.store.get_metadata(key))["threads"] == ["t7"]


@pytest.mark.asyncio
async def test_old_core_store_tracks_nothing_and_logs_once(caplog):
    class OldStore(InMemoryMediaStore):
        pass

    media = _media(OldStore())
    assert not media.supports_links
    key = await _upload(media)
    assert "threads" not in await media.store.get_metadata(key)
    with caplog.at_level("INFO", logger="tenxgraph_api.media"):
        await media.link_threads([key], "t1", "alice")
        await media.link_threads([key], "t1", "alice")
    assert sum("update_metadata" in r.message for r in caplog.records) == 1
    await media.unlink_threads([key], "t1")
    assert await media.store.exists(key)


# ---------------------------------------------------------------------------
# Q15: cleanup on delete
# ---------------------------------------------------------------------------


def _thread_service(monkeypatch, media, messages: list[Message]) -> CheckpointerService:
    """A checkpointer service over an in-memory list of messages for thread ``t1``."""
    service = CheckpointerService.__new__(CheckpointerService)
    cp = MagicMock()
    state = {"messages": list(messages)}

    async def alist(cfg, search, offset, limit):
        return state["messages"][offset : offset + limit]

    async def aget(cfg, message_id):
        return next(m for m in state["messages"] if str(m.message_id) == str(message_id))

    async def adelete(cfg, message_id):
        state["messages"] = [m for m in state["messages"] if str(m.message_id) != str(message_id)]

    async def aclean(cfg):
        state["messages"] = []

    cp.alist_messages = AsyncMock(side_effect=alist)
    cp.aget_message = AsyncMock(side_effect=aget)
    cp.adelete_message = AsyncMock(side_effect=adelete)
    cp.aclean_thread = AsyncMock(side_effect=aclean)
    service.checkpointer = cp
    service.settings = MagicMock()
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: media))
    return service


_USER = {"user_id": "alice"}


@pytest.mark.asyncio
async def test_thread_delete_removes_files_with_no_thread_left(monkeypatch, delete_setting):
    media = _media()
    key = await _upload(media)
    await media.link_threads([key], "t1", "alice")
    service = _thread_service(monkeypatch, media, [_img(f"graph://media/{key}")])
    await service.delete_thread({"thread_id": "t1"}, _USER, "t1")
    assert not await media.store.exists(key)


@pytest.mark.asyncio
async def test_shared_file_stays_until_the_last_thread_goes(monkeypatch, delete_setting):
    media = _media()
    key = await _upload(media)
    await media.link_threads([key], "t1", "alice")
    await media.link_threads([key], "t2", "alice")
    ref = _img(f"graph://media/{key}")
    await _thread_service(monkeypatch, media, [ref]).delete_thread({"thread_id": "t1"}, _USER, "t1")
    assert (await media.store.get_metadata(key))["threads"] == ["t2"]
    await _thread_service(monkeypatch, media, [ref]).delete_thread({"thread_id": "t2"}, _USER, "t2")
    assert not await media.store.exists(key)


@pytest.mark.asyncio
async def test_thread_delete_pages_through_all_messages(monkeypatch, delete_setting):
    media = _media()
    key = await _upload(media)
    await media.link_threads([key], "t1", "alice")
    filler = [
        Message(message_id=f"f{i}", role="user", content=[TextBlock(text="x")]) for i in range(1100)
    ]
    service = _thread_service(monkeypatch, media, [*filler, _img(f"graph://media/{key}")])
    await service.delete_thread({"thread_id": "t1"}, _USER, "t1")
    assert not await media.store.exists(key)


@pytest.mark.asyncio
async def test_legacy_file_is_never_auto_deleted(monkeypatch, delete_setting):
    media = _media()
    key = await media.store.store(b"x", "image/png", metadata={"owner_id": "alice"})
    service = _thread_service(monkeypatch, media, [_img(f"graph://media/{key}")])
    await service.delete_thread({"thread_id": "t1"}, _USER, "t1")
    assert await media.store.exists(key)


@pytest.mark.asyncio
async def test_setting_false_keeps_the_file_but_drops_the_link(monkeypatch, delete_setting):
    delete_setting(False)
    media = _media()
    key = await _upload(media)
    await media.link_threads([key], "t1", "alice")
    service = _thread_service(monkeypatch, media, [_img(f"graph://media/{key}")])
    await service.delete_thread({"thread_id": "t1"}, _USER, "t1")
    assert (await media.store.get_metadata(key))["threads"] == []


@pytest.mark.asyncio
async def test_message_delete_keeps_a_file_another_message_uses(monkeypatch, delete_setting):
    media = _media()
    key = await _upload(media)
    await media.link_threads([key], "t1", "alice")
    service = _thread_service(
        monkeypatch,
        media,
        [_img(f"graph://media/{key}", "m1"), _img(f"graph://media/{key}", "m2")],
    )
    await service.delete_message({"thread_id": "t1"}, _USER, "m1")
    assert (await media.store.get_metadata(key))["threads"] == ["t1"]
    await service.delete_message({"thread_id": "t1"}, _USER, "m2")
    assert not await media.store.exists(key)


@pytest.mark.asyncio
async def test_cleanup_failures_never_fail_the_delete(monkeypatch, delete_setting):
    media = _media()
    key = await _upload(media)
    await media.link_threads([key], "t1", "alice")
    service = _thread_service(monkeypatch, media, [_img(f"graph://media/{key}")])
    media.store.get_metadata = AsyncMock(side_effect=RuntimeError("store down"))
    res = await service.delete_thread({"thread_id": "t1"}, _USER, "t1")
    assert res.success is True
    service.checkpointer.alist_messages = AsyncMock(side_effect=RuntimeError("db down"))
    res = await service.delete_thread({"thread_id": "t1"}, _USER, "t1")
    assert res.success is True
    res = await service.delete_message({"thread_id": "t1"}, _USER, "m1")
    assert res.success is True


@pytest.mark.asyncio
async def test_old_core_store_never_deletes_on_thread_delete(monkeypatch, delete_setting):
    media = _media(InMemoryMediaStore())
    key = await media.store.store(b"x", "image/png", metadata={"owner_id": "alice"})
    service = _thread_service(monkeypatch, media, [_img(f"graph://media/{key}")])
    await service.delete_thread({"thread_id": "t1"}, _USER, "t1")
    assert await media.store.exists(key)


# ---------------------------------------------------------------------------
# Q15: media.delete_with_thread setting and DELETE /v1/files/{file_id}
# ---------------------------------------------------------------------------


def test_setting_defaults_to_true_and_parses_booleans():
    assert MediaConfig.from_dict({}).delete_with_thread is True
    assert MediaConfig.from_dict({"delete_with_thread": False}).delete_with_thread is False
    with pytest.raises(ValueError, match="delete_with_thread"):
        MediaConfig.from_dict({"delete_with_thread": "maybe"})
    with pytest.raises(ValueError, match="object"):
        MediaConfig.from_dict([])  # type: ignore[arg-type]


def test_graph_config_exposes_the_media_section(tmp_path):
    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "a:b", "media": {"delete_with_thread": false}}')
    assert GraphConfig(str(path)).media.delete_with_thread is False
    path.write_text('{"agent": "a:b"}')
    assert GraphConfig(str(path)).media.delete_with_thread is True


def test_config_editor_lists_the_setting():
    from tenxgraph_api.cli.config_editor.schema import KNOWN_TOP_LEVEL_KEYS, build_schema
    from tenxgraph_api.cli.config_editor.validation import validate_config

    assert "media" in KNOWN_TOP_LEVEL_KEYS
    text = str(build_schema())
    assert "delete_with_thread" in text
    ok = validate_config({"agent": "g:a", "media": {"delete_with_thread": False}})
    assert [i["level"] for i in ok if i["path"] == "media"] == []
    bad = validate_config({"agent": "g:a", "media": {"delete_with_thread": "maybe"}})
    assert [i["level"] for i in bad if i["path"] == "media"] == ["error"]


def test_log_safe_escapes_control_characters():
    from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe

    assert log_safe("a\r\nb\x1b[31m\x00\tc") == "a\\r\\nb\\x1b[31m\\x00\tc"


def test_files_delete_scope_is_granted_to_wildcard_roles():
    assert "files:delete" in all_scopes()


def _request() -> MagicMock:
    request = MagicMock()
    request.state.request_id = "rid"
    request.state.timestamp = "2024-01-01T00:00:00Z"
    return request


@pytest.mark.asyncio
async def test_delete_file_owner_deletes_data_and_sidecar():
    media = _media()
    key = await _upload(media)
    response = await delete_file(_request(), key, media, {"user_id": "alice"})
    assert response.status_code == 200
    assert b'"deleted":true' in response.body.replace(b" ", b"")
    assert not await media.store.exists(key)


@pytest.mark.asyncio
async def test_delete_file_foreign_or_missing_is_404_not_403():
    media = _media()
    key = await _upload(media, owner="alice")
    for file_id, user in ((key, "mallory"), ("missing", "alice")):
        with pytest.raises(HTTPException) as exc:
            await delete_file(_request(), file_id, media, {"user_id": user})
        assert exc.value.status_code == 404
    assert await media.store.exists(key)


@pytest.mark.asyncio
async def test_delete_file_ownerless_file_is_404_when_owner_required():
    media = _media()
    key = await media.store.store(b"x", "image/png", metadata={})
    with pytest.raises(HTTPException) as exc:
        await delete_file(_request(), key, media, {"user_id": "alice"})
    assert exc.value.status_code == 404


def test_files_delete_route_is_guarded():
    from fastapi import FastAPI

    from tenxgraph_api.src.app.core.auth.route_guard import assert_all_routes_protected
    from tenxgraph_api.src.app.routers.media.router import router

    app = FastAPI()
    app.include_router(router)
    assert_all_routes_protected(app)
    assert any(
        r.path == "/v1/files/{file_id}" and "DELETE" in r.methods  # type: ignore[attr-defined]
        for r in router.routes
    )


# ---------------------------------------------------------------------------
# Q16: magic bytes
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 8


@pytest.mark.parametrize(
    ("mime", "data"),
    [
        ("image/png", _PNG),
        ("image/jpeg", b"\xff\xd8\xff\xe0" + b"0" * 8),
        ("image/gif", b"GIF89a" + b"0" * 8),
        ("image/webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "),
        ("image/bmp", b"BM" + b"0" * 8),
        ("image/tiff", b"II*\x00" + b"0" * 8),
        ("image/tiff", b"MM\x00*" + b"0" * 8),
        ("image/x-icon", b"\x00\x00\x01\x00" + b"0" * 8),
        ("application/pdf", b"%PDF-1.7\n"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04x"),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04x"),
        (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            b"PK\x03\x04",
        ),
        ("audio/mpeg", b"ID3\x03\x00"),
        ("audio/mpeg", b"\xff\xfb\x90\x00"),
        ("audio/wav", b"RIFF\x00\x00\x00\x00WAVEfmt "),
        ("audio/ogg", b"OggS\x00\x02"),
        ("audio/flac", b"fLaC\x00\x00"),
        ("video/mp4", b"\x00\x00\x00\x18ftypmp42"),
        ("video/webm", b"\x1a\x45\xdf\xa3\x01"),
        ("image/png; charset=binary", _PNG),
    ],
)
def test_matching_content_passes(mime, data):
    assert content_matches_mime(data, mime)


@pytest.mark.parametrize(
    ("mime", "data"),
    [
        ("image/png", b"<html><script>alert(1)</script>"),
        ("image/jpeg", _PNG),
        ("image/gif", b"GIF7a"),
        ("image/webp", b"RIFF\x00\x00\x00\x00WAVE"),
        ("application/pdf", b"MZ\x90\x00"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"%PDF-1.7"),
        ("audio/mpeg", b"\x00\x00\x00\x00"),
        ("audio/wav", b"RIFF\x00\x00\x00\x00WEBP"),
        ("video/mp4", b"abcdefghijkl"),
    ],
)
def test_mismatching_content_fails(mime, data):
    assert not content_matches_mime(data, mime)


@pytest.mark.parametrize(
    "mime", ["text/plain", "text/csv", "application/json", "application/x-foo"]
)
def test_types_without_a_signature_are_accepted(mime):
    assert content_matches_mime(b"anything at all", mime)


def _upload_file(name: str, mime: str | None, data: bytes) -> MagicMock:
    file = MagicMock(spec=UploadFile)
    file.filename = name
    file.content_type = mime
    file.size = len(data)
    file.read = AsyncMock(return_value=data)
    return file


@pytest.mark.asyncio
async def test_upload_route_rejects_a_mismatch_with_415():
    service = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await upload_file(
            _request(),
            _upload_file("evil.png", "image/png", b"<script>alert(1)</script>"),
            service,
            {"user_id": "alice"},
        )
    assert exc.value.status_code == 415
    assert exc.value.detail == "file content does not match image/png"
    service.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_upload_route_accepts_a_real_png_and_unknown_types():
    service = AsyncMock()
    service.upload_file.return_value = {
        "file_id": "f1",
        "mime_type": "image/png",
        "size_bytes": 16,
        "filename": "a.png",
        "extracted_text": None,
        "url": "/v1/files/f1",
        "direct_url": None,
        "direct_url_expires_at": None,
    }
    response = await upload_file(
        _request(), _upload_file("a.png", "image/png", _PNG), service, {"user_id": "alice"}
    )
    assert response.status_code == 200
    service.upload_file.reset_mock()
    await upload_file(
        _request(), _upload_file("n.txt", "text/plain", b"hello"), service, {"user_id": "alice"}
    )
    service.upload_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_route_escapes_the_filename_in_logs(caplog):
    service = AsyncMock()
    with caplog.at_level("INFO"), pytest.raises(HTTPException):
        await upload_file(
            _request(),
            _upload_file("\x1b[31mred.png", "image/png", b"nope"),
            service,
            {"user_id": "alice"},
        )
    assert caplog.records
    assert all("\x1b" not in record.getMessage() for record in caplog.records)
