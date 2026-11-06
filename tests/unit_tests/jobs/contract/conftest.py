"""Fixtures for the store and transport contract suites.

Every backend joins by adding one factory below. A factory returns a ``StoreUnderTest`` /
``TransportUnderTest`` (or an awaitable of one) and should ``pytest.skip`` when its service is
not reachable (for example when ``TEST_POSTGRES_URL`` is unset).

``advance(seconds)`` moves the store clock: the memory store has a fake clock; a real store
sleeps (the checks use short leases, at most a few hundred ms, so this stays fast).
``redeliver()`` makes unacked transport deliveries available again (memory:
``requeue_unacked``; Redis: ``XAUTOCLAIM`` with a zero idle time). ``inject_raw(wire)`` puts a
raw wire message on the transport (Redis: ``XADD`` of the fields), for format checks.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict

from tenxgraph_api.jobs.clock import FakeClock
from tenxgraph_api.jobs.store.base import BaseRunStore
from tenxgraph_api.jobs.store.memory import MemoryRunStore
from tenxgraph_api.jobs.transport.base import BaseTransport
from tenxgraph_api.jobs.transport.memory import MemoryTransport


class StoreUnderTest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    store: BaseRunStore
    advance: Callable[[float], Awaitable[None]]


class TransportUnderTest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    transport: BaseTransport
    redeliver: Callable[[], Awaitable[Any]]
    inject_raw: Callable[[dict[str, Any]], Awaitable[None]]


def memory_store() -> StoreUnderTest:
    clock = FakeClock(1_800_000_000.0)

    async def advance(seconds: float) -> None:
        clock.advance(seconds)

    return StoreUnderTest(store=MemoryRunStore(clock), advance=advance)


def memory_transport() -> TransportUnderTest:
    transport = MemoryTransport()

    async def redeliver() -> int:
        return transport.requeue_unacked()

    async def inject_raw(wire: dict[str, Any]) -> None:
        transport.publish_raw(wire)

    return TransportUnderTest(transport=transport, redeliver=redeliver, inject_raw=inject_raw)


STORE_FACTORIES: dict[str, Callable[[], Any]] = {"memory": memory_store}
TRANSPORT_FACTORIES: dict[str, Callable[[], Any]] = {"memory": memory_transport}

# Each real backend lives in ``backends/<name>.py`` and exposes optional module-level
# ``STORE_FACTORIES`` / ``TRANSPORT_FACTORIES`` dicts, merged here, so backends never edit
# this file. Their factories skip when the service is not reachable or ``--integration`` is off.
for _name in ("postgres", "redis", "sqlite"):
    try:
        _mod = importlib.import_module(f"tests.unit_tests.jobs.contract.backends.{_name}")
    except ModuleNotFoundError as _exc:
        if _exc.name != f"tests.unit_tests.jobs.contract.backends.{_name}":
            raise
        continue
    STORE_FACTORIES.update(getattr(_mod, "STORE_FACTORIES", {}))
    TRANSPORT_FACTORIES.update(getattr(_mod, "TRANSPORT_FACTORIES", {}))


async def _build(factory: Callable[[], Any]) -> Any:
    made = factory()
    if inspect.isawaitable(made):
        made = await made
    return made


@pytest_asyncio.fixture(params=sorted(STORE_FACTORIES))
async def sut(request: pytest.FixtureRequest):
    under_test: StoreUnderTest = await _build(STORE_FACTORIES[request.param])
    await under_test.store.setup()
    try:
        yield under_test
    finally:
        await under_test.store.aclose()


@pytest_asyncio.fixture(params=sorted(TRANSPORT_FACTORIES))
async def tut(request: pytest.FixtureRequest):
    under_test: TransportUnderTest = await _build(TRANSPORT_FACTORIES[request.param])
    await under_test.transport.setup()
    try:
        yield under_test
    finally:
        await under_test.transport.aclose()
