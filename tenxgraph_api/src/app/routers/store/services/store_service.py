from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from injectq import inject, singleton
from tenxgraph.core.state import Message
from tenxgraph.storage.store import BaseStore

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.request_config import client_config
from tenxgraph_api.src.app.routers.store.schemas.store_schemas import (
    ForgetMemorySchema,
    MemoryCreateResponseSchema,
    MemoryItemResponseSchema,
    MemoryListResponseSchema,
    MemoryOperationResponseSchema,
    MemorySearchResponseSchema,
    SearchMemorySchema,
    StoreMemorySchema,
    UpdateMemorySchema,
)


class StoreUnavailableError(HTTPException, ValueError):
    """Raised when the store service has not been configured."""

    def __init__(self) -> None:
        detail = "Store is not configured"
        HTTPException.__init__(self, status_code=503, detail=detail)
        ValueError.__init__(self, detail)


# Memory fields the store owns. Client metadata is kept, but these keys are dropped from it:
# user_id and thread_id decide who sees a memory, and memory_id is the id Mem0 reports back.
SYSTEM_METADATA_KEYS = frozenset(
    {"content", "user_id", "thread_id", "memory_type", "category", "timestamp", "memory_id"}
)

# Store keyword options a client may set. The built-in stores read only these (``infer`` in
# Mem0Store.astore, ``replace_metadata`` in QdrantStore.aupdate). Anything else is dropped so a
# client cannot pass arbitrary keyword arguments to the store; ``memory_id`` is never allowed
# because ``astore`` upserts, so a client-chosen id could replace an existing memory.
CLIENT_OPTION_KEYS = frozenset({"infer", "replace_metadata"})


def _client_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if metadata is None:
        return None
    return {k: v for k, v in metadata.items() if k not in SYSTEM_METADATA_KEYS}


# Keys that choose whose memories a store call touches. The server sets them from the
# authenticated user, so a client ``filters`` dict may never carry them (J-4).
RESERVED_FILTER_KEYS = frozenset({"user_id", "agent_id", "app_id", "run_id"})
_LOGICAL_KEYS = frozenset({"AND", "OR", "NOT"})


def _client_filters(filters: Any) -> Any:
    """Copy of ``filters`` without reserved owner keys, also inside AND / OR / NOT groups."""
    if not isinstance(filters, dict):
        return filters
    clean: dict[str, Any] = {}
    for key, value in filters.items():
        if key in RESERVED_FILTER_KEYS:
            continue
        if key in _LOGICAL_KEYS and isinstance(value, list):
            groups = [g for g in (_client_filters(v) for v in value) if g not in ({}, None)]
            if groups:
                clean[key] = groups
            continue
        clean[key] = value
    return clean


def _client_options(options: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (options or {}).items() if k in CLIENT_OPTION_KEYS}


@singleton
class StoreService:
    """Service layer wrapping interactions with the configured BaseStore."""

    @inject
    def __init__(self, store: BaseStore | None):
        self.store = store

    def _get_store(self) -> BaseStore:
        if not self.store:
            raise StoreUnavailableError()
        return self.store

    def _config(self, config: dict[str, Any] | None, user: dict[str, Any]) -> dict[str, Any]:
        # Client keys pass through; server-owned keys (authz, user, collection, internals)
        # never do. The trusted user object carries the authz policy the store enforces.
        cfg: dict[str, Any] = client_config(config)
        cfg["user"] = user
        cfg["user_id"] = user.get("user_id", "anonymous")
        return cfg

    async def store_memory(
        self,
        payload: StoreMemorySchema,
        user: dict[str, Any],
    ) -> MemoryCreateResponseSchema:
        store = self._get_store()
        cfg = self._config(payload.config, user)
        options = _client_options(payload.options)

        if isinstance(payload.content, Message):
            content: str | Message = payload.content
        else:
            content = payload.content

        memory_id = await store.astore(
            cfg,
            content,
            memory_type=payload.memory_type,
            category=payload.category,
            metadata=_client_metadata(payload.metadata),
            **options,
        )
        logger.debug("Stored memory with id %s", memory_id)
        return MemoryCreateResponseSchema(memory_id=memory_id)

    async def search_memories(
        self,
        payload: SearchMemorySchema,
        user: dict[str, Any],
    ) -> MemorySearchResponseSchema:
        store = self._get_store()
        cfg = self._config(payload.config, user)
        options = _client_options(payload.options)

        results = await store.asearch(
            cfg,
            payload.query,
            memory_type=payload.memory_type,
            category=payload.category,
            limit=payload.limit,
            score_threshold=payload.score_threshold,
            filters=_client_filters(payload.filters),
            retrieval_strategy=payload.retrieval_strategy,
            distance_metric=payload.distance_metric,
            max_tokens=payload.max_tokens,
            **options,
        )
        return MemorySearchResponseSchema(results=results)

    async def get_memory(
        self,
        memory_id: str,
        config: dict[str, Any] | None,
        user: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> MemoryItemResponseSchema:
        store = self._get_store()
        cfg = self._config(config, user)
        result = await store.aget(cfg, memory_id, **_client_options(options))
        return MemoryItemResponseSchema(memory=result)

    async def list_memories(
        self,
        config: dict[str, Any] | None,
        user: dict[str, Any],
        limit: int = 100,
        options: dict[str, Any] | None = None,
        offset: int = 0,
    ) -> MemoryListResponseSchema:
        """One page of memories. The store has no offset, so it is asked for ``offset + limit
        + 1`` rows and the page is cut from those; the extra row only tells ``has_more``."""
        store = self._get_store()
        cfg = self._config(config, user)
        rows = await store.aget_all(cfg, limit=offset + limit + 1, **_client_options(options))
        page = list(rows[offset : offset + limit])
        has_more = len(rows) > offset + limit
        return MemoryListResponseSchema(
            memories=page,
            has_more=has_more,
            next_offset=offset + limit if has_more else None,
        )

    async def update_memory(
        self,
        memory_id: str,
        payload: UpdateMemorySchema,
        user: dict[str, Any],
    ) -> MemoryOperationResponseSchema:
        store = self._get_store()
        cfg = self._config(payload.config, user)
        options = _client_options(payload.options)

        result = await store.aupdate(
            cfg,
            memory_id,
            payload.content,
            metadata=_client_metadata(payload.metadata),
            **options,
        )
        return MemoryOperationResponseSchema(success=True, data=result)

    async def delete_memory(
        self,
        memory_id: str,
        config: dict[str, Any] | None,
        user: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> MemoryOperationResponseSchema:
        store = self._get_store()
        cfg = self._config(config, user)
        result = await store.adelete(cfg, memory_id, **_client_options(options))
        return MemoryOperationResponseSchema(success=True, data=result)

    async def forget_memory(
        self,
        payload: ForgetMemorySchema,
        user: dict[str, Any],
    ) -> MemoryOperationResponseSchema:
        store = self._get_store()
        cfg = self._config(payload.config, user)
        options = _client_options(payload.options)
        forget_kwargs: dict[str, Any] = {
            "memory_type": payload.memory_type,
            "category": payload.category,
            "filters": _client_filters(payload.filters),
        }
        # Remove None values before forwarding to the store
        forget_kwargs = {k: v for k, v in forget_kwargs.items() if v is not None}
        forget_kwargs.update(options)
        result = await store.aforget_memory(cfg, **forget_kwargs)
        return MemoryOperationResponseSchema(success=True, data=result)
