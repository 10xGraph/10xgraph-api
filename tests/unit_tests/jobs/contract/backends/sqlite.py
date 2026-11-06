"""SQLite ``local`` backend factories for the contract suites.

No service needed, so these run without ``--integration``: each factory gets a fresh file in
its own temporary directory, removed when the store or transport is closed. The store clock is
real, so ``advance`` sleeps.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tenxgraph_api.jobs.store.sqlite import SqliteRunStore
from tenxgraph_api.jobs.transport.sqlite import SqliteTransport


class _TempStore(SqliteRunStore):
    async def aclose(self) -> None:
        await super().aclose()
        shutil.rmtree(Path(self.path).parent, ignore_errors=True)


class _TempTransport(SqliteTransport):
    async def aclose(self) -> None:
        await super().aclose()
        shutil.rmtree(Path(self.path).parent, ignore_errors=True)


def _temp_db() -> Path:
    pytest.importorskip("aiosqlite")
    return Path(tempfile.mkdtemp(prefix="tenxgraph-jobs-")) / "jobs.db"


def sqlite_store() -> Any:
    # Imported here: this module is imported while conftest is still loading.
    from tests.unit_tests.jobs.contract.conftest import StoreUnderTest

    async def advance(seconds: float) -> None:
        await asyncio.sleep(seconds)

    return StoreUnderTest(store=_TempStore(_temp_db()), advance=advance)


def sqlite_transport() -> Any:
    from tests.unit_tests.jobs.contract.conftest import TransportUnderTest

    transport = _TempTransport(_temp_db())

    async def redeliver() -> int:
        return await transport.requeue_unacked()

    async def inject_raw(wire: dict[str, Any]) -> None:
        await transport.publish_raw(wire)

    return TransportUnderTest(transport=transport, redeliver=redeliver, inject_raw=inject_raw)


STORE_FACTORIES = {"sqlite": sqlite_store}
TRANSPORT_FACTORIES = {"sqlite": sqlite_transport}
