"""G-123 thread links lose no update; G-125 listing and quota work through an owner index."""

# ruff: noqa: S101, PLR2004, SLF001

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from tenxgraph.storage.media.storage.base import BaseMediaStore
from tenxgraph.storage.media.storage.memory_store import InMemoryMediaStore

from tenxgraph_api.src.app.core.auth.request_config import ClientInputError
from tenxgraph_api.src.app.core.config.media_settings import MediaSettings
from tenxgraph_api.src.app.routers.media import MediaService, _BoundedCache


class _SlowStore(InMemoryMediaStore):
    """Metadata reads go stale while other tasks run, as they do over a network."""

    async def get_metadata(self, storage_key):
        meta = await super().get_metadata(storage_key)
        await asyncio.sleep(0)
        return meta


class _OldCoreStore(_SlowStore):
    """A store from a core without modify_metadata."""

    modify_metadata = None


class _IndexedStore(BaseMediaStore):
    """Like the cloud store: no list_keys, an owner index instead."""

    def __init__(self) -> None:
        self.inner = InMemoryMediaStore()

    async def store(self, data, mime_type, metadata=None):
        return await self.inner.store(data, mime_type, metadata)

    async def retrieve(self, storage_key):
        return await self.inner.retrieve(storage_key)

    async def delete(self, storage_key):
        return await self.inner.delete(storage_key)

    async def exists(self, storage_key):
        return await self.inner.exists(storage_key)

    async def get_metadata(self, storage_key):
        return await self.inner.get_metadata(storage_key)

    async def update_metadata(self, storage_key, updates):
        return await self.inner.update_metadata(storage_key, updates)

    async def list_keys_for_owner(self, owner_id):
        return await self.inner.list_keys_for_owner(owner_id)


def _media(store: Any, **settings: Any) -> MediaService:
    svc = MediaService.__new__(MediaService)
    svc._settings = MediaSettings(_env_file=None, MEDIA_REQUIRE_OWNER=True, **settings)
    svc._checkpointer = MagicMock(
        aget_cache_value=AsyncMock(return_value=None), aput_cache_value=AsyncMock()
    )
    svc._store = store
    svc._extraction_cache = _BoundedCache(8)
    svc._signed_url_cache = _BoundedCache(8)
    svc._links_notice_logged = False
    svc._pipeline = SimpleNamespace(handling="skip")
    return svc


async def _upload(media: MediaService, owner: str = "alice") -> str:
    result = await media.upload_file(b"\x89PNG\r\n\x1a\n", "a.png", "image/png", user_id=owner)
    return result["file_id"]


@pytest.mark.asyncio
async def test_concurrent_links_to_one_file_are_all_kept():
    media = _media(_SlowStore())
    key = await _upload(media)
    await asyncio.gather(*(media.link_threads([key], f"t{i}", "alice") for i in range(15)))
    assert sorted((await media.store.get_metadata(key))["threads"]) == sorted(
        f"t{i}" for i in range(15)
    )


@pytest.mark.asyncio
async def test_concurrent_unlinks_keep_the_rest_and_delete_at_the_end(monkeypatch):
    media = _media(_SlowStore())
    monkeypatch.setattr(media, "delete_with_thread", lambda: True)
    key = await _upload(media)
    for i in range(6):
        await media.link_threads([key], f"t{i}", "alice")
    await asyncio.gather(*(media.unlink_threads([key], f"t{i}") for i in range(5)))
    assert (await media.store.get_metadata(key))["threads"] == ["t5"]
    await media.unlink_threads([key], "t5")
    assert not await media.store.exists(key)


@pytest.mark.asyncio
async def test_store_without_modify_metadata_still_links():
    media = _media(_OldCoreStore())
    key = await _upload(media)
    await media.link_threads([key], "t1", "alice")
    assert (await media.store.get_metadata(key))["threads"] == ["t1"]


@pytest.mark.asyncio
async def test_without_an_atomic_store_concurrent_links_do_lose_updates():
    # Documents the failure the store-side modify_metadata prevents
    media = _media(_OldCoreStore())
    key = await _upload(media)
    await asyncio.gather(*(media.link_threads([key], f"t{i}", "alice") for i in range(5)))
    assert len((await media.store.get_metadata(key))["threads"]) < 5


@pytest.mark.asyncio
async def test_list_files_works_through_the_owner_index():
    media = _media(_IndexedStore())
    a = await _upload(media, "alice")
    await _upload(media, "bob")
    items, more = await media.list_files("alice", 0, 10)
    assert [i["file_id"] for i in items] == [a]
    assert more is False
    # With no user there is no owner to index by: still not listable
    with pytest.raises(NotImplementedError):
        await media.list_files(None, 0, 10)


@pytest.mark.asyncio
async def test_quota_is_enforced_through_the_owner_index():
    media = _media(_IndexedStore(), MEDIA_MAX_FILES_PER_USER=1)
    await _upload(media, "alice")
    with pytest.raises(ClientInputError):
        await _upload(media, "alice")
    await _upload(media, "bob")  # another user is unaffected


@pytest.mark.asyncio
async def test_release_thread_finds_linked_files_through_the_owner_index(monkeypatch):
    media = _media(_IndexedStore())
    monkeypatch.setattr(media, "delete_with_thread", lambda: False)
    key = await _upload(media)
    await media.link_threads([key], "t1", "alice")
    assert await media.release_thread("t1", "alice") == []
    assert (await media.store.get_metadata(key))["threads"] == []
