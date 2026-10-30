"""Document extraction service using textxtract.

Wraps ``AsyncTextExtractor`` from the textxtract library.
This module lives in 10xgraph-api, NOT in the core 10xGraph library,
because extraction is an API-platform concern.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any


logger = logging.getLogger("tenxgraph_api.media.extractor")

try:
    from textxtract import AsyncTextExtractor
    from textxtract.core.exceptions import ExtractionError, FileTypeNotSupportedError
except ImportError:  # pragma: no cover
    AsyncTextExtractor = None  # type: ignore[assignment]


# The handler textxtract runs is chosen from the file extension. Uploads never pass their
# own file name through: the declared MIME type picks one of these extensions, and a file
# with any other type (archives included) is not extracted. That keeps a file named
# ``a.zip`` out of textxtract's ZIP handler, which unpacks nested archives with no overall
# size budget.
HANDLER_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "text/html": ".html",
    "text/xml": ".xml",
    "application/xml": ".xml",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/plain": ".txt",
}
_EXTRACTABLE_EXTENSIONS = frozenset(HANDLER_EXTENSIONS.values())

# DOCX files are ZIP containers, so they are inspected before parsing: a small upload can
# declare gigabytes of XML.
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_ARCHIVE_ENTRIES = 10_000

EXTRACTION_TIMEOUT_SECONDS = 30.0

# textxtract parses in a worker thread that cannot be cancelled, so a timeout only stops the
# wait. Two limits keep the damage bounded: a size cap before parsing, and a small number of
# extraction slots that stay taken until the thread really finishes.
#
# Markup and text parsers expand their input: a 12 MB ``text/html`` file used about 1.2 GB RSS
# (roughly 100x) and a 20 MB one took 32.8 s of CPU. 5 MB keeps one parse near 500 MB and
# inside the timeout. DOCX is bounded by the archive checks above and PDF by the caps below.
MAX_TEXT_DOCUMENT_BYTES = 5 * 1024 * 1024
_SIZE_CAPPED_EXTENSIONS = frozenset({".html", ".xml", ".csv", ".md", ".json", ".txt"})
# At most this many extractions run (or are still running after a timeout) at once.
MAX_CONCURRENT_EXTRACTIONS = 2
MAX_EXTRACTED_CHARS = 1_000_000


# PDF caps. A 0.9 MB PDF can inflate to ~1 GB while parsed and a 100,000-page one outlives the
# timeout, so both are checked before any parser sees the bytes. The page count is read from the
# raw bytes (page objects and page-tree ``/Count`` values), with no parsing: a PDF that hides
# its page tree in compressed object streams is bounded by the size cap alone.
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 500
_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_PDF_COUNT_RE = re.compile(rb"/Count\s+(\d{1,9})")


class DocumentTooLargeError(ValueError):
    """A document is over a configured size or page limit (the upload route answers 413)."""


def pdf_page_estimate(data: bytes) -> int:
    """Best-effort page count read from the raw bytes, without parsing the PDF."""
    pages = len(_PDF_PAGE_RE.findall(data))
    counts = [int(m) for m in _PDF_COUNT_RE.findall(data)]
    return max([pages, *counts])


def check_pdf_limits(data: bytes, max_bytes: int | None, max_pages: int | None) -> None:
    """Raise :class:`DocumentTooLargeError` when a PDF is over the size or page cap.

    ``None`` or ``0`` disables a cap. Runs before parsing.
    """
    if max_bytes and len(data) > max_bytes:
        raise DocumentTooLargeError(
            f"PDF is {len(data)} bytes; the limit for text extraction is {max_bytes} bytes"
        )
    if max_pages:
        pages = pdf_page_estimate(data)
        if pages > max_pages:
            raise DocumentTooLargeError(
                f"PDF has about {pages} pages; the limit for text extraction is {max_pages} pages"
            )


def _handler_extension(filename: str | None, mime_type: str | None) -> str | None:
    """The extension (and so the handler) to extract with, or None to skip extraction."""
    if mime_type:
        return HANDLER_EXTENSIONS.get(mime_type.split(";", 1)[0].strip().lower())
    suffix = PurePosixPath(filename or "").suffix.lower()
    return suffix if suffix in _EXTRACTABLE_EXTENSIONS else None


def _check_zip_container(data: bytes) -> None:
    """Reject a ZIP-based document whose contents would expand far beyond its size."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise ValueError("Document is not a valid ZIP-based file") from exc

    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("Document has too many parts to extract")
    total = sum(entry.file_size for entry in entries)
    if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ValueError("Document is too large to extract once uncompressed")
    for entry in entries:
        if entry.file_size > MAX_COMPRESSION_RATIO * max(entry.compress_size, 1):
            raise ValueError(f"Document part {entry.filename!r} has a suspicious compression ratio")


def _as_text(result: Any) -> str | None:
    if result is None:
        return None
    if isinstance(result, list | tuple):
        result = "\n\n".join(str(part) for part in result)
    text = str(result)
    if len(text) > MAX_EXTRACTED_CHARS:
        logger.warning(
            "Extracted text truncated from %d to %d chars", len(text), MAX_EXTRACTED_CHARS
        )
        text = text[:MAX_EXTRACTED_CHARS]
    return text


class DocumentExtractor:
    """Wraps textxtract AsyncTextExtractor for API-side document extraction.

    Examples::

        extractor = DocumentExtractor()
        text = await extractor.extract(pdf_bytes, "report.pdf")
    """

    def __init__(
        self,
        extractor: Any | None = None,
        max_pdf_bytes: int | None = MAX_PDF_BYTES,
        max_pdf_pages: int | None = MAX_PDF_PAGES,
    ):
        self.max_pdf_bytes = max_pdf_bytes
        self.max_pdf_pages = max_pdf_pages
        if extractor is not None:
            self.extractor = extractor
            return

        if AsyncTextExtractor is None:
            raise ImportError(
                "textxtract is required for document extraction. "
                'Install with `pip install "10xgraph-api[media]"`'
            )

        self.extractor = AsyncTextExtractor()

    def check_limits(self, data: bytes, mime_type: str | None) -> None:
        """Raise :class:`DocumentTooLargeError` when ``data`` is a PDF over the caps.

        Lets the upload route refuse the file up front instead of storing it and then skipping
        its text.
        """
        if _handler_extension(None, mime_type) == ".pdf":
            check_pdf_limits(data, self.max_pdf_bytes, self.max_pdf_pages)

    def _slots(self) -> asyncio.Semaphore:
        slots = getattr(self, "_slot_semaphore", None)
        if slots is None:
            slots = self._slot_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
        return slots

    async def _extract_bounded(self, data: bytes, filename: str) -> Any:
        """Run one extraction under the timeout without ever exceeding the slot limit.

        The slot is released when the extraction task finishes, not when the wait times
        out, so a stuck parse keeps its slot and cannot be outnumbered by new ones.
        """
        slots = self._slots()
        try:
            await asyncio.wait_for(slots.acquire(), timeout=EXTRACTION_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise TimeoutError("no extraction slot free") from exc
        try:
            task = asyncio.ensure_future(self.extractor.extract(data, filename))
        except BaseException:
            slots.release()
            raise
        task.add_done_callback(lambda _t: slots.release())
        # shield: cancelling the wait must not cancel the task, or the slot would be freed
        # while the worker thread is still running.
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=EXTRACTION_TIMEOUT_SECONDS)
        except BaseException:
            # Nobody will read the result of a task we stopped waiting for.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            raise

    async def extract(
        self,
        data: bytes | str,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> str | None:
        """Extract text from bytes or a local path.

        For bytes, the handler comes from ``mime_type`` when given, otherwise from the
        extension of ``filename``, and only document types are extracted: archives and
        unknown types return ``None``. The client's file name is never handed to textxtract.

        Args:
            data: Raw bytes or local path.
            filename: Required when passing bytes (used only when ``mime_type`` is absent).
            mime_type: The declared content type; preferred for picking the handler.

        Returns:
            Extracted text (capped at ``MAX_EXTRACTED_CHARS``), or ``None`` when the file
            type is not extracted.

        Raises:
            ValueError: For a missing filename, an oversized or bomb-like document, a
                timeout, or failed extraction.
        """
        if isinstance(data, bytes) and not filename:
            raise ValueError("filename must be provided when extracting from bytes")

        try:
            if isinstance(data, bytes):
                extension = _handler_extension(filename, mime_type)
                if extension is None:
                    logger.info(
                        "Not extracting %s (%s): not a supported document", filename, mime_type
                    )
                    return None
                if extension == ".pdf":
                    check_pdf_limits(data, self.max_pdf_bytes, self.max_pdf_pages)
                if extension == ".docx":
                    _check_zip_container(data)
                if extension in _SIZE_CAPPED_EXTENSIONS and len(data) > MAX_TEXT_DOCUMENT_BYTES:
                    logger.warning(
                        "Not extracting %s: %d bytes exceeds the %d byte limit for %s files",
                        filename,
                        len(data),
                        MAX_TEXT_DOCUMENT_BYTES,
                        extension,
                    )
                    return None
                result = await self._extract_bounded(data, f"upload{extension}")
                return _as_text(result)
            return _as_text(await self.extractor.extract(data))

        except TimeoutError as exc:
            logger.warning("Document extraction timed out for %s", filename)
            raise ValueError("Document extraction timed out") from exc

        except FileTypeNotSupportedError:  # type: ignore
            logger.warning("Document type not supported for extraction: %s", filename)
            return None
        except ExtractionError as exc:  # type: ignore
            logger.exception("Document extraction failed for %s", filename)
            raise ValueError("Failed to extract text from document") from exc
