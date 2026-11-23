"""Tests for Sprint 2 - DocumentExtractor.

These tests live in 10xgraph-api because document extraction is an API concern
(not a core library concern).
"""

import pytest

from tenxgraph_api.src.app.utils.media.extractor import (
    DocumentExtractor,
    ExtractionError,
    FileTypeNotSupportedError,
)


# ---------------------------------------------------------------------------
# Fake textxtract extractor for testing
# ---------------------------------------------------------------------------


class FakeExtractor:
    """Simulates textxtract.AsyncTextExtractor behavior."""

    async def extract(self, data, filename=None):
        if filename == "unsupported.xyz":
            raise FileTypeNotSupportedError("unsupported file type")
        if data == b"corrupt":
            raise ExtractionError("extraction failed")
        if isinstance(data, bytes):
            return f"extracted:{len(data)}bytes"
        return f"extracted:path={data}"


# ---------------------------------------------------------------------------
# DocumentExtractor tests
# ---------------------------------------------------------------------------


class TestDocumentExtractor:
    @pytest.mark.asyncio
    async def test_extract_bytes_success(self):
        ext = DocumentExtractor(extractor=FakeExtractor())
        result = await ext.extract(b"hello", "doc.pdf")
        assert result == "extracted:5bytes"

    @pytest.mark.asyncio
    async def test_extract_path_success(self):
        ext = DocumentExtractor(extractor=FakeExtractor())
        result = await ext.extract("/tmp/report.pdf")
        assert result == "extracted:path=/tmp/report.pdf"

    @pytest.mark.asyncio
    async def test_extract_bytes_no_filename_raises(self):
        ext = DocumentExtractor(extractor=FakeExtractor())
        with pytest.raises(ValueError, match="filename must be provided"):
            await ext.extract(b"hello")

    @pytest.mark.asyncio
    async def test_unsupported_type_returns_none(self):
        ext = DocumentExtractor(extractor=FakeExtractor())
        result = await ext.extract(b"abc", "unsupported.xyz")
        assert result is None

    @pytest.mark.asyncio
    async def test_extraction_error_raises_value_error(self):
        ext = DocumentExtractor(extractor=FakeExtractor())
        with pytest.raises(ValueError, match="Failed to extract"):
            await ext.extract(b"corrupt", "doc.pdf")

    def test_no_textxtract_raises_import_error(self, monkeypatch):
        """If textxtract is not installed, instantiation raises ImportError."""
        import tenxgraph_api.src.app.utils.media.extractor as mod

        monkeypatch.setattr(mod, "AsyncTextExtractor", None)
        with pytest.raises(ImportError, match="textxtract is required"):
            DocumentExtractor()

    def test_custom_extractor_injection(self):
        """Custom extractor can be injected for testing."""
        ext = DocumentExtractor(extractor=FakeExtractor())
        assert ext.extractor is not None
