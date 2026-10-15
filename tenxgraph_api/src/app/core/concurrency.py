"""Concurrency caps that can be shared by every worker.

The per-user caps on HTTP graph runs (``runs.max_concurrent_per_user``) and on WebSocket
connections (``websocket.max_connections`` / ``max_connections_per_user``) count holders of a
slot. In one process a dict does that. With several workers each process would count only its
own holders, so the real cap would be ``limit x workers``.

:class:`RedisConcurrencyLimiter` keeps the holders in Redis instead, as a sorted set of leases
per key (member = lease id, score = expiry time). A held lease is refreshed in the background
and released on exit; if a worker dies its leases expire on their own, so a crash cannot leak
slots. The loader binds it whenever Redis is configured (the ``redis`` key in 10xgraph.json or
``REDIS_URL``), like the ownership cache.

Acquiring runs one transaction (drop expired leases, add ours, count, set a key TTL) and backs
out if the count is over the limit. Two callers racing for the last slot may both back out,
but the cap is never exceeded. A Redis error fails open: the request is allowed and the error
logged, because refusing every request while Redis is down would be an outage of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any


logger = logging.getLogger("tenxgraph_api.concurrency")


# Release coroutines scheduled from sync code, referenced until they finish.
_pending_releases: set[asyncio.Task[Any]] = set()


class Lease:
    """A held slot. ``release`` is idempotent and safe to call from sync code."""

    def __init__(self, release: Any = None) -> None:
        self._release = release
        self._released = release is None

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        result = self._release()
        if asyncio.iscoroutine(result):
            try:
                task = asyncio.get_running_loop().create_task(result)
            except RuntimeError:  # no loop (garbage collection at shutdown): let it expire
                result.close()
                return
            _pending_releases.add(task)  # keep a reference until it finishes
            task.add_done_callback(_pending_releases.discard)


class ConcurrencyLimiter(ABC):
    @abstractmethod
    async def acquire(self, key: str, limit: int) -> Lease | None:
        """Take one of ``limit`` slots for ``key``; ``None`` when all are taken."""

    def held(self, key: str) -> int | None:
        """Slots held for ``key`` when cheaply known (in-process); otherwise None."""
        return None

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release resources (called on app shutdown)."""


class MemoryConcurrencyLimiter(ConcurrencyLimiter):
    """Per-process slot counts. The default, and correct for a single worker.

    The event loop is single-threaded and there is no ``await`` between the check and the
    increment, so acquiring needs no lock.
    """

    def __init__(self) -> None:
        self._held: dict[str, int] = {}

    def held(self, key: str) -> int:
        return self._held.get(key, 0)

    async def acquire(self, key: str, limit: int) -> Lease | None:
        held = self._held.get(key, 0)
        if held >= limit:
            return None
        self._held[key] = held + 1
        return Lease(lambda: self._release(key))

    def _release(self, key: str) -> None:
        held = self._held.get(key, 0)
        if held <= 1:
            self._held.pop(key, None)
        else:
            self._held[key] = held - 1


class RedisConcurrencyLimiter(ConcurrencyLimiter):
    """Slot counts shared through Redis, with expiring leases (see the module docstring)."""

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = "10xgraph:concurrency",
        lease_ttl: float = 60.0,
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._lease_ttl = lease_ttl
        self._refreshers: set[asyncio.Task[None]] = set()

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisConcurrencyLimiter:
        from redis.asyncio import Redis  # type: ignore[import]

        return cls(Redis.from_url(url, decode_responses=False), **kwargs)

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def acquire(self, key: str, limit: int) -> Lease | None:
        redis_key, member = self._key(key), uuid.uuid4().hex
        now = time.time()
        try:
            pipe = self._client.pipeline(transaction=True)
            pipe.zremrangebyscore(redis_key, "-inf", now)
            pipe.zadd(redis_key, {member: now + self._lease_ttl})
            pipe.zcard(redis_key)
            pipe.expire(redis_key, int(self._lease_ttl) + 1)
            results = await pipe.execute()
            if int(results[2]) > limit:
                await self._client.zrem(redis_key, member)
                return None
        except Exception as exc:
            logger.warning("Shared concurrency limit unavailable (%s); allowing %s", exc, key)
            return Lease(None)

        refresher = asyncio.create_task(self._refresh(redis_key, member))
        self._refreshers.add(refresher)
        refresher.add_done_callback(self._refreshers.discard)

        async def release() -> None:
            refresher.cancel()
            try:
                await self._client.zrem(redis_key, member)
            except Exception as exc:  # the lease expires on its own
                logger.debug("Lease release failed for %s: %s", key, exc)

        return Lease(release)

    async def _refresh(self, redis_key: str, member: str) -> None:
        """Keep a held lease alive; it expires ``lease_ttl`` after the last refresh."""
        interval = max(self._lease_ttl / 3, 0.01)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._client.zadd(redis_key, {member: time.time() + self._lease_ttl}, xx=True)
                await self._client.expire(redis_key, int(self._lease_ttl) + 1)
            except Exception as exc:
                logger.debug("Lease refresh failed for %s: %s", redis_key, exc)

    async def aclose(self) -> None:
        for task in list(self._refreshers):
            task.cancel()
        close = getattr(self._client, "aclose", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await close()


# Used when no shared limiter is bound: one per process.
_process_limiter = MemoryConcurrencyLimiter()


def shared_concurrency_limiter() -> ConcurrencyLimiter | None:
    """The limiter bound in the app's container (Redis-backed when configured), or None."""
    try:
        from tenxgraph_api.src.app.core.utils.container import current_container

        bound = current_container().try_get(ConcurrencyLimiter)
    except Exception:
        return None
    return bound if isinstance(bound, ConcurrencyLimiter) else None


def concurrency_limiter() -> ConcurrencyLimiter:
    """The shared limiter bound in the app's container, else this process's own."""
    return shared_concurrency_limiter() or _process_limiter
