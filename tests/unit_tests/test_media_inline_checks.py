"""Inline media offloaded from a run gets the upload checks: signature, size and quota (AA-8)."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from tenxgraph.core.authz import Identity
from tenxgraph.core.state import Message
from tenxgraph.core.state.message_block import ImageBlock, MediaRef
from tenxgraph.storage.media.storage.memory_store import InMemoryMediaStore

from tenxgraph_api.src.app.core.config.media_settings import MediaSettings
from tenxgraph_api.src.app.core.exceptions.client_errors import client_error
from tenxgraph_api.src.app.core.validation.request_config import ClientInputError
from tenxgraph_api.src.app.routers.graph.router import invoke_graph, stream_graph
from tenxgraph_api.src.app.routers.graph.services.graph_service import (
    GraphService,
    _stream_error_data,
)
from tenxgraph_api.src.app.routers.media import MediaService, _BoundedCache


USER = Identity(user_id="u1")
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


class ListStore(InMemoryMediaStore):
    async def update_metadata(self, storage_key: str, updates: dict[str, Any]) -> bool:
        data, mime, meta = self._data[storage_key]
        self._data[storage_key] = (data, mime, {**meta, **updates})
        return True


def _media(**settings: Any) -> MediaService:
    svc = MediaService.__new__(MediaService)
    settings = {"MEDIA_INLINE_MAX_BYTES": 10, **settings}
    svc._settings = MediaSettings(_env_file=None, MEDIA_REQUIRE_OWNER=True, **settings)
    svc._checkpointer = MagicMock(
        aget_cache_value=AsyncMock(return_value=None),
        aput_cache_value=AsyncMock(),
        aclear_cache_value=AsyncMock(),
        alist_cache_keys=AsyncMock(return_value=[]),
    )
    svc._store = ListStore()
    svc._extraction_cache = _BoundedCache(8)
    svc._signed_url_cache = _BoundedCache(8)
    svc.extractor = None
    return svc


def _image(data: bytes, mime: str = "image/png") -> Message:
    media = MediaRef(kind="data", data_base64=base64.b64encode(data).decode(), mime_type=mime)
    return Message(role="user", content=[ImageBlock(media=media)])


@pytest.mark.asyncio
async def test_inline_images_are_refused_past_the_file_quota():
    svc = _media(MEDIA_MAX_FILES_PER_USER=1)
    await svc.upload_file(PNG, "a.png", "image/png", user_id="u1")
    for _ in range(3):
        with pytest.raises(ClientInputError, match="at most 1 files per user"):
            await svc.offload_inline([_image(PNG)], "u1")
    assert len(await svc._keys_of("u1")) == 1


@pytest.mark.asyncio
async def test_several_inline_images_count_together_and_none_is_stored():
    svc = _media(MEDIA_MAX_FILES_PER_USER=2)
    await svc.upload_file(PNG, "a.png", "image/png", user_id="u1")
    with pytest.raises(ClientInputError, match="at most 2 files per user"):
        await svc.offload_inline([_image(PNG), _image(PNG)], "u1")
    assert len(await svc._keys_of("u1")) == 1


@pytest.mark.asyncio
async def test_fake_png_bytes_are_refused():
    svc = _media()
    message = _image(b"<html><script>alert(1)</script>" * 8)
    with pytest.raises(ClientInputError, match="content does not match image/png"):
        await svc.offload_inline([message], "u1")
    assert message.content[0].media.kind == "data"
    assert await svc._keys_of("u1") == []


@pytest.mark.asyncio
async def test_inline_media_over_the_size_cap_is_refused():
    svc = _media(MEDIA_MAX_SIZE_MB=0.0001)  # about 104 bytes
    with pytest.raises(ClientInputError, match="exceeds maximum"):
        await svc.offload_inline([_image(PNG)], "u1")


@pytest.mark.asyncio
async def test_normal_inline_images_are_still_stored():
    svc = _media(MEDIA_MAX_FILES_PER_USER=3)
    message = _image(PNG)
    await svc.offload_inline([message, _image(PNG)], "u1")
    media = message.content[0].media
    assert media.kind == "url"
    assert media.url.startswith("graph://media/")
    keys = await svc._keys_of("u1")
    assert len(keys) == 2
    meta = await svc.store.get_metadata(keys[0])
    assert meta["owner_id"] == "u1"


@pytest.mark.asyncio
async def test_small_inline_media_is_left_inline_and_not_checked():
    svc = _media(MEDIA_INLINE_MAX_BYTES=10_000, MEDIA_MAX_FILES_PER_USER=1)
    await svc.upload_file(PNG, "a.png", "image/png", user_id="u1")
    message = _image(b"not a png at all")
    await svc.offload_inline([message], "u1")
    assert message.content[0].media.kind == "data"


@pytest.mark.asyncio
async def test_graph_service_check_runs_the_media_check():
    svc = _media()
    graph = GraphService.__new__(GraphService)
    graph._media_service = svc
    with pytest.raises(HTTPException) as exc:
        await graph.check_inline_media([_image(b"x" * 64)], None)
    # The answer the run gives for the same ClientInputError (client_errors.as_http_error).
    assert exc.value.status_code == 422
    assert "content does not match image/png" in exc.value.detail
    graph._media_service = SimpleNamespace()  # a stand-in without the check: nothing to do
    await graph.check_inline_media([_image(b"x" * 64)], None)


def _route_service(media: MediaService) -> MagicMock:
    """A route-level stand-in whose inline check is the real GraphService + MediaService one."""
    graph = GraphService.__new__(GraphService)
    graph._media_service = media
    service = MagicMock()
    service.require_run_thread = AsyncMock()
    service.check_inline_media = graph.check_inline_media
    service.stream_graph = MagicMock()
    service.invoke_graph = AsyncMock()
    return service


async def _over_quota() -> MediaService:
    media = _media(MEDIA_MAX_FILES_PER_USER=1)
    await media.upload_file(PNG, "a.png", "image/png", user_id="u1")
    return media


@pytest.mark.asyncio
async def test_stream_route_refuses_over_quota_before_the_response_starts():
    service = _route_service(await _over_quota())
    graph_input = SimpleNamespace(messages=[_image(PNG)], config={})
    with pytest.raises(HTTPException) as exc:
        await stream_graph(graph_input, service, USER, None)
    assert exc.value.status_code == 422
    assert "at most 1 files per user" in exc.value.detail
    service.stream_graph.assert_not_called()


@pytest.mark.asyncio
async def test_invoke_route_refuses_a_fake_png_before_the_run():
    service = _route_service(_media())
    graph_input = SimpleNamespace(messages=[_image(b"MZ" + b"\x00" * 64)], config={})
    with pytest.raises(HTTPException) as exc:
        await invoke_graph(MagicMock(), graph_input, service, USER)
    assert exc.value.status_code == 422
    assert "content does not match image/png" in exc.value.detail
    service.invoke_graph.assert_not_called()


def test_in_run_refusal_gets_the_same_answer_on_every_transport():
    # offload_inline raises ClientInputError inside the run: WS / stream chunks and AG-UI
    # events take client_error's code and message, the same as the HTTP 422 above.
    exc = ClientInputError("Upload limit reached: at most 1 files per user")
    answer = client_error(exc)
    assert (answer.status_code, answer.message) == (422, str(exc))
    assert _stream_error_data(exc) == {"reason": str(exc), "code": answer.error_code}
