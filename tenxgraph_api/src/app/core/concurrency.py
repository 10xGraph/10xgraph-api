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
but the cap is never exceeded. Expiry times use the Redis clock (``TIME``), not each pod's
own, so clock skew between pods cannot trim a live lease. A Redis error fails open to this
process's own count: the error is logged and the slot is taken from an in-process counter,
because refusing every request while Redis is down would be an outage of its own. The cap then
holds per process, not across workers, until Redis is back (so the per-thread run lock still
stops a double run in one worker).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any

from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe


logger = logging.getLogger("tenxgraph_api.concurrency")


# Release coroutines scheduled from sync code, referenced until they finish.
_pending_releases: set[asyncio.Task[Any]] = set()

# Longest ``aclose`` waits for those releases before it closes the client anyway.
SHUTDOWN_RELEASE_WAIT_SECONDS = 5.0


class Lease:
    """A held slot. ``release`` is idempotent and safe to call from sync code."""

    def __init__(self, release: Any = None) -> None:
        self._release = release
        self._released = release is None
        # Set when a shared lease was found missing on refresh (expired or evicted while held).
        self.lost = False

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
        # Counts used while Redis is unreachable, so one process still honours the cap.
        self._local = MemoryConcurrencyLimiter()

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisConcurrencyLimiter:
        from tenxgraph_api.src.app.core.redis_client import create_redis

        return cls(create_redis(url), **kwargs)

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def _now(self) -> float:
        """The Redis server's clock. Lease expiry is compared across pods, so a pod's own
        clock (skewed by more than the lease ttl) would trim another pod's live lease."""
        server_time = getattr(self._client, "time", None)
        if not callable(server_time):
            return time.time()
        seconds, microseconds = await server_time()
        return float(seconds) + float(microseconds) / 1_000_000

    async def acquire(self, key: str, limit: int) -> Lease | None:
        redis_key, member = self._key(key), uuid.uuid4().hex
        try:
            now = await self._now()
            pipe = self._client.pipeline(transaction=True)
            pipe.zremrangebyscore(redis_key, "-inf", now)
            pipe.zadd(redis_key, {member: now + self._lease_ttl})
            pipe.zcard(redis_key)
            pipe.expire(redis_key, int(self._lease_ttl) + 1)
            results = await pipe.execute()
            if int(results[2]) > limit:
                await self._client.zrem(redis_key, member)
                return None
        except asyncio.CancelledError:
            # The caller gave up (a client left while waiting). The transaction may already
            # have added our member; take it out now rather than leave it for the lease ttl.
            self._discard_later(redis_key, member)
            raise
        except Exception as exc:
            logger.warning(
                "Shared concurrency limit unavailable (%s); counting %s in this process",
                exc,
                log_safe(key),
            )
            return await self._local.acquire(key, limit)

        lease = Lease(lambda: release())  # release is defined below, it needs the refresher
        refresher = asyncio.create_task(self._refresh(redis_key, member, lease))
        self._refreshers.add(refresher)
        refresher.add_done_callback(self._refreshers.discard)

        async def release() -> None:
            refresher.cancel()
            try:
                await self._client.zrem(redis_key, member)
            except Exception as exc:  # the lease expires on its own
                logger.debug("Lease release failed for %s: %s", log_safe(key), exc)

        return lease

    def _discard_later(self, redis_key: str, member: str) -> None:
        """Remove ``member`` on a task of its own, for callers that are being cancelled."""

        async def discard() -> None:
            try:
                await self._client.zrem(redis_key, member)
            except Exception as exc:  # the lease expires on its own
                logger.debug("Discarding a lease failed for %s: %s", log_safe(redis_key), exc)

        task = asyncio.get_running_loop().create_task(discard())
        _pending_releases.add(task)
        task.add_done_callback(_pending_releases.discard)

    async def _refresh(self, redis_key: str, member: str, lease: Lease) -> None:
        """Keep a held lease alive; it expires ``lease_ttl`` after the last refresh."""
        interval = max(self._lease_ttl / 3, 0.01)
        while True:
            await asyncio.sleep(interval)
            try:
                expiry = await self._now() + self._lease_ttl
                # ``xx`` never adds, and without ``ch`` Redis reports 0 for a refreshed member
                # as well as for a missing one, so the reply could not tell them apart.
                changed = await self._client.zadd(redis_key, {member: expiry}, xx=True, ch=True)
                if not changed:
                    lease.lost = True
                    logger.warning(
                        "Lease %s expired or was evicted while held; taking it again", redis_key
                    )
                    await self._client.zadd(redis_key, {member: expiry})
                await self._client.expire(redis_key, int(self._lease_ttl) + 1)
            except Exception as exc:
                logger.warning("Lease refresh failed for %s: %s", log_safe(redis_key), exc)

    async def aclose(self) -> None:
        for task in list(self._refreshers):
            task.cancel()
        # Releases scheduled from sync code run on tasks of their own; the client has to stay
        # open until they have, or the leases stay until their ttl (a 409 on the next pod).
        while _pending_releases:
            pending = set(_pending_releases)
            _, still = await asyncio.wait(pending, timeout=SHUTDOWN_RELEASE_WAIT_SECONDS)
            if still:
                logger.warning("%d lease releases did not finish before shutdown", len(still))
                break
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
