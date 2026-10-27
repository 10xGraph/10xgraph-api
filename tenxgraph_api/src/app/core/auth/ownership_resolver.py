"""Scalable thread-ownership resolution with a two-tier cache.

Thread ownership is *immutable* -- a thread's owner is set when the thread is created and
never changes; only deletion removes it. That makes the owner safe to cache aggressively, so
after the first lookup an authorization check costs nothing (an in-process hit) instead of a
database round-trip per request.

Cache tiers:

- **L1**: a bounded in-process LRU (per worker) with a short TTL.
- **L2** (optional): a shared Redis, so workers/instances do not each re-hit the database.

Deleting a thread evicts it from this worker's L1 and from L2, but another worker's L1 cannot
be reached. A deleted id could be created again by a different user, and a stale L1 entry on
another worker would then grant the old owner access to the new owner's thread. So a delete
also leaves a **tombstone** for one L1 TTL (in L1, and in L2 when Redis is configured), and
:meth:`ThreadOwnershipResolver.recently_deleted` lets the authorization backend refuse to
create a thread under that id until every worker's stale entry has expired. Without Redis the
tombstone only exists on the worker that handled the delete; run multi-worker deployments with
the shared L2 tier. The L2 TTL covers an eviction that failed to reach Redis.

Only *positive* results (a known owner) are cached. A ``None`` result (the thread does not
exist yet) is **never** cached: a nonexistent thread can become owned by the very next
request, and a cached ``None`` would let a later caller be treated as the creator of a thread
someone else just made (a time-of-check/time-of-use hole).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable


logger = logging.getLogger("tenxgraph_api.authorization")

# Sentinel signalling the backing checkpointer cannot resolve ownership at all
# (``aget_thread_owner`` raised ``NotImplementedError``). Propagated so the caller can
# degrade gracefully instead of treating every thread as unowned.
OwnerLookup = Callable[[str], Awaitable["str | int | None"]]

DEFAULT_L1_TTL_SECONDS = 30.0
DEFAULT_L2_TTL_SECONDS = 3600


class ThreadOwnershipResolver:
    """Resolve (and cache) the owning ``user_id`` for a ``thread_id``.

    Args:
        lookup: async ``(thread_id) -> user_id | None`` backing lookup (typically
            ``checkpointer.aget_thread_owner``). May raise ``NotImplementedError`` if the
            checkpointer cannot resolve ownership; that propagates uncached.
        redis: optional async Redis-like client (``get``/``set``/``delete`` coroutines).
            When absent the resolver is L1-only. Redis errors degrade to the DB lookup and
            never fail a request.
        max_size: L1 LRU capacity (entries).
        redis_prefix: key namespace for L2 entries.
        l1_ttl: seconds an L1 entry is trusted; bounds staleness after a delete elsewhere.
        l2_ttl: seconds an L2 entry lives in Redis.
    """

    def __init__(
        self,
        lookup: OwnerLookup,
        *,
        redis: object | None = None,
        max_size: int = 10_000,
        redis_prefix: str = "af:authz:owner",
        l1_ttl: float = DEFAULT_L1_TTL_SECONDS,
        l2_ttl: int = DEFAULT_L2_TTL_SECONDS,
    ) -> None:
        self._lookup = lookup
        self._redis = redis
        self._max_size = max(1, max_size)
        self._prefix = redis_prefix
        self._l1_ttl = l1_ttl
        self._l2_ttl = l2_ttl
        # key -> (owner, monotonic expiry)
        self._l1: OrderedDict[str, tuple[str, float]] = OrderedDict()
        # key -> (monotonic expiry, deleting owner or "") of the tombstone a delete left here
        self._tombstones: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ public

    async def owner_of(self, thread_id: str | int) -> str | None:
        """Return the owning ``user_id`` for ``thread_id``, or None if it has no owner.

        Raises:
            NotImplementedError: if the backing checkpointer cannot resolve ownership.
        """
        key = str(thread_id)

        cached = await self._l1_get(key)
        if cached is not None:
            return cached

        from_redis = await self._redis_get(key)
        if from_redis is not None:
            await self._l1_put(key, from_redis)
            return from_redis

        owner = await self._lookup(key)  # may raise NotImplementedError -> propagate
        if owner is None:
            return None  # never cache negatives (TOCTOU on new threads)

        owner_str = str(owner)
        await self._l1_put(key, owner_str)
        await self._redis_set(key, owner_str)
        return owner_str

    async def evict(self, thread_id: str | int, owner: str | int | None = None) -> None:
        """Drop any cached owner for ``thread_id`` and tombstone it (call on thread delete).

        ``owner`` is the user who deleted it (the owner, since the delete was authorized); that
        user may recreate the id at once, because a stale cache entry can only point at them.
        """
        key = str(thread_id)
        owner_str = "" if owner is None else str(owner)
        async with self._lock:
            cached = self._l1.pop(key, None)
            if not owner_str and cached is not None:
                owner_str = cached[0]
            self._tombstones[key] = (time.monotonic() + self._l1_ttl, owner_str)
            self._tombstones.move_to_end(key)
            while len(self._tombstones) > self._max_size:
                self._tombstones.popitem(last=False)
        await self._redis_delete(key)
        await self._redis_tombstone(key, owner_str)

    async def recently_deleted(self, thread_id: str | int, user_id: str | None = None) -> bool:
        """Whether ``thread_id`` was deleted within the last L1 TTL by someone other than
        ``user_id``.

        Until then another worker may still cache the old owner, so the id must not be handed
        to a new owner yet. The deleting owner is the exception: a stale entry naming them
        grants nothing they do not already own. A tombstone with no recorded owner blocks
        everyone.
        """
        key = str(thread_id)
        async with self._lock:
            entry = self._tombstones.get(key)
            if entry is not None:
                expires_at, owner = entry
                if time.monotonic() < expires_at:
                    return not (owner and user_id is not None and owner == str(user_id))
                del self._tombstones[key]
        shared_owner = await self._redis_tombstone_owner(key)
        if shared_owner is None:
            return False
        return not (shared_owner and user_id is not None and shared_owner == str(user_id))

    # ------------------------------------------------------------------ L1

    async def _l1_get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._l1.get(key)
            if entry is None:
                return None
            owner, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._l1[key]
                return None
            self._l1.move_to_end(key)  # mark most-recently-used
            return owner

    async def _l1_put(self, key: str, owner: str) -> None:
        async with self._lock:
            self._l1[key] = (owner, time.monotonic() + self._l1_ttl)
            self._l1.move_to_end(key)
            while len(self._l1) > self._max_size:
                self._l1.popitem(last=False)  # evict least-recently-used

    # ------------------------------------------------------------------ L2 (Redis)

    # Owner entries and delete markers live under different fixed segments, so no thread id
    # (not even one like "deleted:X") can make an owner key collide with a marker key.
    def _redis_key(self, key: str) -> str:
        return f"{self._prefix}:id:{key}"

    async def _redis_get(self, key: str) -> str | None:
        if self._redis is None:
            return None
        try:
            value = await self._redis.get(self._redis_key(key))  # type: ignore[attr-defined]
        except Exception as exc:  # degrade to DB; never fail the request
            logger.debug("Ownership L2 get failed for %s: %s", key, exc)
            return None
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes | bytearray) else str(value)

    async def _redis_set(self, key: str, owner: str) -> None:
        if self._redis is None:
            return
        try:
            # evict() removes the entry on delete; the TTL covers an eviction that failed.
            await self._redis.set(  # type: ignore[attr-defined]
                self._redis_key(key), owner, ex=self._l2_ttl
            )
        except Exception as exc:
            logger.debug("Ownership L2 set failed for %s: %s", key, exc)

    def _tombstone_key(self, key: str) -> str:
        return f"{self._prefix}:tomb:{key}"

    async def _redis_tombstone(self, key: str, owner: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(  # type: ignore[attr-defined]
                self._tombstone_key(key),
                owner.encode() or b"-",
                ex=max(1, int(self._l1_ttl + 0.999)),
            )
        except Exception as exc:
            logger.debug("Ownership L2 tombstone failed for %s: %s", key, exc)

    async def _redis_tombstone_owner(self, key: str) -> str | None:
        """The deleting owner in a shared tombstone ("" when unknown), or None when none."""
        if self._redis is None:
            return None
        try:
            value = await self._redis.get(self._tombstone_key(key))  # type: ignore[attr-defined]
        except Exception as exc:  # degrade to "not deleted"; L1 already answered for us
            logger.debug("Ownership L2 tombstone read failed for %s: %s", key, exc)
            return None
        if value is None:
            return None
        text = value.decode() if isinstance(value, bytes | bytearray) else str(value)
        return "" if text == "-" else text

    async def _redis_delete(self, key: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._redis_key(key))  # type: ignore[attr-defined]
        except Exception as exc:
            logger.debug("Ownership L2 delete failed for %s: %s", key, exc)
