"""Shared state for the API's workers: one ``BaseCacheStore`` (see ``base.py``)."""

from .base import BaseCacheStore, SlotResult
from .memory_store import InMemoryCacheStore
from .redis_store import RedisCacheStore


__all__ = [
    "BaseCacheStore",
    "InMemoryCacheStore",
    "RedisCacheStore",
    "SlotResult",
]
