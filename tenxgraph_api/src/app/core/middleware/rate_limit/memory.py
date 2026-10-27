import asyncio
import logging
import time
from collections import OrderedDict, deque

from .base import BaseRateLimitBackend, RateLimitDecision


logger = logging.getLogger("tenxgraph_api.rate_limit")

_NUM_STRIPES = 64


class _Bucket:
    """Sliding-window log for one key plus the time it was last used."""

    __slots__ = ("last_seen", "times")

    def __init__(self, now: float) -> None:
        self.times: deque[float] = deque()
        self.last_seen = now


class MemoryRateLimitBackend(BaseRateLimitBackend):
    """In-process sliding-window rate limiter.

    This is useful for development and single-process deployments. It is not
    shared across workers or containers; use Redis for distributed enforcement.

    Keys are kept in least-recently-used order (oldest first). Every check moves its
    key to the end, so evicting at the key cap and removing idle keys only touch the
    front of the table and never scan it.
    """

    _SWEEP_INTERVAL = 2000
    _SWEEP_BATCH = 1000
    _CAP_WARNING_INTERVAL = 60.0

    def __init__(self, max_unique_keys: int = 50_000) -> None:
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._locks = [asyncio.Lock() for _ in range(_NUM_STRIPES)]
        self._sweep_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()
        self._check_count = 0
        self._max_unique_keys = max_unique_keys
        self._last_cap_warning: float | None = None

    def _stripe_lock(self, key: str) -> asyncio.Lock:
        return self._locks[hash(key) % _NUM_STRIPES]

    def _schedule_sweep(self, window: int) -> None:
        task = asyncio.ensure_future(self._sweep(window))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _pop_stale(self, cutoff: float, limit: int) -> int:
        """Remove up to ``limit`` idle keys from the front; stop at the first fresh one."""
        removed = 0
        while self._buckets and removed < limit:
            oldest = next(iter(self._buckets.values()))
            if oldest.last_seen >= cutoff:
                break
            self._buckets.popitem(last=False)
            removed += 1
        return removed

    def _warn_cap_reached(self, now: float) -> None:
        last = self._last_cap_warning
        if last is not None and now - last < self._CAP_WARNING_INTERVAL:
            return
        self._last_cap_warning = now
        logger.warning(
            "Rate-limit bucket cap (%d) reached; evicting least recently used keys",
            self._max_unique_keys,
        )

    async def _sweep(self, window: int) -> None:
        async with self._sweep_lock:
            cutoff = time.monotonic() - window * 2
            total = 0
            while True:
                # Each batch is synchronous, so the table is consistent at every await.
                removed = self._pop_stale(cutoff, self._SWEEP_BATCH)
                total += removed
                if removed < self._SWEEP_BATCH:
                    break
                await asyncio.sleep(0)
            if total:
                logger.debug("Rate-limit memory sweep removed %d stale buckets", total)

    async def check(self, key: str, *, limit: int, window: int) -> RateLimitDecision:
        self._check_count += 1
        if self._check_count % self._SWEEP_INTERVAL == 0:
            self._schedule_sweep(window)

        async with self._stripe_lock(key):
            # No await below: the table is only ever mutated in whole, atomic steps.
            now = time.monotonic()
            window_start = now - window

            entry = self._buckets.get(key)
            if entry is None:
                if len(self._buckets) >= self._max_unique_keys:
                    self._warn_cap_reached(now)
                    self._buckets.popitem(last=False)
                entry = self._buckets[key] = _Bucket(now)
            else:
                self._buckets.move_to_end(key)
                entry.last_seen = now
            bucket = entry.times

            while bucket and bucket[0] < window_start:
                bucket.popleft()

            count = len(bucket)
            if count >= limit:
                reset_after = max(1, int(bucket[0] + window - now) + 1)
                return RateLimitDecision(allowed=False, remaining=0, reset_after=reset_after)

            bucket.append(now)
            return RateLimitDecision(
                allowed=True,
                remaining=limit - len(bucket),
                reset_after=window,
            )

    async def close(self) -> None:
        for task in self._bg_tasks:
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
