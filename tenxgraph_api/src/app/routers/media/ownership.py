"""Tenant and owner metadata helpers for stored media."""

from __future__ import annotations

import time
from typing import Any

from tenxgraph.storage.media.storage.base import BaseMediaStore

from tenxgraph_api.src.app.core.validation.request_config import ClientInputError


# Metadata key listing the threads whose messages reference a file. Written empty at upload
# (so a file without the key is known to predate link tracking and is never auto-deleted).
THREADS_KEY = "threads"
# Metadata key holding the uploader's tenant values: ``{column: canonical text}`` for every
# tenant column the bound checkpointer declares (``PgCheckpointer(tenant_columns=...)``).
# Written only when tenant columns are declared; the values come from ``Identity.tenant``.
TENANT_KEY = "tenant"


def tenant_matches(metadata: dict[str, Any], tenant: dict[str, str | None]) -> bool:
    """Whether a file's recorded tenant values equal ``tenant`` on every column.

    ``tenant`` is the caller's values from :meth:`MediaService.caller_tenant`: empty when no
    tenant columns are declared (always a match). Fails closed: a caller missing a value, or
    a file with no recorded value for a column (uploaded before tenant tracking), never
    matches.
    """
    if not tenant:
        return True
    recorded = metadata.get(TENANT_KEY)
    if not isinstance(recorded, dict):
        return False
    for column, value in tenant.items():
        stored = recorded.get(column)
        if value is None or stored is None or str(stored) != value:
            return False
    return True


class MissingTenantError(PermissionError):
    """The authenticated user has no value for a declared tenant column."""


class _OwnedStore:
    """Wraps a media store so offloaded blobs get the same metadata as an upload."""

    def __init__(
        self,
        store: BaseMediaStore,
        user_id: str | None,
        tenant: dict[str, str | None] | None = None,
    ):
        self._store = store
        self._user_id = user_id
        self._tenant = tenant or {}

    async def store(self, data: bytes, mime_type: str, metadata: dict[str, Any] | None = None):
        meta: dict[str, Any] = {"created_at": int(time.time()), **(metadata or {})}
        if self._user_id:
            if any(value is None for value in self._tenant.values()):
                # Stored unscoped, no one in the tenant could read it back: refuse instead.
                raise ClientInputError("the authenticated user has no tenant value")
            meta["owner_id"] = str(self._user_id)
            if self._tenant:
                meta[TENANT_KEY] = dict(self._tenant)
            meta[THREADS_KEY] = []
        return await self._store.store(data, mime_type, metadata=meta)

    def to_media_ref(self, *args: Any, **kwargs: Any):
        return self._store.to_media_ref(*args, **kwargs)
