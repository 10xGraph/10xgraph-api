"""Scalable thread-ownership resolution with a two-tier cache.

Thread ownership is *immutable* -- a thread's owner is set when the thread is created and
never changes; only deletion removes it. That makes the owner safe to cache aggressively, so
after the first lookup an authorization check costs nothing (an in-process hit) instead of a
database round-trip per request.

What is cached is the thread's **scope**: ``{"user_id": ..., <tenant column>: ...}`` from
``BaseCheckpointer.aget_thread_scope`` (``PgCheckpointer(tenant_columns=...)`` adds the tenant
values; every other checkpointer answers ``{"user_id": owner}``). Values are kept as canonical
strings (:func:`canonical_value`) so they survive the shared cache and compare the same way
whatever type the database returned. The tenant values are set at thread creation and never
change either, so the same caching rules hold.

Cache tiers:

- **L1**: a bounded in-process LRU (per worker) with a short TTL.
- **L2** (optional): the API's shared cache store (``BaseCacheStore``, Redis by default), so
  workers/instances do not each re-hit the database. With an in-memory cache store there is
  no L2: it would only cache the same thing twice in this process.

Deleting a thread evicts it from this worker's L1 and from L2, but another worker's L1 cannot
be reached. A deleted id could be created again by a different user, and a stale L1 entry on
another worker would then grant the old owner access to the new owner's thread. So a delete
also leaves a **tombstone** for one L1 TTL (in L1, and in L2 when a shared cache store is
configured), and :meth:`ThreadOwnershipResolver.recently_deleted` lets the authorization
backend refuse to create a thread under that id until every worker's stale entry has expired.
Without a shared cache store the tombstone only exists on the worker that handled the delete;
run multi-worker deployments with the shared L2 tier. The L2 TTL covers an eviction that
failed to reach the cache store.

Only *positive* results (a known thread) are cached. A ``None`` result (the thread does not
exist yet) is **never** cached: a nonexistent thread can become owned by the very next
request, and a cached ``None`` would let a later caller be treated as the creator of a thread
someone else just made (a time-of-check/time-of-use hole).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from tenxgraph_api.src.app.core.cache_store import BaseCacheStore
from tenxgraph_api.src.app.core.cache_store.loader import is_in_memory
from tenxgraph_api.src.app.core.redis_client import key_part


logger = logging.getLogger("tenxgraph_api.authorization")

# Backing lookup: ``(thread_id) -> owner | scope dict | None``. A scope dict is what
# ``aget_thread_scope`` returns; a bare owner is what ``aget_thread_owner`` returns (read as
# ``{"user_id": owner}``). It may raise ``NotImplementedError`` when the checkpointer cannot
# resolve ownership at all; that propagates so the caller can fail closed.
OwnerLookup = Callable[[str], Awaitable[Any]]
ThreadScope = dict[str, str | None]

DEFAULT_L1_TTL_SECONDS = 30.0
DEFAULT_L2_TTL_SECONDS = 3600


def canonical_value(value: Any) -> str | None:
    """The text a scope value is cached and compared as (``None`` stays ``None``).

    Datetimes are compared in UTC, booleans as ``true``/``false``, everything else (text,
    integers, UUIDs) by ``str()``. Both sides of a comparison go through this, after the
    caller's value was converted with the column's declared type.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.isoformat()
    return str(value)


def to_scope(result: Any) -> ThreadScope | None:
    """Normalise a lookup result into a scope dict of canonical values, or None (no thread).

    A mapping must carry a ``user_id`` (a thread always has an owner); one without it is
    treated as "no such thread" rather than guessed at.
    """
    if result is None:
        return None
    if isinstance(result, Mapping):
        if result.get("user_id") is None:
            return None
        return {str(key): canonical_value(value) for key, value in result.items()}
    return {"user_id": canonical_value(result)}


class ThreadOwnershipResolver:
    """Resolve (and cache) a thread's scope: its owning ``user_id`` and tenant values.

    Args:
        lookup: async backing lookup, ``(thread_id) -> scope dict | owner | None``
            (typically ``checkpointer.aget_thread_scope``, or ``aget_thread_owner`` for a
            checkpointer without it). May raise ``NotImplementedError`` if the checkpointer
            cannot resolve ownership; that propagates uncached.
        cache: the API's cache store, the shared L2 tier. None or an ``InMemoryCacheStore``
            means L1 only. Cache errors degrade to the DB lookup and never fail a request.
            The resolver never closes it. Keys: ``owner:id:<thread>`` (the owner as plain
            text, the layout older releases wrote and read) when there are no tenant
            columns, ``owner:scope:<thread>`` (a JSON object) when there are, and
            ``owner:tomb:<thread>`` for delete markers.
        max_size: L1 LRU capacity (entries).
        l1_ttl: seconds an L1 entry is trusted; bounds staleness after a delete elsewhere.
        l2_ttl: seconds an L2 entry lives in the cache store.
        tenant_columns: the checkpointer's declared tenant columns. Non-empty switches the L2
            entries to the JSON ``owner:scope:`` key, so a plain owner entry (from an older
            release or from before the columns were declared) is never read as a full scope.
    """

    def __init__(
        self,
        lookup: OwnerLookup,
        *,
        cache: BaseCacheStore | None = None,
        max_size: int = 10_000,
        l1_ttl: float = DEFAULT_L1_TTL_SECONDS,
        l2_ttl: int = DEFAULT_L2_TTL_SECONDS,
        tenant_columns: Iterable[str] = (),
    ) -> None:
        self._lookup = lookup
        self._tenant_columns = tuple(tenant_columns)
        # An in-memory store lives in this process, like L1: caching in it too adds nothing.
        self._cache = None if is_in_memory(cache) else cache
        self._max_size = max(1, max_size)
        self._l1_ttl = l1_ttl
        self._l2_ttl = l2_ttl
        # key -> (thread scope, monotonic expiry)
        self._l1: OrderedDict[str, tuple[ThreadScope, float]] = OrderedDict()
        # key -> (monotonic expiry, deleting owner or "") of the tombstone a delete left here
        self._tombstones: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ public

    async def scope_of(
        self, thread_id: str | int, columns: Iterable[str] = ()
    ) -> ThreadScope | None:
        """Return the thread's scope (canonical values), or None if the thread does not exist.

        ``columns`` are the columns the caller's isolation scope compares. A cached entry
        missing one of them (written before that tenant column was declared) is not trusted:
        the thread is looked up again and the cache refreshed. A column the checkpointer does
        not answer stays missing, and the caller treats that as no match.

        Raises:
            NotImplementedError: if the backing checkpointer cannot resolve ownership.
        """
        key = str(thread_id)
        needed = tuple(columns)

        cached = await self._l1_get(key)
        if cached is not None and _covers(cached, needed):
            return dict(cached)

        from_l2 = await self._l2_get(key)
        if from_l2 is not None and _covers(from_l2, needed):
            await self._l1_put(key, from_l2)
            return dict(from_l2)

        # may raise NotImplementedError -> propagate
        scope = to_scope(await self._lookup(key))
        if scope is None:
            return None  # never cache negatives (TOCTOU on new threads)

        await self._l1_put(key, scope)
        await self._l2_set(key, scope)
        return dict(scope)

    async def owner_of(self, thread_id: str | int) -> str | None:
        """Return the owning ``user_id`` for ``thread_id``, or None if it has no owner.

        Raises:
            NotImplementedError: if the backing checkpointer cannot resolve ownership.
        """
        scope = await self.scope_of(thread_id)
        return None if scope is None else scope.get("user_id")

    async def evict(self, thread_id: str | int, owner: str | int | None = None) -> None:
        """Drop any cached owner for ``thread_id`` and tombstone it (call on thread delete).

        ``owner`` is the user who deleted it (the owner, since the delete was authorized); that
        user may recreate the id at once, because a stale cache entry can only point at them.
        """
        key = str(thread_id)
        owner_str = "" if owner is None else str(owner)
        async with self._lock:
            cached = self._l1.pop(key, None)
            cached_owner = (cached[0].get("user_id") or "") if cached is not None else ""
            if not owner_str:
                owner_str = cached_owner
            elif cached_owner and cached_owner != owner_str:
                # Someone other than the owner deleted it (a tenant-wide scope, such as an
                # org admin). A stale entry elsewhere still names the old owner, so the
                # deleter gets no head start: the tombstone blocks everyone.
                owner_str = ""
            self._tombstones[key] = (time.monotonic() + self._l1_ttl, owner_str)
            self._tombstones.move_to_end(key)
            while len(self._tombstones) > self._max_size:
                self._tombstones.popitem(last=False)
        await self._l2_delete(key)
        await self._l2_tombstone(key, owner_str)

    async def _l2_delete(self, key: str) -> None:
        # Both layouts: an older worker may have written the plain owner entry.
        await self._l2_delete_key(self._owner_key(key))
        await self._l2_delete_key(self._scope_key(key))

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
        shared_owner = await self._l2_tombstone_owner(key)
        if shared_owner is None:
            return False
        return not (shared_owner and user_id is not None and shared_owner == str(user_id))

    # ------------------------------------------------------------------ L1

    async def _l1_get(self, key: str) -> ThreadScope | None:
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

    async def _l1_put(self, key: str, scope: ThreadScope) -> None:
        async with self._lock:
            self._l1[key] = (dict(scope), time.monotonic() + self._l1_ttl)
            self._l1.move_to_end(key)
            while len(self._l1) > self._max_size:
                self._l1.popitem(last=False)  # evict least-recently-used

    # ------------------------------------------------------------------ L2 (cache store)

    # Owner entries and delete markers live under different fixed segments, so no thread id
    # (not even one like "deleted:X") can make an owner key collide with a marker key.
    @staticmethod
    def _owner_key(key: str) -> str:
        return f"owner:id:{key_part(key)}"

    @staticmethod
    def _tombstone_key(key: str) -> str:
        return f"owner:tomb:{key_part(key)}"

    @staticmethod
    def _scope_key(key: str) -> str:
        return f"owner:scope:{key_part(key)}"

    async def _l2_get(self, key: str) -> ThreadScope | None:
        if self._cache is None:
            return None
        cache_key = self._scope_key(key) if self._tenant_columns else self._owner_key(key)
        try:
            value = await self._cache.get(cache_key)
        except Exception as exc:  # degrade to DB; never fail the request
            logger.debug("Ownership L2 get failed for %s: %s", key, exc)
            return None
        if value is None:
            return None
        if not self._tenant_columns:
            return {"user_id": _text(value)}
        return _decode_scope(value)

    async def _l2_set(self, key: str, scope: ThreadScope) -> None:
        if self._cache is None:
            return
        if self._tenant_columns:
            cache_key = self._scope_key(key)
            payload = json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
        else:
            cache_key = self._owner_key(key)
            payload = (scope.get("user_id") or "").encode()
        try:
            # evict() removes the entry on delete; the TTL covers an eviction that failed.
            await self._cache.set(cache_key, payload, ttl=self._l2_ttl)
        except Exception as exc:
            logger.debug("Ownership L2 set failed for %s: %s", key, exc)

    async def _l2_tombstone(self, key: str, owner: str) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(
                self._tombstone_key(key),
                owner.encode() or b"-",
                ttl=max(1, int(self._l1_ttl + 0.999)),
            )
        except Exception as exc:
            logger.debug("Ownership L2 tombstone failed for %s: %s", key, exc)

    async def _l2_tombstone_owner(self, key: str) -> str | None:
        """The deleting owner in a shared tombstone ("" when unknown), or None when none."""
        if self._cache is None:
            return None
        try:
            value = await self._cache.get(self._tombstone_key(key))
        except Exception as exc:  # degrade to "not deleted"; L1 already answered for us
            logger.debug("Ownership L2 tombstone read failed for %s: %s", key, exc)
            return None
        if value is None:
            return None
        text = _text(value)
        return "" if text == "-" else text

    async def _l2_delete_key(self, cache_key: str) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.delete(cache_key)
        except Exception as exc:
            logger.debug("Ownership L2 delete failed for %s: %s", cache_key, exc)


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes | bytearray) else str(value)


def _covers(scope: Mapping[str, Any], columns: Iterable[str]) -> bool:
    return all(column in scope for column in columns)


def _decode_scope(value: object) -> ThreadScope | None:
    """A JSON scope entry from the cache store, or None (a miss) when it is not one."""
    try:
        data = json.loads(_text(value))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("user_id"), str):
        return None
    if not all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in data.items()):
        return None
    return data
