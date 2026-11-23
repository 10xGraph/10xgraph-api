"""Extraction text and signed-URL caches for the media service."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe


if TYPE_CHECKING:
    from tenxgraph.storage.checkpointer import BaseCheckpointer

    from tenxgraph_api.src.app.core.config.media_settings import MediaSettings


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


class _CachingMixin:
    """Extraction text and signed-URL caches of ``MediaService``."""

    _settings: MediaSettings
    _extraction_cache: _BoundedCache
    _signed_url_cache: _BoundedCache

    if TYPE_CHECKING:

        @property
        def checkpointer(self) -> BaseCheckpointer | None: ...

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
