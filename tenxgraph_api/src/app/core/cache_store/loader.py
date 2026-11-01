"""Pick the API's cache store from ``cache_store`` in 10xgraph.json, and bind it.

``cache_store.type`` decides, and nothing else (no environment variable picks a store):

- ``custom``: ``cache_store.path`` (``"module:attr"``), a ``BaseCacheStore`` instance or a
  zero-argument factory (a class or function) that returns one.
- ``redis``: ``RedisCacheStore.from_url(cache_store.url)``. Without the ``redis`` package the
  boot stops: the config asks for Redis, so a per-process store would be a silent change.
- ``memory``, or no ``cache_store`` / no ``type``: ``InMemoryCacheStore()``, correct for one
  process, not shared between workers.

The store is bound in the InjectQ container as ``BaseCacheStore`` together with the
``LeaseManager`` that the run locks, run and WebSocket caps and idempotency use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import BaseCacheStore
from .memory_store import InMemoryCacheStore
from .redis_store import RedisCacheStore


if TYPE_CHECKING:
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


logger = logging.getLogger("tenxgraph_api.cache_store")


def build_cache_store(graph_config: GraphConfig | None) -> tuple[BaseCacheStore, str]:
    """The configured cache store and the config key it came from (for logs; no credentials).

    ``graph_config`` may be None (no 10xgraph.json): then the in-memory store.
    """
    settings = graph_config.cache_store if graph_config is not None else None
    if settings is None or settings.type == "memory":
        source = "cache_store.type" if settings is not None and settings.type_set else None
        return InMemoryCacheStore(), source or "cache_store not set"

    if settings.type == "custom":
        from tenxgraph_api.src.app.loader import load_cache_store

        path = str(settings.path)
        return load_cache_store(path), f"cache_store.path {path}"

    url = settings.expanded_url()
    try:
        return RedisCacheStore.from_url(str(url)), settings.url_source
    except ImportError:
        raise RuntimeError(
            'cache_store.type is "redis" but the redis package is not installed. Install the '
            'redis extra: pip install "10xgraph-api[redis]"'
        ) from None


@dataclass(frozen=True)
class CacheStoreSource:
    """The config key the bound cache store came from (``cache_store.url``), for the startup
    check."""

    text: str | None = None


def describe_cache_store(store: BaseCacheStore, source: str | None = None) -> str:
    """``RedisCacheStore (cache_store.url, localhost:6379)``: the store, its source, endpoint.

    The endpoint is the store's ``describe()``; without one (or without a source) only what is
    known is named, at least the class.
    """
    try:
        where = store.describe()
    except Exception:  # a custom store's describe must not break boot
        where = None
    detail = ", ".join(part for part in (source, where) if part)
    name = type(store).__name__
    return f"{name} ({detail})" if detail else name


def bind_cache_store(container: InjectQ, store: BaseCacheStore, source: str | None = None) -> None:
    """Bind ``store`` as ``BaseCacheStore``, its ``CacheStoreSource`` and a ``LeaseManager``."""
    from tenxgraph_api.src.app.core.leases import LeaseManager

    container.bind_instance(BaseCacheStore, store)
    container.bind_instance(CacheStoreSource, CacheStoreSource(source))
    container.bind_instance(LeaseManager, LeaseManager(store))


def load_and_bind_cache_store(
    container: InjectQ, graph_config: GraphConfig | None
) -> BaseCacheStore:
    """Build the cache store (see the module docstring), bind it and log one INFO line."""
    store, source = build_cache_store(graph_config)
    bind_cache_store(container, store, source)
    logger.info("Cache store: %s", describe_cache_store(store, source))
    return store


def is_in_memory(store: BaseCacheStore | None) -> bool:
    """Whether ``store`` keeps its state in this process (or no store is bound)."""
    return store is None or isinstance(store, InMemoryCacheStore)
