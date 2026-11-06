"""Postgres-only checks for ``PostgresRunStore``: setup under load, migrate, schema option,
pool ownership, a backend killed mid-transaction, and claim races over many connections."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import pytest_asyncio

from tenxgraph_api.jobs.models import AttemptOutcome, ClaimStatus, NewRun, RunState
from tenxgraph_api.jobs.store.base import slot_name
from tenxgraph_api.jobs.store.postgres import (
    CURRENT_SCHEMA_VERSION,
    JobsSchemaError,
    PostgresRunStore,
    ensure_schema,
    migrate,
    table_names,
)


asyncpg = pytest.importorskip("asyncpg")

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def new_run(task: str, **over: Any) -> NewRun:
    data: dict[str, Any] = {
        "run_id": f"r-{uuid.uuid4().hex}",
        "task": task,
        "owner_user_id": "u1",
        "identity": {"user_id": "u1"},
    }
    data.update(over)
    return NewRun(**data)


async def tables_in(conn: Any, schema: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = $1 AND tablename LIKE 'tenxgraph_task_%'",
        schema,
    )
    return {r["tablename"] for r in rows}


async def wait_for_lock_waiter(conn: Any, needle: str, timeout: float = 5.0) -> int:
    """pid of the backend waiting on a lock while running a query that contains ``needle``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pid = await conn.fetchval(
            "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND position($1 in query) > 0 AND pid <> pg_backend_pid() LIMIT 1",
            needle,
        )
        if pid is not None:
            return int(pid)
        await asyncio.sleep(0.02)
    raise AssertionError(f"no backend waiting on a lock for {needle!r}")


@pytest_asyncio.fixture
async def store(pg_dsn: str, pg_schema: str):
    s = PostgresRunStore(dsn=pg_dsn, schema=pg_schema)
    await s.setup()
    try:
        yield s
    finally:
        await s.aclose()


# ---------------------------------------------------------------------- setup and migrate


async def test_fifty_concurrent_setups_run_ddl_once(pg_dsn, pg_schema, pg_connection):
    stores = [
        PostgresRunStore(dsn=pg_dsn, schema=pg_schema, pool_config={"min_size": 1, "max_size": 1})
        for _ in range(50)
    ]
    try:
        await asyncio.gather(*(s.setup() for s in stores))
        counts = [s.ddl_statements for s in stores]
        assert sum(1 for c in counts if c) == 1
        assert await tables_in(pg_connection, pg_schema) == {
            "tenxgraph_task_" + n for n in table_names(pg_schema)
        }
        versions = await pg_connection.fetch(
            f'SELECT version FROM "{pg_schema}".tenxgraph_task_schema_version'  # noqa: S608
        )
        assert [r["version"] for r in versions] == [CURRENT_SCHEMA_VERSION]
        # A restart at the current version runs no DDL.
        assert await ensure_schema(pg_connection, pg_schema) == 0
        run = (await stores[7].create_run(new_run("t"))).run
        assert (await stores[42].get_run(run.run_id)) == run
    finally:
        await asyncio.gather(*(s.aclose() for s in stores))


async def test_migrate_check_and_auto_migrate_off(pg_dsn, pg_schema, pg_connection):
    assert await migrate(pg_dsn, pg_schema, check=True) == 0
    assert await tables_in(pg_connection, pg_schema) == set()

    strict = PostgresRunStore(dsn=pg_dsn, schema=pg_schema, auto_migrate=False)
    try:
        with pytest.raises(JobsSchemaError):
            await strict.setup()
        assert await tables_in(pg_connection, pg_schema) == set()

        assert await migrate(pg_dsn, pg_schema) == CURRENT_SCHEMA_VERSION
        assert await migrate(pg_dsn, pg_schema, check=True) == CURRENT_SCHEMA_VERSION
        assert await migrate(pg_dsn, pg_schema) == CURRENT_SCHEMA_VERSION
        await strict.setup()
        assert strict.ddl_statements == 0
        assert (await strict.create_run(new_run("t"))).created
    finally:
        await strict.aclose()


async def test_schema_option_isolates_and_creates_schema(pg_dsn, pg_schema, pg_connection):
    other = f"jobs_t_{uuid.uuid4().hex}"
    a = PostgresRunStore(dsn=pg_dsn, schema=pg_schema)
    b = PostgresRunStore(dsn=pg_dsn, schema=other)
    try:
        await a.setup()
        await b.setup()
        assert await pg_connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = $1)", other
        )
        assert len(await tables_in(pg_connection, other)) == len(table_names(other))
        run = (await a.create_run(new_run("t", idempotency_key="k"))).run
        assert await b.get_run(run.run_id) is None
        # Same key in another schema is another run.
        assert (await b.create_run(new_run("t", idempotency_key="k"))).created
    finally:
        await a.aclose()
        await b.aclose()
        await pg_connection.execute(f'DROP SCHEMA IF EXISTS "{other}" CASCADE')

    for bad in ("", "a-b", 'x"; DROP TABLE t; --', "1abc", "a" * 64):
        with pytest.raises(ValueError):
            PostgresRunStore(dsn=pg_dsn, schema=bad)


async def test_pool_ownership(pg_dsn, pg_schema):
    with pytest.raises(ValueError):
        PostgresRunStore()
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
    try:
        with pytest.raises(ValueError):
            PostgresRunStore(dsn=pg_dsn, pool=pool)
        borrowed = PostgresRunStore(pool=pool, schema=pg_schema)
        await borrowed.setup()
        await borrowed.create_run(new_run("t"))
        await borrowed.aclose()
        await borrowed.aclose()
        assert not pool.is_closing()
        assert await pool.fetchval("SELECT 1") == 1
        with pytest.raises(RuntimeError):
            await borrowed.now()
    finally:
        await pool.close()

    owned = PostgresRunStore(dsn=pg_dsn, schema=pg_schema)
    await owned.setup()
    own_pool = owned._pool
    assert own_pool is not None
    await owned.aclose()
    assert own_pool.is_closing()
    await owned.aclose()


# ---------------------------------------------------------------------- faults


async def test_backend_killed_mid_claim_leaves_no_partial_state(store, pg_dsn, pg_schema):
    task = f"t-{uuid.uuid4().hex}"
    run = (await store.create_run(new_run(task))).run
    slot = slot_name(task, None)
    counters = table_names(pg_schema)["counters"]
    blocker = await asyncpg.connect(pg_dsn)
    watcher = await asyncpg.connect(pg_dsn)
    try:
        # Hold the counter row (uncommitted insert): the claim locks the run row, then waits.
        tx = blocker.transaction()
        await tx.start()
        await blocker.execute(
            f"INSERT INTO {counters} (slot, running) VALUES ($1, 0)",  # noqa: S608
            slot,
        )
        claim = asyncio.create_task(
            store.claim(run.run_id, worker_id="w1", lease_ttl=5, concurrency_limit=2)
        )
        pid = await wait_for_lock_waiter(watcher, "tenxgraph_task_counters")
        assert await watcher.fetchval("SELECT pg_terminate_backend($1)", pid)
        with pytest.raises((asyncpg.PostgresError, asyncpg.InterfaceError, OSError)):
            await claim
        await tx.rollback()
    finally:
        await blocker.close()
        await watcher.close()

    after = await store.get_run(run.run_id)
    assert after.state == RunState.QUEUED
    assert after.attempt == 0 and after.worker_id is None and after.slot is None
    assert await store.concurrency_count(task) == 0
    # The pool replaced the dead connection; the run is still claimable.
    ok = await store.claim(run.run_id, worker_id="w2", lease_ttl=5, concurrency_limit=2)
    assert ok.status == ClaimStatus.CLAIMED and ok.run.attempt == 1
    assert await store.concurrency_count(task) == 1


async def test_backend_killed_mid_transition_keeps_run_and_slot(store, pg_dsn, pg_schema):
    task = f"t-{uuid.uuid4().hex}"
    run = (await store.create_run(new_run(task))).run
    assert (
        await store.claim(run.run_id, worker_id="w1", lease_ttl=5, concurrency_limit=1)
    ).status == ClaimStatus.CLAIMED
    counters = table_names(pg_schema)["counters"]
    blocker = await asyncpg.connect(pg_dsn)
    watcher = await asyncpg.connect(pg_dsn)
    try:
        tx = blocker.transaction()
        await tx.start()
        await blocker.execute(
            f"SELECT running FROM {counters} WHERE slot = $1 FOR UPDATE",  # noqa: S608
            slot_name(task, None),
        )
        # The transition writes the run row, then waits for the counter row.
        done = asyncio.create_task(
            store.transition(
                run.run_id, attempt=1, worker_id="w1", outcome=AttemptOutcome(kind="succeeded")
            )
        )
        pid = await wait_for_lock_waiter(watcher, "tenxgraph_task_counters")
        assert await watcher.fetchval("SELECT pg_terminate_backend($1)", pid)
        with pytest.raises((asyncpg.PostgresError, asyncpg.InterfaceError, OSError)):
            await done
        await tx.rollback()
    finally:
        await blocker.close()
        await watcher.close()

    after = await store.get_run(run.run_id)
    assert after.state == RunState.RUNNING and after.attempt == 1 and after.worker_id == "w1"
    assert await store.concurrency_count(task) == 1
    # The worker retries its final write and it lands once.
    final = await store.transition(
        run.run_id, attempt=1, worker_id="w1", outcome=AttemptOutcome(kind="succeeded")
    )
    assert final.state == RunState.SUCCEEDED
    assert await store.concurrency_count(task) == 0


# ---------------------------------------------------------------------- races


@pytest_asyncio.fixture
async def stores(pg_dsn: str, pg_schema: str):
    many = [
        PostgresRunStore(dsn=pg_dsn, schema=pg_schema, pool_config={"min_size": 1, "max_size": 6})
        for _ in range(4)
    ]
    await asyncio.gather(*(s.setup() for s in many))
    try:
        yield many
    finally:
        await asyncio.gather(*(s.aclose() for s in many))


async def test_duplicate_deliveries_one_winner_each(stores):
    task = f"t-{uuid.uuid4().hex}"
    runs = [(await stores[0].create_run(new_run(task))).run for _ in range(40)]
    results = await asyncio.gather(
        *(
            stores[(i + j) % len(stores)].claim(r.run_id, worker_id=f"w{i}-{j}", lease_ttl=5)
            for i, r in enumerate(runs)
            for j in range(6)
        )
    )
    winners: dict[str, int] = {}
    for res in results:
        if res.status == ClaimStatus.CLAIMED:
            winners[res.run.run_id] = winners.get(res.run.run_id, 0) + 1
        else:
            assert res.status == ClaimStatus.NOT_READY
    assert winners == {r.run_id: 1 for r in runs}
    for r in runs:
        assert (await stores[1].get_run(r.run_id)).attempt == 1


async def test_capped_claims_across_connections(stores):
    task = f"t-{uuid.uuid4().hex}"
    runs = [(await stores[0].create_run(new_run(task, concurrency_key="k"))).run for _ in range(24)]
    results = await asyncio.gather(
        *(
            stores[i % len(stores)].claim(
                r.run_id, worker_id=f"w{i}", lease_ttl=5, concurrency_limit=4
            )
            for i, r in enumerate(runs)
        ),
        stores[1].recount_concurrency(),
        stores[2].recount_concurrency(),
    )
    claimed = [r for r in results[:-2] if r.status == ClaimStatus.CLAIMED]
    assert len(claimed) == 4
    assert all(r.status == ClaimStatus.CAPPED for r in results[:-2] if r not in claimed)
    assert await stores[3].concurrency_count(task, "k") == 4
    await stores[0].recount_concurrency()
    assert await stores[3].concurrency_count(task, "k") == 4


async def test_claim_next_hands_out_each_run_once(stores):
    queue = f"q-{uuid.uuid4().hex}"
    task = f"t-{uuid.uuid4().hex}"
    ids = {(await stores[0].create_run(new_run(task, queue=queue))).run.run_id for _ in range(30)}

    async def drain(store: PostgresRunStore, name: str) -> list[str]:
        got = []
        while (run := await store.claim_next([queue], worker_id=name, lease_ttl=5)) is not None:
            got.append(run.run_id)
        return got

    batches = await asyncio.gather(*(drain(stores[i % len(stores)], f"w{i}") for i in range(12)))
    taken = [run_id for batch in batches for run_id in batch]
    assert sorted(taken) == sorted(ids)
