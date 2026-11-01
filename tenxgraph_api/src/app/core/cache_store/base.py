"""The one extension point for state the API shares between its workers.

Run locks, run and WebSocket caps, idempotency records, the ownership cache, token revocation
and rate limiting all keep their shared state in a :class:`BaseCacheStore`. Two primitives
cover every one of them:

- **Timed slots** (``acquire`` / ``renew`` / ``release``): a set of expiring slots per key,
  capped at ``limit``. A lock is a slot set with ``limit=1``; a rate limit is a slot set whose
  slots are never released and expire after the window.
- **Keys with a TTL** (``get`` / ``set`` / ``delete``), with ``if_absent`` and ``if_greater``
  writes.

``RedisCacheStore`` (also Valkey, Dragonfly and KeyDB) shares the state between workers;
``InMemoryCacheStore`` keeps it in one process. Implement this class for a store that does not
speak the Redis protocol.

Keys: callers pass ``"<feature>:<id>"`` (for example ``"concurrency:runs:thread:42"``). The
store may map the feature segment to its own namespace and add a deployment prefix, but two
different keys must never land on the same entry, and any string (``:``, spaces, unicode) is
a valid key. A key holds either slots or a value, never both.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


SetMode = Literal["always", "if_absent", "if_greater"]


@dataclass(frozen=True)
class SlotResult:
    """Outcome of :meth:`BaseCacheStore.acquire`."""

    granted: bool
    # Live slots after this call, including ours when granted.
    count: int
    # Expiry time of the oldest live slot, on the store's clock; None when no slot is live.
    oldest_expiry: float | None
    # The new slot's id when granted (pass it to renew / release); None when denied.
    slot_id: str | None


class BaseCacheStore(ABC):
    """Shared state for the API: timed slots and keys with a TTL.

    The contract every implementation keeps (``tenxgraph_api.testing.cache_store_contract``
    checks it):

    - ``acquire`` is atomic: drop expired slots, and only if the live count is below
      ``limit``, add a new slot expiring at ``now() + ttl``. A denied call adds nothing. Two
      callers can never both take the last slot. ``limit < 1`` always denies.
    - ``renew`` sets a held slot's expiry to ``now() + ttl``; returns False when the slot no
      longer exists (expired or released). It never re-creates a slot.
    - ``release`` removes a slot; releasing a slot that is gone is not an error.
    - ``set(mode="always")`` writes and returns True. ``set(mode="if_absent")`` writes only
      when the key does not exist; returns whether it wrote.
    - ``set(mode="if_greater")`` treats the value as a decimal number and writes only when it
      is greater than the stored one (or nothing is stored); atomic. Used by token revocation
      so an older cutoff never overwrites a newer one. A value that is not a decimal number
      raises ``ValueError``.
    - ``ttl`` is in seconds and must be positive; ``set(ttl=None)`` keeps the key until it is
      deleted. A write replaces the TTL of the key it writes.
    - TTLs are honoured: a key or slot must not be dropped before its TTL. A store that evicts
      data under memory pressure breaks locks (two workers run the same thread). Redis with a
      ``maxmemory-policy`` other than ``noeviction`` or ``volatile-*`` is a deployment mistake.
      (``InMemoryCacheStore`` bounds its memory and documents its one exception.)
    - Every method touches exactly one key (Redis Cluster safe).
    - Errors: raise any exception when the backend fails. The features decide whether to fail
      open or closed. The store itself never swallows errors.
    - All times are the store's clock (``now()``), so pods with skewed clocks agree.
    """

    @abstractmethod
    async def aping(self) -> None:
        """Return when the store answers; raise otherwise."""

    @abstractmethod
    async def now(self) -> float:
        """The store's clock, in seconds since the epoch."""

    @abstractmethod
    async def acquire(self, key: str, limit: int, ttl: float) -> SlotResult:
        """Take one of ``limit`` slots for ``key``, expiring ``ttl`` seconds from now."""

    @abstractmethod
    async def renew(self, key: str, slot_id: str, ttl: float) -> bool:
        """Move a held slot's expiry to ``now() + ttl``; False if the slot is gone."""

    @abstractmethod
    async def release(self, key: str, slot_id: str) -> None:
        """Remove a slot (no error when it is already gone)."""

    @abstractmethod
    async def get(self, key: str) -> bytes | None:
        """The value stored under ``key``, or None."""

    @abstractmethod
    async def set(
        self,
        key: str,
        value: bytes,
        ttl: float | None,
        mode: SetMode = "always",
    ) -> bool:
        """Store ``value`` under ``key`` (see the class docstring for ``mode``); True if written."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove ``key`` (no error when it does not exist)."""

    def describe(self) -> str | None:
        """Where the store lives (for example ``host:port``) for logs and startup checks.

        Never includes credentials. None when there is nothing useful to name.
        """
        return None

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release resources (called once on app shutdown)."""
