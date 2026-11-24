"""Media service: provides the configured MediaStore and document extractor."""

from __future__ import annotations

import base64
import binascii
import logging
import time
from functools import cached_property
from pathlib import Path
from typing import Any, cast

from injectq import inject, singleton
from tenxgraph.core.authz import Identity, Reach, can_reach
from tenxgraph.core.state.message_block import AudioBlock, DocumentBlock, ImageBlock, VideoBlock
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.storage.media.offload import MediaOffloadPolicy, ensure_media_offloaded
from tenxgraph.storage.media.security import check_image_pixels, validate_magic_bytes
from tenxgraph.storage.media.storage.base import BaseMediaStore

from tenxgraph_api.src.app.core.config.media_settings import MediaSettings
from tenxgraph_api.src.app.core.utils.container import lookup
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.core.validation.request_config import ClientInputError
from tenxgraph_api.src.app.routers.media.caching import (
    _SIGNED_URL_NAMESPACE,
    MAX_EXTRACTION_CHARS,
    MAX_EXTRACTION_ENTRIES,
    MAX_SIGNED_URL_ENTRIES,
    _BoundedCache,
    _CachingMixin,
)
from tenxgraph_api.src.app.routers.media.factory import (
    _create_document_extractor,
    _create_media_store,
)
from tenxgraph_api.src.app.routers.media.links import _LinksMixin
from tenxgraph_api.src.app.routers.media.ownership import (
    TENANT_KEY,
    THREADS_KEY,
    MissingTenantError,
    _OwnedStore,
    tenant_matches,
)
from tenxgraph_api.src.app.utils.media.extractor import DocumentExtractor, DocumentTooLargeError


logger = logging.getLogger("tenxgraph_api.media")

# The blocks core ``ensure_media_offloaded`` moves into the store.
_OFFLOADED_BLOCKS = (ImageBlock, AudioBlock, VideoBlock, DocumentBlock)


@singleton
class MediaService(_LinksMixin, _CachingMixin):
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
        # file_id -> extracted text
        self._extraction_cache = _BoundedCache(MAX_EXTRACTION_ENTRIES, MAX_EXTRACTION_CHARS)
        # file_id -> signed URL payload
        self._signed_url_cache = _BoundedCache(MAX_SIGNED_URL_ENTRIES)

    @property
    def store(self) -> BaseMediaStore:
        if self._store is None:
            self._store = _create_media_store(self._settings)
        return self._store

    @cached_property
    def extractor(self) -> DocumentExtractor | None:
        """Text extractor for uploaded documents; None with ``DOCUMENT_HANDLING`` pass_raw
        or skip. Built on first use, so a missing ``media`` extra is a 422 on upload."""
        return _create_document_extractor(self._settings)

    @property
    def checkpointer(self) -> BaseCheckpointer | None:
        if self._checkpointer is None:
            self._checkpointer = lookup(BaseCheckpointer)  # type: ignore[type-abstract]
        return self._checkpointer

    @property
    def max_size_bytes(self) -> int:
        return int(self._settings.MEDIA_MAX_SIZE_MB * 1024 * 1024)

    # ------------------------------------------------------------------
    # Tenant scope
    # ------------------------------------------------------------------

    def tenant_columns(self) -> tuple[str, ...]:
        """Tenant columns the bound checkpointer declares (empty when none or no checkpointer)."""
        try:
            checkpointer = self.checkpointer
        except AttributeError:  # a bare stand-in built without __init__
            return ()
        return tuple(getattr(checkpointer, "tenant_column_names", ()))

    def caller_tenant(self, user: Identity | None) -> dict[str, str | None]:
        """The caller's value for each declared tenant column, as canonical text.

        Read from ``Identity.tenant`` only. Empty when no tenant columns are declared; a
        missing value is None, which never matches a file.
        """
        columns = self.tenant_columns()
        if not columns:
            return {}
        values = user.tenant if user is not None else {}
        return {column: values.get(column) for column in columns}

    @staticmethod
    def _owned_by(metadata: dict[str, Any], user_id: Any, tenant: dict[str, str | None]) -> bool:
        """Whether the file records ``user_id`` as owner and ``tenant`` as its tenant values."""
        return str(metadata.get("owner_id")) == str(user_id) and tenant_matches(metadata, tenant)

    def _in_reach(self, metadata: dict[str, Any], user: Identity) -> bool:
        """Whether the file is within the caller's reach (reads, lists and deletes).

        Reach ``own``: the caller's own files. ``tenant`` and ``all`` add the files
        ``can_reach`` allows by the file's recorded owner and tenant values. With tenant
        columns declared, a file without recorded tenant values is never reachable, and a
        file without an owner is never widened to.
        """
        if self._owned_by(metadata, user.user_id, self.caller_tenant(user)):
            return True
        owner = metadata.get("owner_id")
        if user.reach is Reach.OWN or owner is None:
            return False
        recorded = metadata.get(TENANT_KEY)
        recorded = recorded if isinstance(recorded, dict) else {}
        tenant = {column: recorded.get(column) for column in self.tenant_columns()}
        if any(value is None for value in tenant.values()):
            return False
        return can_reach({"user": user}, owner, tenant)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    async def ensure_can_access(
        self,
        file_id: str,
        user_id: str | None,
        user: Identity | None = None,
        *,
        follow_reach: bool = False,
    ) -> None:
        """Reject access to a file owned by a different user (or tenant).

        Files had no owner concept at all: `get_file(file_id)` returned bytes to any
        authenticated caller who knew (or guessed) the id. Ownership is recorded in
        the file's own metadata at upload time, so it is as durable as the file.

        Legacy files uploaded before ownership existed, or while auth was off, carry no
        ``owner_id``. ``MEDIA_REQUIRE_OWNER`` decides what happens to them: denied (the
        production default) or allowed with a warning (the development default).

        With tenant columns declared on the checkpointer, the file's recorded tenant values
        must also equal the caller's (``user``, the authenticated identity) on every column:
        the same ``user_id`` in two tenants is two identities. A caller without a value, and
        a file with no recorded tenant values (uploaded before tenant tracking or without an
        owner), are denied.

        ``follow_reach=True`` is for the file routes' reads and deletes: they follow the
        caller's reach (``tenant`` and ``all`` widen them, see :meth:`_in_reach`). Everything
        else (a ``graph://media/`` or ``file_id`` reference in a run or a thread write, thread
        links) stays with the caller's own files.
        """
        if not user_id:
            return
        tenant = self.caller_tenant(user)

        try:
            metadata = await self.store.get_metadata(file_id)
        except Exception:
            metadata = None

        if metadata is None:
            raise KeyError(f"File not found: {file_id}")

        owner_id = metadata.get("owner_id")

        if owner_id is None:
            if tenant or self._settings.MEDIA_REQUIRE_OWNER:
                raise PermissionError(f"File has no recorded owner: {file_id}")
            logger.warning(
                "File %s has no recorded owner (uploaded before ownership tracking); "
                "allowing access. Set MEDIA_REQUIRE_OWNER=true to deny these.",
                log_safe(file_id),
            )
            return

        if follow_reach and user is not None:
            allowed = self._in_reach(metadata, user)
        else:
            allowed = self._owned_by(metadata, user_id, tenant)
        if not allowed:
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
        user: Identity | None = None,
    ) -> dict[str, Any]:
        """Upload a file: store binary + optionally extract text.

        Records the uploader as the file's owner, so it cannot later be read by a
        different user who happens to know the id. With tenant columns declared, the
        uploader's tenant values (from ``user``, the authenticated identity) are recorded
        too, under ``TENANT_KEY``.

        Raises:
            MissingTenantError: Tenant columns are declared and ``user`` lacks a value.

        Returns a dict with file_id, mime_type, size_bytes, filename,
        extracted_text (nullable), and url.
        """
        self._check_content(data, mime_type)

        extractor = self.extractor
        if not self._is_extractable(mime_type):
            extractor = None
        if extractor is not None:
            try:
                extractor.check_limits(data, mime_type)
            except DocumentTooLargeError as exc:
                raise ClientInputError(str(exc)) from exc

        tenant = self._upload_tenant(user_id, user)
        await self._check_quota(user_id, len(data), tenant)

        # Store binary in the configured MediaStore. The owner is written into the
        # file's own metadata, so ownership survives as long as the file does --
        # unlike a cache entry, which would expire and silently un-own it.
        file_metadata: dict[str, Any] = {"filename": filename, "created_at": int(time.time())}
        if user_id:
            file_metadata["owner_id"] = str(user_id)
            if tenant:
                file_metadata[TENANT_KEY] = tenant
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
        if extractor is not None:
            try:
                text = await extractor.extract(data, filename, mime_type)
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

    def _check_content(self, data: bytes, mime_type: str) -> None:
        """Refuse a file over ``MEDIA_MAX_SIZE_MB``, an image over the pixel limit, or bytes
        that do not start like ``mime_type`` (for types with a known signature).

        Raises:
            ClientInputError: One of the checks failed.
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
        if not validate_magic_bytes(data, mime_type):
            raise ClientInputError(
                f"file content does not match {mime_type.split(';', 1)[0].strip()}"
            )

    def _upload_tenant(self, user_id: str | None, user: Identity | None) -> dict[str, str | None]:
        """The tenant values an upload records (empty without a user or tenant columns).

        Raises:
            MissingTenantError: Tenant columns are declared and ``user`` lacks a value.
        """
        tenant = self.caller_tenant(user) if user_id else {}
        if any(value is None for value in tenant.values()):
            raise MissingTenantError("The authenticated user has no value for a tenant column")
        return tenant

    async def _check_quota(
        self,
        user_id: str | None,
        incoming_bytes: int,
        tenant: dict[str, str | None] | None = None,
        incoming_files: int = 1,
    ) -> None:
        """Refuse ``incoming_files`` new files that would take ``user_id`` past the per-user
        file or byte quota.

        Does nothing without a user, with both quotas off, or when the store cannot list its
        files (a warning is logged once). Counts only files that record this user as owner
        and, with tenant columns declared, the same tenant values (``tenant``), so the same
        ``user_id`` in two tenants has two quotas.
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
            if meta and self._owned_by(meta, user_id, tenant or {}):
                count += 1
                used += int(meta.get("size_bytes") or 0)
        if max_files and count + incoming_files > max_files:
            raise ClientInputError(f"Upload limit reached: at most {max_files} files per user")
        if max_bytes and used + incoming_bytes > max_bytes:
            raise ClientInputError(
                f"Storage limit reached: at most {self._settings.MEDIA_MAX_USER_STORAGE_MB}MB "
                "of files per user"
            )

    async def check_inline(
        self,
        messages: list[Any],
        user_id: str | None = None,
        user: Identity | None = None,
    ) -> None:
        """Apply the upload checks to the inline blobs :meth:`offload_inline` would store.

        Each blob over ``MEDIA_INLINE_MAX_BYTES`` gets the size, pixel and signature checks,
        and all of them together the per-user quota, before anything is stored. Stores
        nothing, so a route can call it before its response starts.

        Raises:
            ClientInputError: A blob is not valid base64 or fails a check, or the blobs would
                take the user past the quota.
        """
        threshold = self._settings.MEDIA_INLINE_MAX_BYTES
        if threshold <= 0:
            return
        total = 0
        count = 0
        for message in messages:
            for block in getattr(message, "content", None) or []:
                media = getattr(block, "media", None)
                if not isinstance(block, _OFFLOADED_BLOCKS) or media is None:
                    continue
                if media.kind != "data" or not media.data_base64:
                    continue
                # The same size estimate core's offload uses to pick the blobs it stores.
                if len(media.data_base64) * 3 // 4 <= threshold:
                    continue
                try:
                    data = base64.b64decode(media.data_base64)
                except (binascii.Error, ValueError) as exc:
                    raise ClientInputError(f"Inline media rejected: {exc}") from exc
                try:
                    self._check_content(data, media.mime_type or "application/octet-stream")
                except ClientInputError as exc:
                    raise ClientInputError(f"Inline media rejected: {exc}") from exc
                total += len(data)
                count += 1
        if count:
            tenant = self.caller_tenant(user) if user_id else {}
            await self._check_quota(user_id, total, tenant, incoming_files=count)

    async def offload_inline(
        self,
        messages: list[Any],
        user_id: str | None = None,
        user: Identity | None = None,
    ) -> None:
        """Move large inline base64 media in ``messages`` into the store, in place.

        Uses core ``ensure_media_offloaded`` with ``MEDIA_INLINE_MAX_BYTES`` as the threshold
        (0 turns it off). Offloaded files are owned by ``user_id`` and carry the same metadata
        as an upload (tenant values included), so the caller can link them to the thread and
        they are deleted with it.

        The blobs first pass the same checks as an upload (:meth:`check_inline`): size,
        pixels, signature and the per-user quota.

        Raises:
            ClientInputError: A blob is not valid base64, fails an upload check or would take
                the user past the quota, or a blob must be stored while tenant columns are
                declared and ``user`` lacks a value (nothing to scope the offloaded file to).
        """
        threshold = self._settings.MEDIA_INLINE_MAX_BYTES
        if threshold <= 0:
            return
        await self.check_inline(messages, user_id, user)
        tenant = self.caller_tenant(user) if user_id else {}
        store = _OwnedStore(self.store, user_id, tenant)
        try:
            for message in messages:
                await ensure_media_offloaded(
                    message,
                    cast("BaseMediaStore", store),
                    MediaOffloadPolicy.THRESHOLD,
                    max_inline_bytes=threshold,
                )
        except ValueError as exc:  # bad base64 (binascii.Error) or MediaTooLargeError
            raise ClientInputError(f"Inline media rejected: {exc}") from exc

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

    async def list_files(
        self,
        user_id: str | None,
        offset: int = 0,
        limit: int = 50,
        user: Identity | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """One page of the files within the caller's reach, newest first, and whether more
        follow.

        Reach ``own`` lists the files ``user_id`` owns; ``tenant`` and ``all`` widen the list
        (see :meth:`_in_reach`). With tenant columns declared, only files recording tenant
        values the caller's reach covers are listed.

        Files with no recorded owner are not listed for an identified user. With no ``user_id``
        (authentication off) every file is listed, matching ``ensure_can_access``. Reads each
        stored file's metadata, so the cost grows with the number of files in the store.

        Raises:
            NotImplementedError: The media store cannot enumerate its files.
        """
        widened = user is not None and user.reach is not Reach.OWN
        keys = await self._keys_of(None if widened else user_id)
        tenant = self.caller_tenant(user) if user_id else {}
        owned: list[dict[str, Any]] = []
        for key in keys:
            try:
                meta = await self.store.get_metadata(key)
            except Exception as exc:
                logger.debug("Skipping unreadable file %s while listing: %s", log_safe(key), exc)
                continue
            if not meta:
                continue
            if user is not None:
                if not self._in_reach(meta, user):
                    continue
            elif user_id and not self._owned_by(meta, user_id, tenant):
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

    async def get_file(
        self, file_id: str, user_id: str | None = None, user: Identity | None = None
    ) -> tuple[bytes, str]:
        """Retrieve file bytes and MIME type."""
        if user_id:
            await self.ensure_can_access(file_id, user_id, user)
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

    async def get_file_info(
        self, file_id: str, user_id: str | None = None, user: Identity | None = None
    ) -> dict[str, Any]:
        """Return metadata about a stored file."""
        if user_id:
            await self.ensure_can_access(file_id, user_id, user)
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
