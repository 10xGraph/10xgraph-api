# ruff: noqa: PLR2004 - the checks compare against literal counts

"""Contract checks for a ``BaseCacheStore`` implementation.

Every check takes a factory (a zero-argument callable returning a store, or an awaitable of
one), opens its own store, works under keys of its own and closes the store, so the checks can
run against a shared server. Use them in two ways:

As a pytest mixin (needs no pytest plugin; each test runs its check with ``asyncio.run``)::

    from tenxgraph_api.testing.cache_store_contract import CacheStoreContract


    class TestMyStore(CacheStoreContract):
        def make_store(self):
            return MyStore(...)

Or directly, from any async code::

    await run_cache_store_contract(lambda: MyStore(...))

A failed check raises ``AssertionError``. The timing checks use TTLs of a few hundred
milliseconds, so the store must answer well within that.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from tenxgraph_api.src.app.core.cache_store.base import BaseCacheStore


StoreFactory = Callable[[], BaseCacheStore | Awaitable[BaseCacheStore]]

SHORT_TTL = 0.3
LONG_TTL = 30.0
# Allowed gap between expected and reported expiry times (round trips, clock resolution).
TOLERANCE = 0.25


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _some[T](value: T | None, message: str) -> T:
    if value is None:
        raise AssertionError(message)
    return value


def _key(name: str) -> str:
    """A key no other run uses, so checks can share a server."""
    return f"contract:{uuid.uuid4().hex}:{name}"


@contextlib.asynccontextmanager
async def _open(factory: StoreFactory) -> AsyncIterator[BaseCacheStore]:
    made: Any = factory()
    store = await made if inspect.isawaitable(made) else made
    _expect(isinstance(store, BaseCacheStore), f"factory returned {type(store).__name__}")
    try:
        yield store
    finally:
        await store.aclose()


# 1
async def check_aping(factory: StoreFactory) -> None:
    async with _open(factory) as store:
        await store.aping()


# 2
async def check_acquire_limit(factory: StoreFactory) -> None:
    async with _open(factory) as store:
        key = _key("limit")
        slots = set()
        for taken in range(1, 4):
            result = await store.acquire(key, 3, LONG_TTL)
            _expect(result.granted, f"slot {taken} of 3 was denied")
            _expect(result.count == taken, f"count {result.count} after {taken} grants")
            _expect(result.slot_id is not None, "a granted slot has no slot_id")
            slots.add(result.slot_id)
        _expect(len(slots) == 3, "slot ids are not unique")
        for _ in range(3):
            denied = await store.acquire(key, 3, LONG_TTL)
            _expect(not denied.granted, "acquire over the limit was granted")
            _expect(denied.slot_id is None, "a denied acquire has a slot_id")
            _expect(denied.count == 3, f"a denied acquire changed the count to {denied.count}")
        _expect(not (await store.acquire(_key("zero"), 0, LONG_TTL)).granted, "limit 0 granted")


# 3
async def check_concurrent_acquire(factory: StoreFactory) -> None:
    async with _open(factory) as store:
        key = _key("race")
        results = await asyncio.gather(*(store.acquire(key, 5, LONG_TTL) for _ in range(50)))
        granted = [r for r in results if r.granted]
        _expect(len(granted) == 5, f"50 concurrent acquires granted {len(granted)}, not 5")
        after = await store.acquire(key, 5, LONG_TTL)
        _expect(not after.granted and after.count == 5, f"live count is {after.count}, not 5")


# 4
async def check_release_and_renew(factory: StoreFactory) -> None:
    async with _open(factory) as store:
        key = _key("lease")
        first = await store.acquire(key, 1, LONG_TTL)
        _expect(first.granted, "the first slot was denied")
        _expect(not (await store.acquire(key, 1, LONG_TTL)).granted, "a held lock was taken")
        first_id = _some(first.slot_id, "slot_id missing")
        await store.release(key, first_id)
        await store.release(key, first_id)  # releasing twice is not an error
        _expect(not await store.renew(key, first_id, LONG_TTL), "renewed a released slot")
        second = await store.acquire(key, 1, SHORT_TTL)
        _expect(second.granted, "release did not free the slot")
        _expect(second.count == 1, "renew of a released slot re-created it")
        second_id = _some(second.slot_id, "slot_id missing")

        # renew extends: past the original ttl the slot is still held
        await asyncio.sleep(SHORT_TTL / 2)
        _expect(await store.renew(key, second_id, SHORT_TTL * 3), "renew of a held slot")
        await asyncio.sleep(SHORT_TTL)
        held = await store.acquire(key, 1, LONG_TTL)
        _expect(not held.granted, "a renewed slot expired at its old ttl")

        # an expired slot cannot be renewed and is not re-created
        other = _key("expired")
        short = await store.acquire(other, 1, SHORT_TTL)
        short_id = _some(short.slot_id, "slot_id missing")
        await asyncio.sleep(SHORT_TTL + TOLERANCE)
        _expect(not await store.renew(other, short_id, LONG_TTL), "renewed an expired slot")
        retaken = await store.acquire(other, 1, LONG_TTL)
        _expect(retaken.granted and retaken.count == 1, "renew re-created an expired slot")
        await store.release(_key("missing"), "no-such-slot")


# 5
async def check_slot_expiry(factory: StoreFactory) -> None:
    async with _open(factory) as store:
        key = _key("expiry")
        start = await store.now()
        first = await store.acquire(key, 5, SHORT_TTL)
        first_oldest = _some(first.oldest_expiry, "oldest_expiry missing on a granted slot")
        _expect(
            abs(first_oldest - (start + SHORT_TTL)) <= TOLERANCE,
            f"oldest_expiry {first_oldest} is not about now + ttl ({start + SHORT_TTL})",
        )
        second = await store.acquire(key, 5, LONG_TTL)
        _expect(second.count == 2, "second slot not counted")
        _expect(second.oldest_expiry == first_oldest, "oldest_expiry is not the oldest")
        await asyncio.sleep(SHORT_TTL + TOLERANCE)
        later = await store.now()
        third = await store.acquire(key, 5, LONG_TTL)
        _expect(third.count == 2, f"expired slot still counted ({third.count} live)")
        third_oldest = _some(third.oldest_expiry, "oldest_expiry missing")
        _expect(
            abs(third_oldest - (start + LONG_TTL)) <= TOLERANCE + (later - start),
            "oldest_expiry did not move to the next live slot",
        )
        denied = await store.acquire(key, 2, LONG_TTL)
        _expect(not denied.granted, "acquire over the limit was granted")
        _expect(denied.oldest_expiry == third_oldest, "denied result has no oldest_expiry")

        lone = _key("lone")
        await store.acquire(lone, 1, SHORT_TTL)
        await asyncio.sleep(SHORT_TTL + TOLERANCE)
        fresh = await store.acquire(lone, 1, SHORT_TTL)
        _expect(fresh.granted and fresh.count == 1, "the only slot did not expire")


# 6
async def check_values(factory: StoreFactory) -> None:
    async with _open(factory) as store:
        key = _key("value")
        _expect(await store.get(key) is None, "a missing key has a value")
        _expect(await store.set(key, b"one", None) is True, "set(always) did not return True")
        _expect(await store.get(key) == b"one", "get after set")
        await store.set(key, b"two", LONG_TTL)
        _expect(await store.get(key) == b"two", "set(always) did not overwrite")
        await store.delete(key)
        _expect(await store.get(key) is None, "delete left the value")
        await store.delete(key)  # deleting a missing key is not an error

        short = _key("ttl")
        await store.set(short, b"x", SHORT_TTL)
        _expect(await store.get(short) == b"x", "value with a ttl not readable")
        await asyncio.sleep(SHORT_TTL + TOLERANCE)
        _expect(await store.get(short) is None, "value outlived its ttl")

        absent = _key("absent")
        _expect(await store.set(absent, b"a", LONG_TTL, "if_absent"), "if_absent on a new key")
        _expect(not await store.set(absent, b"b", LONG_TTL, "if_absent"), "if_absent overwrote")
        _expect(await store.get(absent) == b"a", "if_absent changed the value")

        greater = _key("greater")
        _expect(await store.set(greater, b"10.5", LONG_TTL, "if_greater"), "if_greater on missing")
        _expect(await store.set(greater, b"11", LONG_TTL, "if_greater"), "if_greater larger")
        _expect(not await store.set(greater, b"3", LONG_TTL, "if_greater"), "if_greater smaller")
        _expect(not await store.set(greater, b"11", LONG_TTL, "if_greater"), "if_greater equal")
        _expect(float(await store.get(greater) or b"nan") == 11, "if_greater kept the wrong value")
        for bad in (b"abc", b"", b"1e999"):
            with contextlib.suppress(ValueError):
                await store.set(greater, bad, LONG_TTL, "if_greater")
                raise AssertionError(f"if_greater accepted {bad!r}")

        race = _key("greater-race")
        values = [f"{1_700_000_000 + n * 0.25:.6f}".encode() for n in range(50)]
        await asyncio.gather(
            *(store.set(race, v, LONG_TTL, "if_greater") for v in reversed(values))
        )
        stored = await store.get(race)
        _expect(stored is not None and float(stored) == float(values[-1]), "lost the maximum")


# 7
async def check_clock(factory: StoreFactory) -> None:
    async with _open(factory) as store:
        previous = await store.now()
        _expect(previous > 1_600_000_000, f"now() {previous} is not seconds since the epoch")
        for _ in range(20):
            current = await store.now()
            _expect(current >= previous, f"now() went back from {previous} to {current}")
            previous = current


# 8
async def check_odd_keys(factory: StoreFactory) -> None:
    async with _open(factory) as store:
        base = _key("")
        names = [f"{base}a:b:c", f"{base}with space", f"{base}ünicøde 漢字", f"{base}%3A"]
        for index, name in enumerate(names):
            await store.set(name, f"v{index}".encode(), LONG_TTL)
        for index, name in enumerate(names):
            _expect(await store.get(name) == f"v{index}".encode(), f"value under {name!r}")
        for name in names:
            slot = await store.acquire(name + ":slots", 1, LONG_TTL)
            _expect(slot.granted, f"slot under {name!r}")
            slot_id = _some(slot.slot_id, "slot_id missing")
            _expect(await store.renew(name + ":slots", slot_id, LONG_TTL), "renew odd key")
            await store.release(name + ":slots", slot_id)
            await store.delete(name)
            _expect(await store.get(name) is None, f"delete under {name!r}")


CONTRACT_CHECKS: tuple[Callable[[StoreFactory], Awaitable[None]], ...] = (
    check_aping,
    check_acquire_limit,
    check_concurrent_acquire,
    check_release_and_renew,
    check_slot_expiry,
    check_values,
    check_clock,
    check_odd_keys,
)


async def run_cache_store_contract(factory: StoreFactory) -> None:
    """Run every contract check against stores made by ``factory``."""
    for check in CONTRACT_CHECKS:
        await check(factory)


class CacheStoreContract:
    """Pytest mixin: subclass it as ``Test...`` and implement :meth:`make_store`."""

    def make_store(self) -> BaseCacheStore | Awaitable[BaseCacheStore]:
        raise NotImplementedError

    def _run(self, check: Callable[[StoreFactory], Awaitable[None]]) -> None:
        async def main() -> None:
            await check(self.make_store)

        asyncio.run(main())

    def test_aping(self) -> None:
        self._run(check_aping)

    def test_acquire_limit(self) -> None:
        self._run(check_acquire_limit)

    def test_concurrent_acquire(self) -> None:
        self._run(check_concurrent_acquire)

    def test_release_and_renew(self) -> None:
        self._run(check_release_and_renew)

    def test_slot_expiry(self) -> None:
        self._run(check_slot_expiry)

    def test_values(self) -> None:
        self._run(check_values)

    def test_clock(self) -> None:
        self._run(check_clock)

    def test_odd_keys(self) -> None:
        self._run(check_odd_keys)
