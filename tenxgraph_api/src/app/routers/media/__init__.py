"""Media service — provides the configured MediaStore and DocumentPipeline."""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from injectq import InjectQ, inject, singleton
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.storage.media.config import DocumentHandling
from tenxgraph.storage.media.storage.base import BaseMediaStore

from tenxgraph_api.src.app.core.auth.request_config import ClientInputError
from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.utils.media.extractor import DocumentExtractor
from tenxgraph_api.src.app.utils.media.pipeline import DocumentPipeline


logger = logging.getLogger("tenxgraph_api.media")


_SIGNED_URL_NAMESPACE = "media:signed-url"
_EXTRACTION_NAMESPACE = "media:extraction"

# In-process caches in front of the shared checkpointer cache. Bounded so a stream of new
# uploads cannot grow a worker's memory without limit.
MAX_SIGNED_URL_ENTRIES = 4096
MAX_EXTRACTION_ENTRIES = 512
MAX_EXTRACTION_CHARS = 32_000_000


class _BoundedCache(OrderedDict):
    """A dict that evicts least-recently-used entries past ``max_entries`` or ``max_chars``.

    ``max_chars`` sums ``len()`` of string values, so a few large extractions cannot hold
    more memory than the entry count suggests.
    """

    def __init__(self, max_entries: int, max_chars: int | None = None):
        super().__init__()
        self.max_entries = max_entries
        self.max_chars = max_chars
        self._chars = 0

    @staticmethod
    def _size(value: Any) -> int:
        return len(value) if isinstance(value, str) else 0

    def get(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            return default
        self.move_to_end(key)
        return self[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            self._chars -= self._size(self[key])
        super().__setitem__(key, value)
        self.move_to_end(key)
        self._chars += self._size(value)
        while len(self) > self.max_entries or (
            self.max_chars is not None and self._chars > self.max_chars and len(self) > 1
        ):
            self.popitem(last=False)

    def __delitem__(self, key: Any) -> None:
        self._chars -= self._size(self[key])
        super().__delitem__(key)

    def pop(self, key: Any, *default: Any) -> Any:
        if key in self:
            self._chars -= self._size(self[key])
        return super().pop(key, *default)

    def popitem(self, last: bool = True) -> tuple[Any, Any]:
        key, value = super().popitem(last=last)
        self._chars -= self._size(value)
        return key, value


def _build_config_instance(config_cls: type[Any], values: dict[str, Any]) -> Any:
    """Instantiate third-party config classes using only supported kwargs."""
    params = inspect.signature(config_cls).parameters
    supported = {
        name: value for name, value in values.items() if name in params and value not in (None, "")
    }
    return config_cls(**supported)


def _create_media_store(settings: MediaSettings) -> BaseMediaStore:
    """Factory that instantiates the right MediaStore from settings."""
    stype = settings.MEDIA_STORAGE_TYPE

    if stype == MediaStorageType.MEMORY:
        from tenxgraph.storage.media.storage.memory_store import InMemoryMediaStore

        return InMemoryMediaStore()

    if stype == MediaStorageType.LOCAL:
        from tenxgraph.storage.media.storage.local_store import LocalFileMediaStore

        return LocalFileMediaStore(base_dir=settings.MEDIA_STORAGE_PATH)

    if stype == MediaStorageType.CLOUD:
        from cloud_storage_manager import (
            AwsConfig,
            CloudStorageFactory,
            StorageConfig,
            StorageProvider,
        )
        from tenxgraph.storage.media.storage.cloud_store import CloudMediaStore

        provider_name = settings.MEDIA_CLOUD_PROVIDER.lower()
        provider = getattr(StorageProvider, provider_name.upper(), None)
        if provider is None and provider_name == "gcs":
            provider = getattr(StorageProvider, "GCP", None)
        if provider is None:
            raise ValueError(f"Unsupported MEDIA_CLOUD_PROVIDER: {settings.MEDIA_CLOUD_PROVIDER}")

        storage_kwargs: dict[str, Any] = {}

        if provider_name == "aws":
            storage_kwargs["aws"] = _build_config_instance(
                AwsConfig,
                {
                    "bucket_name": settings.MEDIA_CLOUD_BUCKET,
                    "region": settings.MEDIA_CLOUD_REGION,
                    "region_name": settings.MEDIA_CLOUD_REGION,
                    "access_key_id": settings.MEDIA_CLOUD_ACCESS_KEY_ID,
                    "secret_access_key": settings.MEDIA_CLOUD_SECRET_ACCESS_KEY,
                    "session_token": settings.MEDIA_CLOUD_SESSION_TOKEN,
                },
            )
        else:
            from cloud_storage_manager import GcpConfig

            credentials = None
            if settings.MEDIA_CLOUD_CREDENTIALS_JSON:
                try:
                    credentials = json.loads(settings.MEDIA_CLOUD_CREDENTIALS_JSON)
                except json.JSONDecodeError as exc:
                    raise ValueError("MEDIA_CLOUD_CREDENTIALS_JSON must be valid JSON") from exc

            storage_kwargs["gcp"] = _build_config_instance(
                GcpConfig,
                {
                    "bucket_name": settings.MEDIA_CLOUD_BUCKET,
                    "project_id": settings.MEDIA_CLOUD_PROJECT_ID,
                    "credentials": credentials,
                    "credentials_json": credentials,
                },
            )

        storage = CloudStorageFactory.get_storage(provider, StorageConfig(**storage_kwargs))
        return CloudMediaStore(
            storage=storage,
            prefix=settings.MEDIA_CLOUD_PREFIX,
        )

    raise ValueError(f"Unknown MEDIA_STORAGE_TYPE: {stype}")


def _create_document_pipeline(settings: MediaSettings) -> DocumentPipeline:
    handling_map = {
        "extract_text": DocumentHandling.EXTRACT_TEXT,
        "pass_raw": DocumentHandling.FORWARD_RAW,
        "skip": DocumentHandling.SKIP,
    }
    handling = handling_map.get(settings.DOCUMENT_HANDLING, DocumentHandling.EXTRACT_TEXT)

    extractor = DocumentExtractor() if handling == DocumentHandling.EXTRACT_TEXT else None
    return DocumentPipeline(document_extractor=extractor, handling=handling)


@singleton
class MediaService:
    """Central media service providing storage + document extraction."""

    @inject
    def __init__(
        self,
        settings: MediaSettings | None = None,
        checkpointer: BaseCheckpointer | None = None,
    ):
        from tenxgraph_api.src.app.core.config.media_settings import get_media_settings

        self._settings = settings or get_media_settings()
        self._checkpointer = checkpointer
        self._store: BaseMediaStore | None = None
        self._pipeline: DocumentPipeline | None = None
        # file_id -> extracted text
        self._extraction_cache = _BoundedCache(MAX_EXTRACTION_ENTRIES, MAX_EXTRACTION_CHARS)
        # file_id -> signed URL payload
        self._signed_url_cache = _BoundedCache(MAX_SIGNED_URL_ENTRIES)

    @property
    def store(self) -> BaseMediaStore:
        if self._store is None:
            self._store = _create_media_store(self._settings)
        return self._store

    @property
    def pipeline(self) -> DocumentPipeline:
        if self._pipeline is None:
            self._pipeline = _create_document_pipeline(self._settings)
        return self._pipeline

    @property
    def checkpointer(self) -> BaseCheckpointer | None:
        if self._checkpointer is None:
            try:
                self._checkpointer = InjectQ.get_instance().try_get(BaseCheckpointer)
            except Exception:
                self._checkpointer = None
        return self._checkpointer

    @property
    def max_size_bytes(self) -> int:
        return int(self._settings.MEDIA_MAX_SIZE_MB * 1024 * 1024)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    async def ensure_can_access(self, file_id: str, user_id: str | None) -> None:
        """Reject access to a file owned by a different user.

        Files had no owner concept at all: `get_file(file_id)` returned bytes to any
        authenticated caller who knew (or guessed) the id. Ownership is recorded in
        the file's own metadata at upload time, so it is as durable as the file.

        Legacy files uploaded before ownership existed, or while auth was off, carry no
        ``owner_id``. ``MEDIA_REQUIRE_OWNER`` decides what happens to them: denied (the
        production default) or allowed with a warning (the development default).
        """
        if not user_id:
            return

        try:
            metadata = await self.store.get_metadata(file_id)
        except Exception:
            metadata = None

        if metadata is None:
            raise KeyError(f"File not found: {file_id}")

        owner_id = metadata.get("owner_id")

        if owner_id is None:
            if self._settings.MEDIA_REQUIRE_OWNER:
                raise PermissionError(f"File has no recorded owner: {file_id}")
            logger.warning(
                "File %s has no recorded owner (uploaded before ownership tracking); "
                "allowing access. Set MEDIA_REQUIRE_OWNER=true to deny these.",
                log_safe(file_id),
            )
            return

        if str(owner_id) != str(user_id):
            # Deliberately not "you don't own this" -- do not confirm the id exists.
            logger.warning(
                "User %s attempted to access file %s owned by another user",
                log_safe(user_id),
                log_safe(file_id),
            )
            raise PermissionError(f"File not accessible: {file_id}")

    async def upload_file(
        self,
        data: bytes,
        filename: str,
        mime_type: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload a file: store binary + optionally extract text.

        Records the uploader as the file's owner, so it cannot later be read by a
        different user who happens to know the id.

        Returns a dict with file_id, mime_type, size_bytes, filename,
        extracted_text (nullable), and url.
        """
        if len(data) > self.max_size_bytes:
            raise ClientInputError(
                f"File size {len(data)} exceeds maximum {self._settings.MEDIA_MAX_SIZE_MB}MB"
            )

        # Store binary in the configured MediaStore. The owner is written into the
        # file's own metadata, so ownership survives as long as the file does --
        # unlike a cache entry, which would expire and silently un-own it.
        file_metadata: dict[str, Any] = {"filename": filename}
        if user_id:
            file_metadata["owner_id"] = str(user_id)

        storage_key = await self.store.store(
            data,
            mime_type,
            metadata=file_metadata,
        )

        result: dict[str, Any] = {
            "file_id": storage_key,
            "mime_type": mime_type,
            "size_bytes": len(data),
            "filename": filename,
            "extracted_text": None,
            "url": f"/v1/files/{storage_key}",
            "direct_url": None,
            "direct_url_expires_at": None,
        }

        # If it's a document, try extraction
        if self.pipeline.handling == DocumentHandling.EXTRACT_TEXT and self._is_extractable(
            mime_type
        ):
            try:
                text = await self.pipeline.extractor.extract(data, filename, mime_type)
                if text:
                    result["extracted_text"] = text
                    await self._cache_extraction(storage_key, text)
            except Exception as exc:
                logger.warning("Extraction failed for %s: %s", filename, exc)

        direct_url_info = await self.get_direct_url_info(
            storage_key,
            mime_type=mime_type,
        )
        if direct_url_info:
            result["direct_url"] = direct_url_info["url"]
            result["direct_url_expires_at"] = direct_url_info["expires_at"]

        return result

    async def get_file(self, file_id: str, user_id: str | None = None) -> tuple[bytes, str]:
        """Retrieve file bytes and MIME type."""
        if user_id:
            await self.ensure_can_access(file_id, user_id)
        return await self.store.retrieve(file_id)

    async def local_file(self, file_id: str) -> tuple[Path, str] | None:
        """``(path, mime_type)`` of a file the local filesystem store holds, else None.

        Lets the download route stream the file from disk instead of reading it whole. Only
        the core ``LocalFileMediaStore`` qualifies; any other store (memory, cloud) and any
        lookup problem returns None, and the caller falls back to ``get_file``. Call it only
        after ``ensure_can_access``.
        """
        from tenxgraph.storage.media.storage.local_store import LocalFileMediaStore

        store = self.store
        key_to_path = getattr(store, "_key_to_path", None)
        if not isinstance(store, LocalFileMediaStore) or not callable(key_to_path):
            return None
        try:
            metadata = await store.get_metadata(file_id)
            if not metadata or not metadata.get("mime_type"):
                return None
            path = Path(key_to_path(file_id, metadata["mime_type"]))
        except Exception:  # invalid key, unreadable metadata: let get_file decide
            return None
        return (path, metadata["mime_type"]) if path.is_file() else None

    async def get_file_info(self, file_id: str, user_id: str | None = None) -> dict[str, Any]:
        """Return metadata about a stored file."""
        if user_id:
            await self.ensure_can_access(file_id, user_id)
        metadata = await self.store.get_metadata(file_id)
        if metadata is None:
            raise KeyError(f"File not found: {file_id}")

        info = {
            "file_id": file_id,
            "mime_type": metadata["mime_type"],
            "size_bytes": metadata["size_bytes"],
            "filename": metadata.get("filename"),
            "extracted_text": await self.aget_cached_extraction(file_id),
            "direct_url": None,
            "direct_url_expires_at": None,
        }
        direct_url_info = await self.get_direct_url_info(
            file_id,
            mime_type=metadata["mime_type"],
        )
        if direct_url_info:
            info["direct_url"] = direct_url_info["url"]
            info["direct_url_expires_at"] = direct_url_info["expires_at"]
        return info

    async def get_direct_url_info(
        self,
        file_id: str,
        mime_type: str | None = None,
        expiration_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """Return a cached direct URL payload when the store supports it."""
        ttl = expiration_seconds or self._settings.MEDIA_SIGNED_URL_TTL_SECONDS
        refresh_buffer = self._settings.MEDIA_SIGNED_URL_REFRESH_BUFFER_SECONDS

        if mime_type is None:
            metadata = await self.store.get_metadata(file_id)
            if metadata is None:
                raise KeyError(f"File not found: {file_id}")
            mime_type = metadata["mime_type"]

        cache_key = f"{file_id}:{mime_type}:{ttl}"
        cached = await self._get_cached_payload(
            namespace=_SIGNED_URL_NAMESPACE,
            cache_key=cache_key,
            local_cache=self._signed_url_cache,
        )
        if cached and cached.get("expires_at", 0) > int(time.time()) + refresh_buffer:
            return cached

        direct_url = await self.store.get_direct_url(
            file_id,
            mime_type=mime_type,
            expiration=ttl,
        )
        if direct_url is None:
            return None

        payload = {
            "url": direct_url,
            "expires_at": int(time.time() + ttl),
        }
        await self._cache_payload(
            namespace=_SIGNED_URL_NAMESPACE,
            cache_key=cache_key,
            payload=payload,
            local_cache=self._signed_url_cache,
            ttl_seconds=ttl,
        )
        return payload

    def get_cached_extraction(self, file_id: str) -> str | None:
        """Return cached extracted text for a file_id."""
        return self._extraction_cache.get(file_id)

    async def aget_cached_extraction(self, file_id: str) -> str | None:
        """Return cached extracted text from local memory or shared cache."""
        cached = self._extraction_cache.get(file_id)
        if cached is not None:
            return cached

        payload = await self._get_cached_payload(
            namespace=_EXTRACTION_NAMESPACE,
            cache_key=file_id,
            local_cache=None,
        )
        if isinstance(payload, dict):
            text = payload.get("text")
            if isinstance(text, str):
                self._extraction_cache[file_id] = text
                return text
        return None

    async def _cache_extraction(self, file_id: str, text: str) -> None:
        self._extraction_cache[file_id] = text
        await self._cache_payload(
            namespace=_EXTRACTION_NAMESPACE,
            cache_key=file_id,
            payload={"text": text},
            local_cache=None,
            ttl_seconds=24 * 3600,
        )

    async def _get_cached_payload(
        self,
        namespace: str,
        cache_key: str,
        local_cache: dict[str, dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        if local_cache is not None:
            cached = local_cache.get(cache_key)
            if cached and cached.get("expires_at", float("inf")) > time.time():
                return cached
            if cached:
                local_cache.pop(cache_key, None)

        if self.checkpointer is None:
            return None

        payload = await self.checkpointer.aget_cache_value(namespace, cache_key)
        if isinstance(payload, dict) and local_cache is not None:
            local_cache[cache_key] = payload
        return payload if isinstance(payload, dict) else None

    async def _cache_payload(
        self,
        namespace: str,
        cache_key: str,
        payload: dict[str, Any],
        local_cache: dict[str, dict[str, Any]] | None,
        ttl_seconds: int,
    ) -> None:
        if local_cache is not None:
            local_cache[cache_key] = payload

        if self.checkpointer is not None:
            await self.checkpointer.aput_cache_value(
                namespace,
                cache_key,
                payload,
                ttl_seconds=ttl_seconds,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_extractable(mime_type: str) -> bool:
        extractable = {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
            "text/html",
            "text/xml",
            "application/xml",
            "text/markdown",
            "text/csv",
            "application/json",
            "text/plain",
        }
        return mime_type.lower() in extractable
