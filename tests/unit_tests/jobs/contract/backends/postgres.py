"""Postgres run store for the contract suite.

Runs only with ``--integration`` and a reachable Postgres (``JOBS_TEST_PG_DSN``, default the
compose service in ``tests/integration_tests/jobs``). Each test gets a fresh schema
``jobs_t_<uuid>``, dropped when the store is closed. ``advance`` sleeps (real clock).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

from tenxgraph_api.jobs.store.postgres import PostgresRunStore


DEFAULT_PG_DSN = "postgresql://postgres:jobs_test_pw@localhost:15432/postgres"
CONNECT_TIMEOUT = 2.0


def pg_dsn() -> str:
    return os.environ.get("JOBS_TEST_PG_DSN", DEFAULT_PG_DSN)


def integration_enabled() -> bool:
    args = sys.argv + os.environ.get("PYTEST_ADDOPTS", "").split()
    return "--integration" in args


class _SchemaPostgresStore(PostgresRunStore):
    """Drops its test schema after closing."""

    async def aclose(self) -> None:
        import asyncpg  # noqa: PLC0415

        if self._closed:
            return
        await super().aclose()
        conn = await asyncpg.connect(pg_dsn(), timeout=CONNECT_TIMEOUT)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        finally:
            await conn.close()


async def postgres_store():
    # Imported here: conftest imports this module while it is still loading.
    from tests.unit_tests.jobs.contract.conftest import StoreUnderTest  # noqa: PLC0415

    if not integration_enabled():
        pytest.skip("postgres store needs --integration")
    asyncpg = pytest.importorskip("asyncpg")
    dsn = pg_dsn()
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn, timeout=CONNECT_TIMEOUT), 4)
    except Exception as exc:
        pytest.skip(f"jobs-pg not reachable: {exc!r}")
    await conn.close()

    async def advance(seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    store = _SchemaPostgresStore(dsn=dsn, schema=f"jobs_t_{uuid.uuid4().hex}")
    return StoreUnderTest(store=store, advance=advance)


STORE_FACTORIES = {"postgres": postgres_store}
