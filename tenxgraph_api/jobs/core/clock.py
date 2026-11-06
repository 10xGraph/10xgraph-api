"""Clocks. Times in the jobs system are float epoch seconds (UTC)."""

from __future__ import annotations

import time
from datetime import UTC, datetime


class SystemClock:
    """Wall clock, ``time.time()``."""

    def now(self) -> float:
        return time.time()

    def __call__(self) -> float:
        return self.now()


class FakeClock(SystemClock):
    """A clock that moves only when told to. Used by ``jobs_harness`` and the memory store.

    ``advance`` drives delays, retry backoff, lease expiry and progress flush timing in the
    harness. Attempt ``timeout`` is measured on the event loop's real clock, not this one.
    """

    def __init__(self, start: float | datetime | None = None) -> None:
        self._now = _to_epoch(start) if start is not None else time.time()

    def now(self) -> float:
        return self._now

    def set(self, when: float | datetime) -> None:
        """Jump to ``when``. Moving backwards is refused."""
        target = _to_epoch(when)
        if target < self._now:
            raise ValueError("FakeClock cannot move backwards")
        self._now = target

    def advance(
        self,
        seconds: float = 0.0,
        *,
        minutes: float = 0.0,
        hours: float = 0.0,
        days: float = 0.0,
    ) -> float:
        """Move forward and return the new time."""
        delta = seconds + minutes * 60 + hours * 3600 + days * 86400
        if delta < 0:
            raise ValueError("FakeClock.advance needs a non-negative amount")
        self._now += delta
        return self._now

    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self._now, tz=UTC)


def _to_epoch(value: float | datetime) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime: pass a timezone-aware datetime")
        return value.timestamp()
    return float(value)


def to_epoch(value: float | datetime) -> float:
    """Epoch seconds from a float or a timezone-aware datetime (naive is refused)."""
    return _to_epoch(value)
