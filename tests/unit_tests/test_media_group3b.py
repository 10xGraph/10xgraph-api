"""Group 3b API rows: G-124, G-126, G-130, G-38, G-89 wiring and J-5."""

# ruff: noqa: S101, PLR2004, SLF001

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tenxgraph.core.state import Message
from tenxgraph.core.state.message_block import ImageBlock, MediaRef
from tenxgraph.storage.media.storage.base import MediaTooLargeError
from tenxgraph.storage.media.storage.memory_store import InMemoryMediaStore

from tenxgraph_api.src.app import main as main_module
from tenxgraph_api.src.app.core.auth.request_config import ClientInputError
from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    internal_media_keys,
    preprocess_multimodal_messages,
)
from tenxgraph_api.src.app.routers.media import MediaService, _BoundedCache, _create_media_store
from tenxgraph_api.src.app.routers.media.router import upload_file
from tenxgraph_api.src.app.routers.store.schemas.store_schemas import ListMemoriesSchema
from tenxgraph_api.src.app.routers.store.services.store_service import StoreService
from tenxgraph_api.src.app.utils.media.extractor import DocumentExtractor


PNG = b"\x89PNG\r\n\x1a\n"


class ListStore(InMemoryMediaStore):
    async def update_metadata(self, storage_key: str, updates: dict[str, Any]) -> bool:
        data, mime, meta = self._data[storage_key]
        self._data[storage_key] = (data, mime, {**meta, **updates})
        return True


def _media(store: Any = None, **settings: Any) -> MediaService:
    svc = MediaService.__new__(MediaService)
    svc._settings = MediaSettings(_env_file=None, MEDIA_REQUIRE_OWNER=True, **settings)
    svc._checkpointer = MagicMock(
        aget_cache_value=AsyncMock(return_value=None),
        aput_cache_value=AsyncMock(),
        aclear_cache_value=AsyncMock(),
        alist_cache_keys=AsyncMock(return_value=[]),
    )
    svc._store = store if store is not None else ListStore()
    svc._extraction_cache = _BoundedCache(8)
    svc._signed_url_cache = _BoundedCache(8)
    svc._links_notice_logged = False
    svc._pipeline = SimpleNamespace(handling="skip")
    return svc


# --- G-124 -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_clears_shared_extraction_and_signed_url_cache():
    svc = _media()
    svc._checkpointer.alist_cache_keys.return_value = ["f:image/png:60", "f:image/png:3600"]
    key = await svc.store.store(PNG, "image/png", metadata={"owner_id": "a"})
    svc._checkpointer.alist_cache_keys.return_value = [f"{key}:image/png:60"]
    svc._signed_url_cache[f"{key}:image/png:3600"] = {"url": "u"}
    svc._extraction_cache[key] = "secret text"

    assert await svc.delete_file(key) is True

    cleared = {c.args for c in svc._checkpointer.aclear_cache_value.await_args_list}
    assert ("media:extraction", key) in cleared
    assert ("media:signed-url", f"{key}:image/png:60") in cleared
    assert ("media:signed-url", f"{key}:image/png:3600") in cleared
    assert key not in svc._extraction_cache
    assert not list(svc._signed_url_cache)


# --- G-126 -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_memories_pages_with_offset_and_has_more():
    rows = [f"m{i}" for i in range(7)]

    async def aget_all(cfg, limit, **kw):
        return rows[:limit]

    store = MagicMock(aget_all=aget_all)
    svc = StoreService(store=store)
    svc._config = lambda config, user: {}
    with patch(
        "tenxgraph_api.src.app.routers.store.services.store_service.MemoryListResponseSchema",
        lambda **kw: SimpleNamespace(**kw),
    ):
        first = await svc.list_memories({}, {}, limit=3, offset=0)
        second = await svc.list_memories({}, {}, limit=3, offset=3)
        last = await svc.list_memories({}, {}, limit=3, offset=6)
    assert (first.memories, first.has_more, first.next_offset) == (rows[0:3], True, 3)
    assert (second.memories, second.has_more, second.next_offset) == (rows[3:6], True, 6)
    assert (last.memories, last.has_more, last.next_offset) == (rows[6:], False, None)


def test_list_memories_schema_offset_defaults_to_zero_and_rejects_negative():
    assert ListMemoriesSchema().offset == 0
    assert ListMemoriesSchema(offset=5).offset == 5
    with pytest.raises(ValueError):
        ListMemoriesSchema(offset=-1)


# --- G-130 -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_count_quota_blocks_the_next_upload_for_that_user_only():
    svc = _media(MEDIA_MAX_FILES_PER_USER=2)
    await svc.upload_file(PNG, "a.png", "image/png", user_id="alice")
    await svc.upload_file(PNG, "b.png", "image/png", user_id="alice")
    with pytest.raises(ClientInputError, match="at most 2 files"):
        await svc.upload_file(PNG, "c.png", "image/png", user_id="alice")
    await svc.upload_file(PNG, "c.png", "image/png", user_id="bob")


@pytest.mark.asyncio
async def test_storage_quota_counts_bytes():
    svc = _media(MEDIA_MAX_USER_STORAGE_MB=0.001)  # about 1048 bytes
    await svc.upload_file(b"\x89PNG\r\n\x1a\n" + b"x" * 600, "a.png", "image/png", user_id="a")
    with pytest.raises(ClientInputError, match="Storage limit"):
        await svc.upload_file(b"\x89PNG\r\n\x1a\n" + b"x" * 600, "b.png", "image/png", user_id="a")


@pytest.mark.asyncio
async def test_quota_off_by_default():
    svc = _media()
    for i in range(5):
        await svc.upload_file(PNG, f"{i}.png", "image/png", user_id="alice")


@pytest.mark.asyncio
async def test_cleanup_uses_the_media_service_store():
    """The temp-cache cleanup used to get media_store=None because nothing bound that name."""
    store = object()
    cache = AsyncMock()
    cache.cleanup.return_value = 0
    container = MagicMock()
    container.try_get.side_effect = lambda key: (
        SimpleNamespace(store=store)
        if key is MediaService
        else (object() if key in ("checkpointer",) else None)
    )
    with (
        patch.object(main_module, "container", container),
        patch("tenxgraph.storage.media.temp_cache.TemporaryMediaCache", return_value=cache),
    ):
        await main_module._cleanup_temp_media_cache()
    assert cache.cleanup.await_args.args[1] is store


@pytest.mark.asyncio
async def test_sweeper_calls_sweep_orphans_and_survives_errors():
    calls: list[float] = []

    class Store:
        async def sweep_orphans(self, min_age: float) -> int:
            calls.append(min_age)
            if len(calls) == 1:
                raise OSError("boom")
            return 1

    container = MagicMock()
    container.try_get.return_value = SimpleNamespace(store=Store())
    with patch.object(main_module, "container", container):
        task = asyncio.create_task(main_module._sweep_media_loop(0.01))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 2
    assert calls[0] == main_module.MEDIA_SWEEP_MIN_AGE_SECONDS


@pytest.mark.asyncio
async def test_sweeper_returns_for_a_store_without_sweep_orphans():
    container = MagicMock()
    container.try_get.return_value = SimpleNamespace(store=object())
    with patch.object(main_module, "container", container):
        await asyncio.wait_for(main_module._sweep_media_loop(0.01), 1)


# --- G-38 --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_pixel_limit_is_enforced_on_upload():
    svc = _media(MEDIA_MAX_IMAGE_PIXELS=123)
    seen: list[Any] = []

    def fake(data: bytes, max_pixels: Any) -> None:
        seen.append(max_pixels)
        raise ValueError("Image has 999 pixels; the limit is 123 pixels")

    with patch("tenxgraph_api.src.app.routers.media.check_image_pixels", fake):
        with pytest.raises(ClientInputError, match="pixels"):
            await svc.upload_file(PNG, "a.png", "image/png", user_id="a")
    assert seen == [123]
    assert not await svc.store.list_keys()


@pytest.mark.asyncio
async def test_pixel_check_skips_non_images():
    svc = _media(MEDIA_MAX_IMAGE_PIXELS=1)
    with patch("tenxgraph_api.src.app.routers.media.check_image_pixels") as check:
        await svc.upload_file(b"hello", "a.txt", "text/plain", user_id="a")
    check.assert_not_called()


@pytest.mark.parametrize(
    "stype", [MediaStorageType.MEMORY, MediaStorageType.LOCAL], ids=["memory", "local"]
)
@pytest.mark.asyncio
async def test_media_store_gets_the_size_cap(stype, tmp_path):
    settings = MediaSettings(
        _env_file=None,
        MEDIA_STORAGE_TYPE=stype,
        MEDIA_STORAGE_PATH=str(tmp_path),
        MEDIA_MAX_SIZE_MB=0.001,
    )
    store = _create_media_store(settings)
    with pytest.raises(MediaTooLargeError):
        await store.store(b"x" * 2000, "application/octet-stream")


# --- J-5 ---------------------------------------------------------------------------------


def _pdf(pages: int, pad: int = 0) -> bytes:
    body = b"".join(b"%d 0 obj << /Type /Page >> endobj\n" % i for i in range(pages))
    return b"%PDF-1.4\n" + body + b"/Type /Pages /Count %d\n" % pages + b"x" * pad


def _extracting_media(**settings: Any) -> MediaService:
    svc = _media(**settings)
    svc._pipeline = SimpleNamespace(
        handling="extract_text",
        extractor=DocumentExtractor(
            extractor=SimpleNamespace(extract=AsyncMock(return_value="text")),
            max_pdf_bytes=int(svc._settings.MEDIA_MAX_PDF_MB * 1024 * 1024),
            max_pdf_pages=svc._settings.MEDIA_MAX_PDF_PAGES,
        ),
    )
    from tenxgraph.storage.media.config import DocumentHandling

    svc._pipeline.handling = DocumentHandling.EXTRACT_TEXT
    return svc


@pytest.mark.asyncio
async def test_oversize_pdf_is_refused_before_it_is_stored():
    svc = _extracting_media(MEDIA_MAX_PDF_MB=0.001)
    with pytest.raises(ClientInputError, match="PDF is"):
        await svc.upload_file(_pdf(1, pad=5000), "a.pdf", "application/pdf", user_id="a")
    assert not await svc.store.list_keys()


@pytest.mark.asyncio
async def test_pdf_with_too_many_pages_is_refused():
    svc = _extracting_media(MEDIA_MAX_PDF_PAGES=10)
    with pytest.raises(ClientInputError, match="pages"):
        await svc.upload_file(_pdf(50), "a.pdf", "application/pdf", user_id="a")
    # page count hidden in a Pages node alone is caught too
    big_tree = b"%PDF-1.4\n/Type /Pages /Count 100000\n"
    with pytest.raises(ClientInputError, match="pages"):
        await svc.upload_file(big_tree, "b.pdf", "application/pdf", user_id="a")


@pytest.mark.asyncio
async def test_small_pdf_is_still_extracted():
    svc = _extracting_media()
    result = await svc.upload_file(_pdf(3), "a.pdf", "application/pdf", user_id="a")
    assert result["extracted_text"] == "text"


@pytest.mark.asyncio
async def test_extractor_itself_refuses_oversize_pdf():
    from tenxgraph_api.src.app.utils.media.extractor import DocumentTooLargeError

    fake = SimpleNamespace(extract=AsyncMock(return_value="x"))
    extractor = DocumentExtractor(extractor=fake, max_pdf_pages=5)
    with pytest.raises(DocumentTooLargeError):
        await extractor.extract(_pdf(20), "a.pdf", "application/pdf")
    fake.extract.assert_not_called()


@pytest.mark.asyncio
async def test_pdf_cap_is_a_413_on_the_upload_route():
    from fastapi import HTTPException

    service = AsyncMock()
    service.upload_file.side_effect = ClientInputError("PDF is 99 bytes; the limit is 1 bytes")
    file = MagicMock()
    file.filename = "a.pdf"
    file.content_type = "application/pdf"
    file.size = 12
    file.read = AsyncMock(return_value=b"%PDF-1.4\nabcd")
    with pytest.raises(HTTPException) as exc:
        await upload_file(MagicMock(), file, service, {"user_id": "a"})
    assert exc.value.status_code == 413


# --- G-89 wiring -------------------------------------------------------------------------


def _inline_message(size: int) -> Message:
    data = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * size).decode()
    return Message(
        message_id="m1",
        role="user",
        content=[ImageBlock(media=MediaRef(kind="data", data_base64=data, mime_type="image/png"))],
    )


@pytest.mark.asyncio
async def test_large_inline_media_is_offloaded_with_owner_and_thread_tracking():
    svc = _media(MEDIA_INLINE_MAX_BYTES=100)
    out = await preprocess_multimodal_messages([_inline_message(500)], _as_service(svc), "alice")
    keys = internal_media_keys(out[0])
    assert len(keys) == 1
    assert out[0].content[0].media.kind == "url"
    meta = await svc.store.get_metadata(keys[0])
    assert meta["owner_id"] == "alice"
    assert meta["threads"] == []
    await svc.ensure_can_access(keys[0], "alice")


@pytest.mark.asyncio
async def test_small_inline_media_stays_inline_and_zero_turns_it_off():
    svc = _media(MEDIA_INLINE_MAX_BYTES=10_000)
    out = await preprocess_multimodal_messages([_inline_message(50)], _as_service(svc), "a")
    assert out[0].content[0].media.kind == "data"
    off = _media(MEDIA_INLINE_MAX_BYTES=0)
    out = await preprocess_multimodal_messages([_inline_message(500)], _as_service(off), "a")
    assert out[0].content[0].media.kind == "data"


@pytest.mark.asyncio
async def test_offload_rejects_bad_base64_as_client_error():
    svc = _media(MEDIA_INLINE_MAX_BYTES=1)
    msg = Message(
        message_id="m1",
        role="user",
        content=[
            ImageBlock(
                media=MediaRef(kind="data", data_base64="!!!notbase64!!!", mime_type="image/png")
            )
        ],
    )
    with pytest.raises(ClientInputError):
        await svc.offload_inline([msg], "a")


def _as_service(svc: MediaService) -> MediaService:
    # preprocess reads extraction via these; the stub checkpointer returns nothing
    return svc
