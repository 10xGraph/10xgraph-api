"""Sprint 4 — API layer multimodal tests.

Tests the file upload/retrieval endpoints, MediaService, multimodal
preprocessing, and config endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# MediaService unit tests
# ---------------------------------------------------------------------------
from tenxgraph.storage.checkpointer import InMemoryCheckpointer

from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType


def _make_settings(**overrides) -> MediaSettings:
    defaults = {
        "MEDIA_STORAGE_TYPE": MediaStorageType.MEMORY,
        "MEDIA_STORAGE_PATH": "./test_uploads",
        "MEDIA_MAX_SIZE_MB": 1.0,
        "DOCUMENT_HANDLING": "extract_text",
    }
    defaults.update(overrides)
    return MediaSettings(**defaults)


class TestMediaService:
    """Unit tests for MediaService."""

    def _make_service(self, **kwargs):
        from tenxgraph_api.src.app.routers.media import MediaService

        return MediaService(
            settings=_make_settings(**kwargs),
            checkpointer=InMemoryCheckpointer(),
        )

    @pytest.mark.asyncio
    async def test_upload_file_stores_and_returns_metadata(self):
        svc = self._make_service()
        result = await svc.upload_file(b"hello world", "test.txt", "text/plain")
        assert result["filename"] == "test.txt"
        assert result["mime_type"] == "text/plain"
        assert result["size_bytes"] == 11
        assert result["file_id"]
        assert result["url"].startswith("/v1/files/")

    @pytest.mark.asyncio
    async def test_upload_file_exceeds_max_size(self):
        svc = self._make_service(MEDIA_MAX_SIZE_MB=0.0001)  # ~100 bytes
        with pytest.raises(ValueError, match="exceeds maximum"):
            await svc.upload_file(b"x" * 200, "big.bin", "application/octet-stream")

    @pytest.mark.asyncio
    async def test_upload_file_extracts_text_for_documents(self):
        svc = self._make_service()
        with patch.object(
            svc.extractor,
            "extract",
            new_callable=AsyncMock,
            return_value="Extracted text!",
        ):
            result = await svc.upload_file(b"%PDF-1.4 ...", "doc.pdf", "application/pdf")
            assert result["extracted_text"] == "Extracted text!"

    @pytest.mark.asyncio
    async def test_upload_no_extraction_for_images(self):
        svc = self._make_service()
        result = await svc.upload_file(b"\xff\xd8\xff\xe0", "img.jpg", "image/jpeg")
        assert result["extracted_text"] is None

    @pytest.mark.asyncio
    async def test_get_file_round_trip(self):
        svc = self._make_service()
        result = await svc.upload_file(b"binary data", "test.bin", "application/octet-stream")
        data, mime = await svc.get_file(result["file_id"])
        assert data == b"binary data"
        assert mime == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_get_file_info(self):
        svc = self._make_service()
        result = await svc.upload_file(b"abc", "small.txt", "text/plain")
        info = await svc.get_file_info(result["file_id"])
        assert info["mime_type"] == "text/plain"
        assert info["size_bytes"] == 3
        assert info["filename"] == "small.txt"

    @pytest.mark.asyncio
    async def test_get_file_not_found(self):
        svc = self._make_service()
        with pytest.raises(KeyError):
            await svc.get_file("nonexistent-key")

    @pytest.mark.asyncio
    async def test_get_file_info_not_found(self):
        svc = self._make_service()
        with pytest.raises(KeyError):
            await svc.get_file_info("nonexistent-key")

    @pytest.mark.asyncio
    async def test_cached_extraction_lookup(self):
        svc = self._make_service()
        with patch.object(svc.extractor, "extract", new_callable=AsyncMock, return_value="Cached!"):
            result = await svc.upload_file(b"%PDF-1.4 data", "report.pdf", "application/pdf")
        assert svc.get_cached_extraction(result["file_id"]) == "Cached!"
        assert await svc.aget_cached_extraction(result["file_id"]) == "Cached!"
        assert svc.get_cached_extraction("missing") is None

    @pytest.mark.asyncio
    async def test_get_file_info_uses_store_metadata_without_blob_download(self):
        svc = self._make_service()
        mock_store = MagicMock()
        mock_store.get_metadata = AsyncMock(
            return_value={
                "mime_type": "image/png",
                "size_bytes": 123,
                "filename": "img.png",
            }
        )
        mock_store.get_direct_url = AsyncMock(return_value=None)
        mock_store.retrieve = AsyncMock(side_effect=AssertionError("retrieve should not be called"))
        svc._store = mock_store

        info = await svc.get_file_info("file-123")

        assert info["mime_type"] == "image/png"
        assert info["size_bytes"] == 123
        assert info["filename"] == "img.png"
        mock_store.retrieve.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_direct_url_info_uses_cache(self):
        svc = self._make_service()
        mock_store = MagicMock()
        mock_store.get_direct_url = AsyncMock(return_value="https://signed.example.com/file")
        svc._store = mock_store

        first = await svc.get_direct_url_info("file-123", mime_type="image/png")
        second = await svc.get_direct_url_info("file-123", mime_type="image/png")

        assert first == second
        assert first["url"] == "https://signed.example.com/file"
        mock_store.get_direct_url.assert_awaited_once_with(
            "file-123",
            mime_type="image/png",
            expiration=3600,
        )


# ---------------------------------------------------------------------------
# Multimodal preprocessor tests
# ---------------------------------------------------------------------------


class TestMultimodalPreprocessor:
    """Tests for preprocess_multimodal_messages."""

    @pytest.mark.asyncio
    async def test_noop_when_no_media_service(self):
        from tenxgraph.core.state import Message

        from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
            preprocess_multimodal_messages,
        )

        msgs = [Message.text_message("hello")]
        result = await preprocess_multimodal_messages(msgs, None)
        assert result is msgs  # same identity, no copy

    @pytest.mark.asyncio
    async def test_document_file_id_resolved_to_text(self):
        from tenxgraph.core.state import Message
        from tenxgraph.core.state.message_block import DocumentBlock, MediaRef, TextBlock

        from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
            preprocess_multimodal_messages,
        )

        doc_block = DocumentBlock(
            media=MediaRef(kind="file_id", file_id="file-abc", mime_type="application/pdf"),
        )
        msg = Message(role="user", content=[TextBlock(text="Read this"), doc_block])

        mock_svc = MagicMock()
        mock_svc.get_cached_extraction.return_value = "The extracted PDF text"

        result = await preprocess_multimodal_messages([msg], mock_svc)
        # doc_block should be replaced with a TextBlock
        assert len(result[0].content) == 2
        assert result[0].content[0].type == "text"
        assert result[0].content[0].text == "Read this"
        assert result[0].content[1].type == "text"
        assert result[0].content[1].text == "The extracted PDF text"

    @pytest.mark.asyncio
    async def test_image_file_id_to_media_url(self):
        from tenxgraph.core.state import Message
        from tenxgraph.core.state.message_block import ImageBlock, MediaRef

        from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
            preprocess_multimodal_messages,
        )

        img_block = ImageBlock(
            media=MediaRef(kind="file_id", file_id="file-img-123", mime_type="image/png"),
        )
        msg = Message(role="user", content=[img_block])

        mock_svc = MagicMock()
        mock_svc.get_cached_extraction.return_value = None

        result = await preprocess_multimodal_messages([msg], mock_svc)
        media = result[0].content[0].media
        assert media.kind == "url"
        assert media.url == "graph://media/file-img-123"

    @pytest.mark.asyncio
    async def test_text_only_message_unchanged(self):
        from tenxgraph.core.state import Message
        from tenxgraph.core.state.message_block import TextBlock

        from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
            preprocess_multimodal_messages,
        )

        msg = Message(role="user", content=[TextBlock(text="hello")])
        mock_svc = MagicMock()

        result = await preprocess_multimodal_messages([msg], mock_svc)
        assert result[0].content[0].text == "hello"

    @pytest.mark.asyncio
    async def test_document_file_id_without_cached_text(self):
        from tenxgraph.core.state import Message
        from tenxgraph.core.state.message_block import DocumentBlock, MediaRef

        from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
            preprocess_multimodal_messages,
        )

        doc = DocumentBlock(
            media=MediaRef(kind="file_id", file_id="file-no-cache", mime_type="application/pdf"),
        )
        msg = Message(role="user", content=[doc])

        mock_svc = MagicMock()
        mock_svc.get_cached_extraction.return_value = None

        result = await preprocess_multimodal_messages([msg], mock_svc)
        # Should convert file_id → graph://media/ URL reference
        media = result[0].content[0].media
        assert media.kind == "url"
        assert media.url == "graph://media/file-no-cache"


# ---------------------------------------------------------------------------
# MediaSettings tests
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Media store factory tests
# ---------------------------------------------------------------------------


class TestMediaStoreFactory:
    def test_memory_store(self):
        from tenxgraph.storage.media.storage.memory_store import InMemoryMediaStore

        from tenxgraph_api.src.app.routers.media import _create_media_store

        s = _make_settings(MEDIA_STORAGE_TYPE=MediaStorageType.MEMORY)
        store = _create_media_store(s)
        assert isinstance(store, InMemoryMediaStore)

    def test_local_store(self):
        from tenxgraph.storage.media.storage.local_store import LocalFileMediaStore

        from tenxgraph_api.src.app.routers.media import _create_media_store

        s = _make_settings(MEDIA_STORAGE_TYPE=MediaStorageType.LOCAL)
        store = _create_media_store(s)
        assert isinstance(store, LocalFileMediaStore)

    def test_unknown_type_raises(self):
        from tenxgraph_api.src.app.routers.media import _create_media_store

        s = _make_settings()
        s.MEDIA_STORAGE_TYPE = "bogus"
        with pytest.raises(ValueError, match="Unknown"):
            _create_media_store(s)


# ---------------------------------------------------------------------------
# Schemas tests
# ---------------------------------------------------------------------------


