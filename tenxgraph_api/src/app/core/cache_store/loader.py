"""Pick the API's cache store from 10xgraph.json and the environment, and bind it.

Selection order (the first one set wins):

1. ``"cache_store": "module:attr"`` in 10xgraph.json: a ``BaseCacheStore`` instance, or a
   zero-argument factory (a class or function) that returns one.
2. ``"redis"`` in 10xgraph.json: ``RedisCacheStore.from_url(...)``.
3. ``REDIS_URL`` in the environment: ``RedisCacheStore.from_url(...)``.
4. Otherwise ``InMemoryCacheStore()``: correct for one process, not shared between workers.

The store is bound in the InjectQ container as ``BaseCacheStore`` together with the
``LeaseManager`` that the run locks, run and WebSocket caps and idempotency use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .base import BaseCacheStore
from .memory_store import InMemoryCacheStore
from .redis_store import RedisCacheStore


if TYPE_CHECKING:
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


logger = logging.getLogger("tenxgraph_api.cache_store")


def build_cache_store(
    graph_config: GraphConfig | None, settings: Any
) -> tuple[BaseCacheStore, str]:
    """The configured cache store and where it came from (for logs; never credentials).

    ``graph_config`` may be None (no 10xgraph.json). ``settings`` supplies ``REDIS_URL``.
    """
    path = graph_config.cache_store if graph_config is not None else None
    if path:
        from tenxgraph_api.src.app.loader import load_cache_store

        return load_cache_store(path), f"from cache_store {path}"

    redis_url = graph_config.redis_url if graph_config is not None else None
    source = "redis in 10xgraph.json"
    if not redis_url:
        redis_url = getattr(settings, "REDIS_URL", None)
        source = "REDIS_URL"
    if redis_url:
        try:
            return RedisCacheStore.from_url(redis_url), f"from {source}"
        except ImportError:
            logger.warning(
                "%s is set but the 'redis' package is not installed; the cache store is "
                "in-memory (per process). Install the redis extra to share it across workers.",
                source,
            )
            return InMemoryCacheStore(), f"{source} set but the redis package is missing"

    return InMemoryCacheStore(), "no cache_store, redis or REDIS_URL set"


@dataclass(frozen=True)
class CacheStoreSource:
    """Where the bound cache store came from (``from REDIS_URL``), for the startup check."""

    text: str | None = None


def describe_cache_store(store: BaseCacheStore, source: str | None = None) -> str:
    """``RedisCacheStore (from REDIS_URL, localhost:6379)``: the store, its source, endpoint.

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
    container: InjectQ, graph_config: GraphConfig | None, settings: Any
) -> BaseCacheStore:
    """Build the cache store (see the module docstring), bind it and log one INFO line."""
    store, source = build_cache_store(graph_config, settings)
    bind_cache_store(container, store, source)
    logger.info("Cache store: %s", describe_cache_store(store, source))
    return store


def is_in_memory(store: BaseCacheStore | None) -> bool:
    """Whether ``store`` keeps its state in this process (or no store is bound)."""
    return store is None or isinstance(store, InMemoryCacheStore)
