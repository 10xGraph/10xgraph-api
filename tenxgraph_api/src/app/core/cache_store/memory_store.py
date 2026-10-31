"""``BaseCacheStore`` held in this process: correct for one worker, not shared between them.

Each operation runs under the ``asyncio.Lock`` of its key's stripe and has no ``await`` inside
that lock, so it is one atomic step on the event loop. Slot sets and plain keys live in two
separate tables, each in least-recently-used order.

Bounded memory, and the one exception to "never drop before TTL". Unbounded tables would let
a flood of distinct rate-limit keys (one per client IP) grow the process without limit, so
each table has a cap (``max_slot_keys``, 50,000 like the old memory rate limiter;
``max_values``, 10,000 like the old in-memory idempotency records). At the cap the store first
drops expired entries, then evicts the least recently used entry that is not protected, and
logs a warning (at most once a minute). Protected entries are never evicted while live: keys
whose feature segment is in ``protected_features`` (default ``concurrency``, every lease:
thread locks, run and WebSocket caps, idempotency running state; and ``revoked``, so a revoked
token cannot come back). They are bounded by real work in flight and by admin calls, not by
anonymous traffic. If only protected entries are left, the table grows past its cap (with the
warning) rather than break a lock. Everything else (rate-limit windows, idempotency records,
ownership cache entries) may be dropped before its TTL at the cap.

Expired entries are swept in the background every ``SWEEP_EVERY_OPS`` operations or
``SWEEP_INTERVAL_SECONDS`` seconds, whichever comes first, in batches that yield to the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import math
import time
import uuid
from collections import OrderedDict

from .base import BaseCacheStore, SetMode, SlotResult
from .redis_store import decimal_text


logger = logging.getLogger("tenxgraph_api.cache_store")

_NUM_STRIPES = 64

DEFAULT_MAX_SLOT_KEYS = 50_000
# Same as the old in-memory idempotency record cap (routers/graph/idempotency.py).
DEFAULT_MAX_VALUES = 10_000
DEFAULT_PROTECTED_FEATURES = ("concurrency", "revoked")


def _check_ttl(ttl: float) -> None:
    if not ttl > 0 or math.isinf(ttl):
        raise ValueError(f"ttl must be a positive number of seconds, got {ttl!r}")


class _SlotSet:
    """Live slots of one key: slot id -> expiry, plus a min-heap of expiries.

    The heap may hold stale entries (a renewed or released slot); they are skipped when they
    reach the top and the heap is rebuilt when they outnumber the live slots.
    """

    __slots__ = ("expiries", "heap")

    def __init__(self) -> None:
        self.expiries: dict[str, float] = {}
        self.heap: list[tuple[float, str]] = []

    def _clean_top(self) -> None:
        heap, expiries = self.heap, self.expiries
        while heap and expiries.get(heap[0][1]) != heap[0][0]:
            heapq.heappop(heap)

    def drop_expired(self, now: float) -> None:
        heap, expiries = self.heap, self.expiries
        while heap and heap[0][0] <= now:
            expiry, slot_id = heapq.heappop(heap)
            if expiries.get(slot_id) == expiry:
                del expiries[slot_id]
        self._clean_top()

    def oldest(self) -> float | None:
        self._clean_top()
        return self.heap[0][0] if self.heap else None

    def put(self, slot_id: str, expiry: float) -> None:
        self.expiries[slot_id] = expiry
        heapq.heappush(self.heap, (expiry, slot_id))
        if len(self.heap) > 2 * len(self.expiries) + 16:
            self.heap = [(e, s) for s, e in self.expiries.items()]
            heapq.heapify(self.heap)

    def remove(self, slot_id: str) -> None:
        self.expiries.pop(slot_id, None)
        self._clean_top()


class InMemoryCacheStore(BaseCacheStore):
    """Per-process cache store (see the module docstring for its memory bounds)."""

    SWEEP_EVERY_OPS = 2000
    SWEEP_INTERVAL_SECONDS = 60.0
    SWEEP_BATCH = 1000
    CAP_WARNING_INTERVAL = 60.0

    def __init__(
        self,
        *,
        max_slot_keys: int = DEFAULT_MAX_SLOT_KEYS,
        max_values: int = DEFAULT_MAX_VALUES,
        protected_features: tuple[str, ...] = DEFAULT_PROTECTED_FEATURES,
    ) -> None:
        self._slots: OrderedDict[str, _SlotSet] = OrderedDict()
        # key -> (value, expiry or None)
        self._values: OrderedDict[str, tuple[bytes, float | None]] = OrderedDict()
        self._max_slot_keys = max_slot_keys
        self._max_values = max_values
        self._protected = tuple(f"{feature}:" for feature in protected_features)
        self._locks = [asyncio.Lock() for _ in range(_NUM_STRIPES)]
        self._ops = 0
        self._last_sweep = time.monotonic()
        self._sweep_task: asyncio.Task[None] | None = None
        self._last_cap_warning: dict[str, float] = {}

    def _stripe_lock(self, key: str) -> asyncio.Lock:
        return self._locks[hash(key) % _NUM_STRIPES]

    def _protected_key(self, key: str) -> bool:
        return key.startswith(self._protected)

    # Sweeping and caps

    def _tick(self) -> None:
        self._ops += 1
        due = self._ops >= self.SWEEP_EVERY_OPS
        if not due and time.monotonic() - self._last_sweep < self.SWEEP_INTERVAL_SECONDS:
            return
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        self._ops = 0
        self._last_sweep = time.monotonic()
        self._sweep_task = asyncio.get_running_loop().create_task(self._sweep())

    async def _sweep(self) -> None:
        removed = 0
        for index, key in enumerate(list(self._slots)):
            if index and index % self.SWEEP_BATCH == 0:
                await asyncio.sleep(0)
            slots = self._slots.get(key)
            if slots is not None:
                slots.drop_expired(time.time())
                if not slots.expiries:
                    del self._slots[key]
                    removed += 1
        for index, key in enumerate(list(self._values)):
            if index and index % self.SWEEP_BATCH == 0:
                await asyncio.sleep(0)
            entry = self._values.get(key)
            if entry is not None and entry[1] is not None and entry[1] <= time.time():
                del self._values[key]
                removed += 1
        if removed:
            logger.debug("Cache store sweep removed %d expired entries", removed)

    def _warn_cap(self, table: str, cap: int, detail: str) -> None:
        now = time.monotonic()
        last = self._last_cap_warning.get(table)
        if last is not None and now - last < self.CAP_WARNING_INTERVAL:
            return
        self._last_cap_warning[table] = now
        logger.warning("In-memory cache store %s cap (%d) reached; %s", table, cap, detail)

    def _make_room_for_slots(self, now: float) -> None:
        """Free one entry in the slot table before a new key is added, if it is full."""
        if len(self._slots) < self._max_slot_keys:
            return
        victim = None
        for key, slots in self._slots.items():  # least recently used first
            slots.drop_expired(now)
            if not slots.expiries or not self._protected_key(key):
                victim = key
                break
        if victim is None:
            self._warn_cap("slot", self._max_slot_keys, "only live leases are left, growing")
            return
        if self._slots[victim].expiries:
            self._warn_cap("slot", self._max_slot_keys, "evicting least recently used keys")
        del self._slots[victim]

    def _make_room_for_values(self, now: float) -> None:
        if len(self._values) < self._max_values:
            return
        victim = None
        for key, (_, expiry) in self._values.items():
            expired = expiry is not None and expiry <= now
            if expired or not self._protected_key(key):
                victim = key
                break
        if victim is None:
            self._warn_cap("key", self._max_values, "only protected keys are left, growing")
            return
        expiry = self._values[victim][1]
        if expiry is None or expiry > now:
            self._warn_cap("key", self._max_values, "evicting least recently used keys")
        del self._values[victim]

    # BaseCacheStore

    async def aping(self) -> None:
        return None

    async def now(self) -> float:
        return time.time()

    async def acquire(self, key: str, limit: int, ttl: float) -> SlotResult:
        _check_ttl(ttl)
        self._tick()
        async with self._stripe_lock(key):
            # No await below: the check and the add are one step.
            now = time.time()
            slots = self._slots.get(key)
            if slots is not None:
                slots.drop_expired(now)
                self._slots.move_to_end(key)
            count = len(slots.expiries) if slots is not None else 0
            if count >= limit:
                if slots is not None and not slots.expiries:
                    del self._slots[key]
                    return SlotResult(False, 0, None, None)
                return SlotResult(False, count, slots.oldest() if slots else None, None)
            if slots is None:
                self._make_room_for_slots(now)
                slots = self._slots[key] = _SlotSet()
            slot_id = uuid.uuid4().hex
            slots.put(slot_id, now + ttl)
            return SlotResult(True, count + 1, slots.oldest(), slot_id)

    async def renew(self, key: str, slot_id: str, ttl: float) -> bool:
        _check_ttl(ttl)
        self._tick()
        async with self._stripe_lock(key):
            now = time.time()
            slots = self._slots.get(key)
            if slots is None:
                return False
            slots.drop_expired(now)
            if slot_id not in slots.expiries:
                if not slots.expiries:
                    del self._slots[key]
                return False
            slots.put(slot_id, now + ttl)
            self._slots.move_to_end(key)
            return True

    async def release(self, key: str, slot_id: str) -> None:
        async with self._stripe_lock(key):
            slots = self._slots.get(key)
            if slots is None:
                return
            slots.remove(slot_id)
            if not slots.expiries:
                del self._slots[key]

    async def get(self, key: str) -> bytes | None:
        self._tick()
        async with self._stripe_lock(key):
            entry = self._values.get(key)
            if entry is None:
                return None
            if entry[1] is not None and entry[1] <= time.time():
                del self._values[key]
                return None
            self._values.move_to_end(key)
            return entry[0]

    async def set(
        self,
        key: str,
        value: bytes,
        ttl: float | None,
        mode: SetMode = "always",
    ) -> bool:
        if ttl is not None:
            _check_ttl(ttl)
        if mode not in ("always", "if_absent", "if_greater"):
            raise ValueError(f"Unknown set mode {mode!r}")
        new_number = 0.0
        if mode == "if_greater":
            value = decimal_text(value).encode()
            new_number = float(value)
        self._tick()
        async with self._stripe_lock(key):
            now = time.time()
            entry = self._values.get(key)
            if entry is not None and entry[1] is not None and entry[1] <= now:
                del self._values[key]
                entry = None
            if mode == "if_absent" and entry is not None:
                return False
            if mode == "if_greater" and entry is not None:
                current = _number(entry[0])
                if current is not None and current >= new_number:
                    return False
            if entry is None:
                self._make_room_for_values(now)
            self._values[key] = (bytes(value), None if ttl is None else now + ttl)
            self._values.move_to_end(key)
            return True

    async def delete(self, key: str) -> None:
        async with self._stripe_lock(key):
            self._values.pop(key, None)
            self._slots.pop(key, None)

    async def aclose(self) -> None:
        task, self._sweep_task = self._sweep_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _number(raw: bytes) -> float | None:
    """A stored value as a number, or None when it is not one (then it counts as absent)."""
    try:
        return float(decimal_text(raw))
    except ValueError:
        return None
