"""Media service — provides the configured MediaStore and DocumentPipeline."""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from injectq import InjectQ, inject, singleton
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.storage.media.config import DocumentHandling
from tenxgraph.storage.media.offload import MediaOffloadPolicy, ensure_media_offloaded
from tenxgraph.storage.media.security import check_image_pixels
from tenxgraph.storage.media.storage.base import BaseMediaStore

from tenxgraph_api.src.app.core.auth.request_config import ClientInputError
from tenxgraph_api.src.app.core.config.graph_config import MediaConfig
from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.utils.media.extractor import DocumentExtractor, DocumentTooLargeError
from tenxgraph_api.src.app.utils.media.pipeline import DocumentPipeline


logger = logging.getLogger("tenxgraph_api.media")


_SIGNED_URL_NAMESPACE = "media:signed-url"
_EXTRACTION_NAMESPACE = "media:extraction"

# Metadata key listing the threads whose messages reference a file. Written empty at upload
# (so a file without the key is known to predate link tracking and is never auto-deleted).
THREADS_KEY = "threads"

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
    # The store enforces the upload cap itself, so offloaded and other non-upload writes
    # cannot go past it either.
    max_bytes = int(settings.MEDIA_MAX_SIZE_MB * 1024 * 1024)

    if stype == MediaStorageType.MEMORY:
        from tenxgraph.storage.media.storage.memory_store import InMemoryMediaStore

        return InMemoryMediaStore(max_size_bytes=max_bytes)

    if stype == MediaStorageType.LOCAL:
        from tenxgraph.storage.media.storage.local_store import LocalFileMediaStore

        return LocalFileMediaStore(base_dir=settings.MEDIA_STORAGE_PATH, max_size_bytes=max_bytes)

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
            max_size_bytes=max_bytes,
        )

    raise ValueError(f"Unknown MEDIA_STORAGE_TYPE: {stype}")


def _create_document_pipeline(settings: MediaSettings) -> DocumentPipeline:
    handling_map = {
        "extract_text": DocumentHandling.EXTRACT_TEXT,
        "pass_raw": DocumentHandling.FORWARD_RAW,
        "skip": DocumentHandling.SKIP,
    }
    handling = handling_map.get(settings.DOCUMENT_HANDLING, DocumentHandling.EXTRACT_TEXT)

    extractor = (
        DocumentExtractor(
            max_pdf_bytes=int(settings.MEDIA_MAX_PDF_MB * 1024 * 1024),
            max_pdf_pages=settings.MEDIA_MAX_PDF_PAGES,
        )
        if handling == DocumentHandling.EXTRACT_TEXT
        else None
    )
    return DocumentPipeline(document_extractor=extractor, handling=handling)


class _OwnedStore:
    """Wraps a media store so offloaded blobs get the same metadata as an upload."""

    def __init__(self, store: BaseMediaStore, user_id: str | None, track_threads: bool):
        self._store = store
        self._user_id = user_id
        self._track_threads = track_threads

    async def store(self, data: bytes, mime_type: str, metadata: dict[str, Any] | None = None):
        meta: dict[str, Any] = {"created_at": int(time.time()), **(metadata or {})}
        if self._user_id:
            meta["owner_id"] = str(self._user_id)
            if self._track_threads:
                meta[THREADS_KEY] = []
        return await self._store.store(data, mime_type, metadata=meta)

    def to_media_ref(self, *args: Any, **kwargs: Any):
        return self._store.to_media_ref(*args, **kwargs)


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
        self._links_notice_logged = False
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

        if mime_type.lower().startswith("image/"):
            try:
                check_image_pixels(data, self._settings.MEDIA_MAX_IMAGE_PIXELS or None)
            except ValueError as exc:
                raise ClientInputError(str(exc)) from exc

        extracting = self.pipeline.handling == DocumentHandling.EXTRACT_TEXT and (
            self._is_extractable(mime_type)
        )
        check_limits = (
            getattr(self.pipeline.extractor, "check_limits", None) if extracting else None
        )
        if callable(check_limits):
            try:
                check_limits(data, mime_type)
            except DocumentTooLargeError as exc:
                raise ClientInputError(str(exc)) from exc

        await self._check_quota(user_id, len(data))

        # Store binary in the configured MediaStore. The owner is written into the
        # file's own metadata, so ownership survives as long as the file does --
        # unlike a cache entry, which would expire and silently un-own it.
        file_metadata: dict[str, Any] = {"filename": filename, "created_at": int(time.time())}
        if user_id:
            file_metadata["owner_id"] = str(user_id)
            if self.supports_links:
                file_metadata[THREADS_KEY] = []

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
        if extracting:
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

    async def _check_quota(self, user_id: str | None, incoming_bytes: int) -> None:
        """Refuse an upload that would take ``user_id`` past the per-user file or byte quota.

        Does nothing without a user, with both quotas off, or when the store cannot list its
        files (a warning is logged once). Counts only files that record this user as owner.
        """
        max_files = self._settings.MEDIA_MAX_FILES_PER_USER
        max_bytes = int(self._settings.MEDIA_MAX_USER_STORAGE_MB * 1024 * 1024)
        if not user_id or (not max_files and not max_bytes):
            return
        try:
            keys = await self._keys_of(user_id)
        except NotImplementedError:
            if not getattr(self, "_quota_notice_logged", False):
                self._quota_notice_logged = True
                logger.warning(
                    "MEDIA_MAX_FILES_PER_USER / MEDIA_MAX_USER_STORAGE_MB are set but %s "
                    "cannot list files; the per-user quota is not enforced.",
                    type(self.store).__name__,
                )
            return
        count = 0
        used = 0
        for key in keys:
            try:
                meta = await self.store.get_metadata(key)
            except Exception as exc:
                logger.debug("Skipping unreadable file %s in quota count: %s", log_safe(key), exc)
                continue
            if meta and str(meta.get("owner_id")) == str(user_id):
                count += 1
                used += int(meta.get("size_bytes") or 0)
        if max_files and count >= max_files:
            raise ClientInputError(f"Upload limit reached: at most {max_files} files per user")
        if max_bytes and used + incoming_bytes > max_bytes:
            raise ClientInputError(
                f"Storage limit reached: at most {self._settings.MEDIA_MAX_USER_STORAGE_MB}MB "
                "of files per user"
            )

    async def offload_inline(self, messages: list[Any], user_id: str | None = None) -> None:
        """Move large inline base64 media in ``messages`` into the store, in place.

        Uses core ``ensure_media_offloaded`` with ``MEDIA_INLINE_MAX_BYTES`` as the threshold
        (0 turns it off). Offloaded files are owned by ``user_id`` and carry the same metadata
        as an upload, so the caller can link them to the thread and they are deleted with it.

        Raises:
            ClientInputError: The blob is not valid base64 or exceeds ``MEDIA_MAX_SIZE_MB``.
        """
        threshold = self._settings.MEDIA_INLINE_MAX_BYTES
        if threshold <= 0:
            return
        store = _OwnedStore(self.store, user_id, self.supports_links)
        try:
            for message in messages:
                await ensure_media_offloaded(
                    message, store, MediaOffloadPolicy.THRESHOLD, max_inline_bytes=threshold
                )
        except ValueError as exc:  # bad base64 (binascii.Error) or MediaTooLargeError
            raise ClientInputError(f"Inline media rejected: {exc}") from exc

    # ------------------------------------------------------------------
    # Thread links and deletion
    # ------------------------------------------------------------------

    @property
    def supports_links(self) -> bool:
        """Whether the media store can update metadata in place (core with ``update_metadata``)."""
        return callable(getattr(self.store, "update_metadata", None))

    async def _update_metadata(self, key: str, updates: dict[str, Any]) -> bool:
        """``store.update_metadata`` (newer core only; callers check ``supports_links``)."""
        store: Any = self.store
        return bool(await store.update_metadata(key, updates))

    def delete_with_thread(self) -> bool:
        """``media.delete_with_thread`` from 10xgraph.json (default true)."""
        try:
            from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

            graph_config = InjectQ.get_instance().try_get(GraphConfig)
            media = getattr(graph_config, "media", None)
        except Exception:
            return True
        return bool(media.delete_with_thread) if isinstance(media, MediaConfig) else True

    async def _keys_of(self, user_id: str | None) -> list[str]:
        """Keys worth inspecting for ``user_id``; every key when there is no user.

        Uses the store's per-owner listing when it has one (the cloud store's owner index),
        else lists all keys. Callers still check each file's recorded owner.

        Raises:
            NotImplementedError: The store cannot enumerate the files.
        """
        if user_id:
            for_owner = getattr(self.store, "list_keys_for_owner", None)  # core before 1.0: none
            if callable(for_owner):
                return list(await for_owner(str(user_id)))
        list_keys = getattr(self.store, "list_keys", None)  # absent in core older than 1.0
        if not callable(list_keys):
            raise NotImplementedError(f"{type(self.store).__name__} cannot list files")
        return list(await list_keys())

    async def _modify_metadata(
        self, key: str, fn: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> bool:
        """Change one file's metadata from its current value as a single step.

        Uses the store's ``modify_metadata`` (atomic in the bundled stores). A store from an
        older core falls back to read then ``update_metadata``, which can lose a concurrent
        update. ``fn`` may run more than once, so it must only compute.
        """
        modify = getattr(self.store, "modify_metadata", None)
        if callable(modify):
            return bool(await modify(key, fn))
        metadata = await self.store.get_metadata(key)
        if metadata is None:
            return False
        updates = fn(dict(metadata))
        return await self._update_metadata(key, updates) if updates else True

    async def link_threads(self, keys: list[str], thread_id: str, user_id: str) -> None:
        """Record ``thread_id`` on each file the caller owns that an accepted message references.

        Never raises: a failure only means the file is not tracked, so it is never auto-deleted.
        Files without a ``threads`` list (uploaded before link tracking) are left alone.
        """
        if not keys:
            return
        if not self.supports_links:
            if not self._links_notice_logged:
                self._links_notice_logged = True
                logger.info(
                    "The installed core has no BaseMediaStore.update_metadata; uploaded files "
                    "are not linked to threads and never deleted automatically."
                )
            return
        for key in dict.fromkeys(keys):
            try:

                def add_link(metadata: dict[str, Any]) -> dict[str, Any] | None:
                    threads = metadata.get(THREADS_KEY)
                    if (
                        str(metadata.get("owner_id")) != str(user_id)
                        or not isinstance(threads, list)
                        or thread_id in threads
                    ):
                        return None
                    return {THREADS_KEY: [*threads, thread_id]}

                await self._modify_metadata(key, add_link)
            except Exception as exc:
                logger.warning("Could not link file %s to a thread: %s", log_safe(key), exc)

    async def unlink_threads(self, keys: list[str], thread_id: str) -> list[str]:
        """Drop ``thread_id`` from each file's links; delete a file left with none.

        The delete only happens with ``media.delete_with_thread`` on. Never raises; returns the
        keys that could not be released (empty when all were), so a caller can report a partial
        cleanup.
        """
        failed: list[str] = []
        if not keys or not self.supports_links:
            return failed
        delete = self.delete_with_thread()
        for key in dict.fromkeys(keys):
            try:
                last_link = False

                def drop_link(metadata: dict[str, Any]) -> dict[str, Any] | None:
                    nonlocal last_link
                    last_link = False
                    threads = metadata.get(THREADS_KEY)
                    if not isinstance(threads, list) or thread_id not in threads:
                        return None
                    remaining = [t for t in threads if t != thread_id]
                    if not remaining and delete:
                        last_link = True  # deleted below, not rewritten
                        return None
                    return {THREADS_KEY: remaining}

                await self._modify_metadata(key, drop_link)
                if last_link:
                    await self.delete_file(key)
            except Exception as exc:
                failed.append(key)
                logger.warning("Could not release file %s from a thread: %s", log_safe(key), exc)
        return failed

    async def release_thread(self, thread_id: str, user_id: str | None) -> list[str]:
        """Release every file ``user_id`` linked to ``thread_id``, whatever the messages say.

        ``unlink_threads`` works from the keys a thread's messages reference. A file linked
        through a message that is gone since (cleared state, replaced messages) would stay
        linked to a deleted thread forever; this finds those by scanning the store's metadata
        (needs ``list_keys()`` or ``list_keys_for_owner()``, so stores that cannot
        enumerate are skipped). Same rules as ``unlink_threads``: a file left with no thread is
        deleted only with ``media.delete_with_thread`` on. Never raises; returns the keys that
        could not be released.
        """
        if not self.supports_links or user_id is None:
            return []
        try:
            all_keys = await self._keys_of(user_id)
        except NotImplementedError:
            return []
        except Exception as exc:
            logger.warning("Could not list files for thread %s: %s", log_safe(thread_id), exc)
            return ["*"]
        linked: list[str] = []
        failed: list[str] = []
        for key in all_keys:
            try:
                metadata = await self.store.get_metadata(key)
            except Exception as exc:
                failed.append(key)
                logger.warning("Could not read file %s to release it: %s", log_safe(key), exc)
                continue
            threads = metadata.get(THREADS_KEY) if metadata else None
            if (
                isinstance(threads, list)
                and thread_id in threads
                and str(metadata.get("owner_id")) == str(user_id)
            ):
                linked.append(key)
        failed.extend(await self.unlink_threads(linked, thread_id))
        return failed

    async def delete_file(self, file_id: str) -> bool:
        """Delete a file's bytes and metadata; False when it does not exist."""
        mime_type: str | None = None
        try:
            metadata = await self.store.get_metadata(file_id)
            mime_type = metadata.get("mime_type") if metadata else None
        except Exception:
            metadata = None
        deleted = bool(await self.store.delete(file_id))
        await self._forget_cached(file_id, mime_type)
        return deleted

    async def _forget_cached(self, file_id: str, mime_type: str | None) -> None:
        """Drop the file's extracted text and signed URLs from the local and shared caches.

        The shared (checkpointer) entries outlive the file for 24 h otherwise, so a deleted
        file's text could still be read back. Never raises.
        """
        self._extraction_cache.pop(file_id, None)
        prefix = f"{file_id}:"
        for key in [k for k in self._signed_url_cache if str(k).startswith(prefix)]:
            self._signed_url_cache.pop(key, None)
        checkpointer = self.checkpointer
        if checkpointer is None:
            return
        keys = [(_EXTRACTION_NAMESPACE, file_id)]
        signed_keys: list[str] = []
        try:  # every ttl variant of the key; not all backends can list keys
            signed_keys = list(
                await checkpointer.alist_cache_keys(_SIGNED_URL_NAMESPACE, prefix=prefix) or []
            )
        except Exception as exc:
            logger.debug("Could not list signed-URL cache keys: %s", exc)
        if mime_type:
            ttl = self._settings.MEDIA_SIGNED_URL_TTL_SECONDS
            signed_keys.append(f"{file_id}:{mime_type}:{ttl}")
        keys += [(_SIGNED_URL_NAMESPACE, key) for key in dict.fromkeys(signed_keys)]
        for namespace, key in keys:
            try:
                await checkpointer.aclear_cache_value(namespace, key)
            except Exception as exc:
                logger.warning(
                    "Could not clear cached %s for %s: %s", namespace, log_safe(file_id), exc
                )

    async def list_files(
        self, user_id: str | None, offset: int = 0, limit: int = 50
    ) -> tuple[list[dict[str, Any]], bool]:
        """One page of the files ``user_id`` owns, newest first, and whether more follow.

        Files with no recorded owner are not listed for an identified user. With no ``user_id``
        (authentication off) every file is listed, matching ``ensure_can_access``. Reads each
        stored file's metadata, so the cost grows with the number of files in the store.

        Raises:
            NotImplementedError: The media store cannot enumerate its files.
        """
        keys = await self._keys_of(user_id)
        owned: list[dict[str, Any]] = []
        for key in keys:
            try:
                meta = await self.store.get_metadata(key)
            except Exception as exc:
                logger.debug("Skipping unreadable file %s while listing: %s", log_safe(key), exc)
                continue
            if not meta:
                continue
            if user_id and str(meta.get("owner_id")) != str(user_id):
                continue
            owned.append(
                {
                    "file_id": key,
                    "filename": meta.get("filename"),
                    "mime_type": meta.get("mime_type"),
                    "size_bytes": meta.get("size_bytes"),
                    "url": f"/v1/files/{key}",
                    "created_at": meta.get("created_at"),
                }
            )
        owned.sort(key=lambda f: f["file_id"])
        owned.sort(key=lambda f: f["created_at"] or 0, reverse=True)
        page = owned[offset : offset + limit]
        return page, offset + limit < len(owned)

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
