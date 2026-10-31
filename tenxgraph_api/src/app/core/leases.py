"""Leases: held slots on the cache store, renewed while held and released on exit.

The per-thread run lock, the per-user run cap (``runs.max_concurrent_per_user``), the
WebSocket connection caps (``websocket.max_connections`` / ``max_connections_per_user``) and
the idempotency running state all count holders of a slot. :class:`LeaseManager` takes those
slots from the bound ``BaseCacheStore`` (``acquire`` / ``renew`` / ``release``), under keys
``concurrency:<lease key>``, so they are shared between workers when the store is shared
(``RedisCacheStore``) and per process with ``InMemoryCacheStore``.

A held lease expires ``LEASE_TTL_SECONDS`` after its last renewal and is renewed in the
background every third of that, so a worker that dies cannot hold a slot for longer than the
ttl. A renewal that finds the slot gone (expired during a stall, or evicted) sets
``Lease.lost`` and takes the slot again.

A cache store error fails open to this process's own count: the error is logged and the slot
is taken from an in-process ``InMemoryCacheStore`` instead, because refusing every request
while the store is down would be an outage of its own. The cap then holds per process, not
across workers, until the store is back (so the per-thread run lock still stops a double run
in one worker). Leases held in either store count against the same limit in this process, and
a lease taken in the fallback stays there until it is released.

Releases run on tasks of their own (a lease is often released from sync code, a ``finally``
or a garbage collection callback), and the app's shutdown waits for them before it closes the
cache store (``wait_for_releases``). A caller cancelled while its slot is being taken has the
slot released on such a task too, so a client that left while waiting leaves no ghost slot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from tenxgraph_api.src.app.core.cache_store.base import BaseCacheStore, SlotResult
from tenxgraph_api.src.app.core.cache_store.memory_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe


logger = logging.getLogger("tenxgraph_api.concurrency")

# A held lease expires this long after its last renewal.
LEASE_TTL_SECONDS = 60.0

# Longest the shutdown waits for pending releases before it closes the cache store anyway.
SHUTDOWN_RELEASE_WAIT_SECONDS = 5.0

# Feature segment of every lease key in the cache store.
LEASE_FEATURE = "concurrency"

# Release coroutines scheduled from sync code, referenced until they finish.
_pending_releases: set[asyncio.Task[Any]] = set()


def _schedule(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any] | None:
    """Run ``coro`` on a task of its own, referenced until it finishes; None without a loop."""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # no loop (garbage collection at shutdown): let the slot expire
        coro.close()
        return None
    _pending_releases.add(task)
    task.add_done_callback(_pending_releases.discard)
    return task


async def wait_for_releases(timeout: float = SHUTDOWN_RELEASE_WAIT_SECONDS) -> None:
    """Wait (up to ``timeout`` in all) for releases scheduled on tasks of their own."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _pending_releases:
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning(
                "%d lease releases did not finish before shutdown", len(_pending_releases)
            )
            return
        await asyncio.wait(set(_pending_releases), timeout=remaining)


class Lease:
    """A held slot. ``release`` is idempotent and safe to call from sync code."""

    def __init__(self, release: Callable[[], Any] | None = None) -> None:
        self._release = release
        self._released = release is None
        # Set when a shared lease was found missing on renewal (expired or evicted while held).
        self.lost = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        result = self._release()  # type: ignore[misc]
        if asyncio.iscoroutine(result):
            _schedule(result)


class _Held:
    """Where a granted lease lives; ``slot_id`` changes when a lost slot is taken again."""

    __slots__ = ("key", "limit", "renewer", "slot_id", "stop", "store")

    def __init__(self, store: BaseCacheStore, key: str, limit: int, slot_id: str) -> None:
        self.store = store
        self.key = key
        self.limit = limit
        self.slot_id: str | None = slot_id
        self.stop = asyncio.Event()
        self.renewer: asyncio.Task[None] | None = None


class LeaseManager:
    """Leases on a ``BaseCacheStore``, failing open to an in-process store (module docstring)."""

    def __init__(
        self,
        cache: BaseCacheStore,
        *,
        lease_ttl: float = LEASE_TTL_SECONDS,
        fallback: BaseCacheStore | None = None,
    ) -> None:
        self._cache = cache
        self._lease_ttl = lease_ttl
        # Slots taken while the cache store is unreachable, so one process still honours caps.
        self._fallback = fallback if fallback is not None else InMemoryCacheStore()
        # Leases this manager holds per lease key, and how many of them are in the fallback.
        self._held: dict[str, int] = {}
        self._in_fallback: dict[str, int] = {}
        # Releases still running per lease key; a new acquire in this process waits for them.
        self._releasing: dict[str, set[asyncio.Task[Any]]] = {}
        self._renewers: set[asyncio.Task[None]] = set()

    @property
    def cache(self) -> BaseCacheStore:
        """The cache store the leases are shared through (also used for idempotency records)."""
        return self._cache

    @property
    def fallback(self) -> BaseCacheStore:
        return self._fallback

    def held(self, key: str) -> int:
        """Leases this process holds for ``key`` (not the count across workers)."""
        return self._held.get(key, 0)

    async def acquire(self, key: str, limit: int) -> Lease | None:
        """Take one of ``limit`` slots for ``key``; None when all are taken."""
        pending = self._releasing.get(key)
        if pending:
            # This process's own release of the same key goes first, so a slot given back
            # just now is free for the next caller here (as it is with an in-process count).
            await asyncio.wait(set(pending))
        store_key = f"{LEASE_FEATURE}:{key}"
        local = self._in_fallback.get(key, 0)
        try:
            result = await self._take(self._cache, key, store_key, limit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Shared concurrency limit unavailable (%s); counting %s in this process",
                exc,
                log_safe(key),
            )
            # Leases this process holds in the shared store still count against the limit.
            shared_here = self._held.get(key, 0) - self._in_fallback.get(key, 0)
            result = await self._take(self._fallback, key, store_key, limit - shared_here)
            if not result.granted or result.slot_id is None:
                return None
            return self._hold(self._fallback, key, store_key, limit, result.slot_id)
        if not result.granted or result.slot_id is None:
            return None
        if local and result.count + local > limit:
            # Leases taken in the fallback during an outage are invisible to the shared store.
            self._release_later(key, self._release_slot(self._cache, store_key, result.slot_id))
            return None
        return self._hold(self._cache, key, store_key, limit, result.slot_id)

    async def _take(
        self, store: BaseCacheStore, key: str, store_key: str, limit: int
    ) -> SlotResult:
        """``store.acquire`` that gives the slot back if the caller is cancelled meanwhile."""
        task = asyncio.ensure_future(store.acquire(store_key, limit, self._lease_ttl))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                # The store may grant the slot after the caller left; release it then.
                self._release_later(key, self._discard_when_done(task, store, store_key))
            raise

    async def _discard_when_done(
        self, task: asyncio.Future[SlotResult], store: BaseCacheStore, store_key: str
    ) -> None:
        try:
            result = await task
        except BaseException:  # nothing was granted, or the error was already logged
            return
        if result.granted and result.slot_id is not None:
            await self._release_slot(store, store_key, result.slot_id)

    def _hold(
        self, store: BaseCacheStore, key: str, store_key: str, limit: int, slot_id: str
    ) -> Lease:
        held = _Held(store, store_key, limit, slot_id)
        in_fallback = store is self._fallback
        self._count(self._held, key, 1)
        if in_fallback:
            self._count(self._in_fallback, key, 1)

        def release() -> None:
            self._count(self._held, key, -1)
            if in_fallback:
                self._count(self._in_fallback, key, -1)
            with contextlib.suppress(RuntimeError):  # the loop is already closed
                held.stop.set()
            self._release_later(key, self._finish(held))

        lease = Lease(release)
        renewer = asyncio.get_running_loop().create_task(self._renew(held, lease))
        held.renewer = renewer
        self._renewers.add(renewer)
        renewer.add_done_callback(self._renewers.discard)
        return lease

    @staticmethod
    def _count(table: dict[str, int], key: str, delta: int) -> None:
        value = table.get(key, 0) + delta
        if value > 0:
            table[key] = value
        else:
            table.pop(key, None)

    def _release_later(self, key: str, coro: Coroutine[Any, Any, Any]) -> None:
        task = _schedule(coro)
        if task is None:
            return
        tasks = self._releasing.setdefault(key, set())
        tasks.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            tasks.discard(finished)
            if not tasks and self._releasing.get(key) is tasks:
                del self._releasing[key]

        task.add_done_callback(done)

    async def _finish(self, held: _Held) -> None:
        """Release a lease once its renewer has stopped (it may be taking the slot again)."""
        renewer = held.renewer
        if renewer is not None and not renewer.done():
            await asyncio.wait({renewer})
        slot_id, held.slot_id = held.slot_id, None
        if slot_id is not None:
            await self._release_slot(held.store, held.key, slot_id)

    @staticmethod
    async def _release_slot(store: BaseCacheStore, store_key: str, slot_id: str) -> None:
        try:
            await store.release(store_key, slot_id)
        except Exception as exc:  # the slot expires on its own
            logger.debug("Lease release failed for %s: %s", log_safe(store_key), exc)

    async def _renew(self, held: _Held, lease: Lease) -> None:
        """Keep a held lease alive until it is released; take it again if it was lost."""
        interval = max(self._lease_ttl / 3, 0.01)
        while True:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(interval):
                    await held.stop.wait()
            if held.stop.is_set():
                return
            try:
                slot_id = held.slot_id
                if slot_id is not None and await held.store.renew(
                    held.key, slot_id, self._lease_ttl
                ):
                    continue
                if held.stop.is_set():
                    return
                if slot_id is not None:
                    held.slot_id = None
                    lease.lost = True
                    logger.warning(
                        "Lease %s expired or was evicted while held; taking it again",
                        log_safe(held.key),
                    )
                result = await held.store.acquire(held.key, held.limit, self._lease_ttl)
                if result.granted:
                    held.slot_id = result.slot_id
            except Exception as exc:
                logger.warning("Lease refresh failed for %s: %s", log_safe(held.key), exc)

    async def aclose(self) -> None:
        """Wait for pending releases, then stop renewing leases that are still held."""
        await wait_for_releases()
        renewers = list(self._renewers)
        for task in renewers:
            task.cancel()
        await asyncio.gather(*renewers, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._fallback.aclose()


# Used when no LeaseManager is bound (no app container): one per process.
_process_manager: LeaseManager | None = None


def process_lease_manager() -> LeaseManager:
    """This process's own lease manager, over an ``InMemoryCacheStore``."""
    global _process_manager  # noqa: PLW0603 - one per process, made on first use
    if _process_manager is None:
        _process_manager = LeaseManager(InMemoryCacheStore())
    return _process_manager


def lease_manager() -> LeaseManager:
    """The lease manager bound in the app's container, else this process's own."""
    try:
        from tenxgraph_api.src.app.core.utils.container import current_container

        bound = current_container().try_get(LeaseManager)
    except Exception:
        bound = None
    return bound if isinstance(bound, LeaseManager) else process_lease_manager()
