"""Thread links and deletion for the media service."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, MediaConfig
from tenxgraph_api.src.app.core.utils.container import lookup
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.routers.media.ownership import THREADS_KEY


if TYPE_CHECKING:
    from tenxgraph.storage.media.storage.base import BaseMediaStore


logger = logging.getLogger("tenxgraph_api.media")


class _LinksMixin:
    """Thread links and deletion of ``MediaService``."""

    if TYPE_CHECKING:

        @property
        def store(self) -> BaseMediaStore: ...

        def caller_tenant(self, user: Identity | None) -> dict[str, str | None]: ...

        @staticmethod
        def _owned_by(
            metadata: dict[str, Any], user_id: Any, tenant: dict[str, str | None]
        ) -> bool: ...

        def _in_reach(self, metadata: dict[str, Any], user: Identity) -> bool: ...

        async def delete_file(self, file_id: str) -> bool: ...

    def delete_with_thread(self) -> bool:
        """``media.delete_with_thread`` from 10xgraph.json (default true)."""
        media = getattr(lookup(GraphConfig), "media", None)
        return bool(media.delete_with_thread) if isinstance(media, MediaConfig) else True

    async def _keys_of(self, user_id: str | None) -> list[str]:
        """Keys worth inspecting for ``user_id``; every key when there is no user.

        Uses the store's per-owner listing when it has one (the cloud store's owner index),
        else lists all keys. Callers still check each file's recorded owner.

        Raises:
            NotImplementedError: The store cannot enumerate the files.
        """
        if user_id:
            return list(await self.store.list_keys_for_owner(str(user_id)))
        return list(await self.store.list_keys())

    async def _modify_metadata(
        self, key: str, fn: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> bool:
        """Change one file's metadata from its current value as a single step.

        Uses the store's ``modify_metadata`` (atomic in the bundled stores). ``fn`` may run
        more than once, so it must only compute.
        """
        return bool(await self.store.modify_metadata(key, fn))

    async def link_threads(
        self, keys: list[str], thread_id: str, user_id: str, user: Identity | None = None
    ) -> None:
        """Record ``thread_id`` on each file the caller owns that an accepted message references.

        Owning means the same ``user_id`` and, with tenant columns declared, the same tenant
        values (``user``). Never raises: a failure only means the file is not tracked, so it
        is never auto-deleted. Files without a ``threads`` list (uploaded before link
        tracking) are left alone.
        """
        if not keys:
            return
        tenant = self.caller_tenant(user)
        for key in dict.fromkeys(keys):
            try:

                def add_link(metadata: dict[str, Any]) -> dict[str, Any] | None:
                    threads = metadata.get(THREADS_KEY)
                    if (
                        not self._owned_by(metadata, user_id, tenant)
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
        if not keys:
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

    async def release_thread(
        self,
        thread_id: str,
        owner_id: str | None,
        user: Identity | None = None,
    ) -> list[str]:
        """Release every file ``owner_id`` linked to ``thread_id``, whatever the messages say.

        A thread delete calls this after storage deleted the thread, with the owner from the
        caller's scoped thread read. Only files of that owner within the deleter's reach
        (``user``, see :meth:`_in_reach`) are released; with no ``user`` (auth off) every file
        of the owner linked to the thread is.

        ``unlink_threads`` works from the keys a thread's messages reference. A file linked
        through a message that is gone since (cleared state, replaced messages) would stay
        linked to a deleted thread forever; this finds those by scanning the store's metadata
        (needs ``list_keys()`` or ``list_keys_for_owner()``, so stores that cannot
        enumerate are skipped). Same rules as ``unlink_threads``: a file left with no thread is
        deleted only with ``media.delete_with_thread`` on. Never raises; returns the keys that
        could not be released.
        """
        if owner_id is None:
            return []
        try:
            all_keys = await self._keys_of(owner_id)
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
                metadata is not None
                and isinstance(threads, list)
                and thread_id in threads
                and str(metadata.get("owner_id")) == str(owner_id)
                and (user is None or self._in_reach(metadata, user))
            ):
                linked.append(key)
        failed.extend(await self.unlink_threads(linked, thread_id))
        return failed
