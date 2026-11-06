"""SQLite ``local`` backend beyond the contract suite: several processes on one file, a locked
database, kill -9 mid-transaction, the SQLite version check, schema versions and the polling
transport across connections. No service needed."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import pytest

from tenxgraph_api.jobs.models import (
    AttemptOutcome,
    ClaimStatus,
    NewRun,
    RunState,
)


pytest.importorskip("aiosqlite")

from tenxgraph_api.jobs.store import sqlite as sqlite_store  # noqa: E402
from tenxgraph_api.jobs.store.sqlite import (  # noqa: E402
    SqliteRunStore,
    SqliteSchemaError,
    SqliteVersionError,
    check_sqlite_version,
    clock_sql,
)
from tenxgraph_api.jobs.transport.base import TransportMessage  # noqa: E402
from tenxgraph_api.jobs.transport.sqlite import SqliteTransport  # noqa: E402


pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[3]
QUEUE = "mp"
CAPPED = "capped-task"


def new_run(task: str = "plain-task", **over) -> NewRun:
    data = {
        "run_id": f"r-{uuid.uuid4().hex}",
        "task": task,
        "queue": QUEUE,
        "owner_user_id": "u1",
        "identity": {"user_id": "u1"},
        "retention": 60.0,
    }
    data.update(over)
    return NewRun(**data)


def run_python(code: str, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", textwrap.dedent(code), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(ROOT),
        check=False,
    )


# ---------------------------------------------------------------------- several processes

CLAIM_NEXT_WORKER = """
import asyncio, json, sys
from tenxgraph_api.jobs.models import AttemptOutcome
from tenxgraph_api.jobs.store.sqlite import SqliteRunStore

async def main(path, name):
    store = SqliteRunStore(path)
    await store.setup()
    claimed = []
    idle = 0
    while idle < 3:
        run = await store.claim_next(
            ["mp"], worker_id=name, lease_ttl=30, concurrency_limits={"capped-task": 2}
        )
        if run is None:
            idle += 1
            await asyncio.sleep(0.02)
            continue
        idle = 0
        claimed.append(run.run_id)
        done = await store.transition(
            run.run_id, attempt=run.attempt, worker_id=name,
            outcome=AttemptOutcome(kind="succeeded", result={"by": name}),
        )
        assert done is not None, run.run_id
    await store.aclose()
    print(json.dumps(claimed))

asyncio.run(main(sys.argv[1], sys.argv[2]))
"""

CLAIM_BY_ID_WORKER = """
import asyncio, json, random, sys
from tenxgraph_api.jobs.models import ClaimStatus
from tenxgraph_api.jobs.store.sqlite import SqliteRunStore

async def main(path, name, ids_file):
    ids = json.load(open(ids_file))
    random.shuffle(ids)
    store = SqliteRunStore(path)
    won = []
    for run_id in ids:
        res = await store.claim(run_id, worker_id=name, lease_ttl=30)
        if res.status == ClaimStatus.CLAIMED:
            won.append(run_id)
    await store.aclose()
    print(json.dumps(won))

asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
"""


async def _run_procs(code: str, args_per_proc: list[list[str]]) -> list[list[str]]:
    results = await asyncio.gather(
        *(asyncio.to_thread(run_python, code, *args) for args in args_per_proc)
    )
    out = []
    for res in results:
        assert res.returncode == 0, res.stderr
        out.append(json.loads(res.stdout.strip().splitlines()[-1]))
    return out


@pytest.mark.parametrize("procs", [2, 4])
async def test_processes_claim_next_no_double_claims(tmp_path, procs):
    path = tmp_path / "jobs.db"
    store = SqliteRunStore(path)
    await store.setup()
    ids = set()
    for i in range(500):
        task = CAPPED if i % 5 == 0 else "plain-task"
        ids.add((await store.create_run(new_run(task, concurrency_key="k"))).run.run_id)

    claimed = await _run_procs(CLAIM_NEXT_WORKER, [[str(path), f"w{i}"] for i in range(procs)])

    every = [run_id for batch in claimed for run_id in batch]
    dupes = [run_id for run_id, n in Counter(every).items() if n > 1]
    assert dupes == []
    assert set(every) == ids
    for run_id in ids:
        run = await store.get_run(run_id)
        assert run.state == RunState.SUCCEEDED
        assert run.attempt == 1
    assert await store.concurrency_count(CAPPED, "k") == 0
    await store.recount_concurrency()
    assert await store.counters() == {}
    await store.aclose()


async def test_processes_claim_same_ids_one_winner_each(tmp_path):
    path = tmp_path / "jobs.db"
    store = SqliteRunStore(path)
    ids = [(await store.create_run(new_run())).run.run_id for _ in range(500)]
    ids_file = tmp_path / "ids.json"
    ids_file.write_text(json.dumps(ids))

    won = await _run_procs(
        CLAIM_BY_ID_WORKER, [[str(path), f"w{i}", str(ids_file)] for i in range(3)]
    )

    every = [run_id for batch in won for run_id in batch]
    assert sorted(every) == sorted(ids)  # each run won exactly once
    for run_id in ids[:50]:
        assert (await store.get_run(run_id)).attempt == 1
    await store.aclose()


# ---------------------------------------------------------------------- locked database


def _hold_write_lock(path: Path, seconds: float) -> threading.Event:
    """Take the write lock from another connection and release it after ``seconds``."""
    held = threading.Event()

    def hold() -> None:
        conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        conn.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(seconds)
        conn.execute("COMMIT")
        conn.close()

    threading.Thread(target=hold, daemon=True).start()
    assert held.wait(5)
    return held


async def test_locked_database_is_retried(tmp_path):
    path = tmp_path / "jobs.db"
    store = SqliteRunStore(path, busy_timeout_ms=50, lock_retries=8)
    await store.setup()
    _hold_write_lock(path, 0.4)
    start = time.monotonic()
    res = await store.create_run(new_run())
    assert res.created
    assert time.monotonic() - start >= 0.3  # it waited for the other writer
    assert (await store.get_run(res.run.run_id)) is not None
    await store.aclose()


async def test_locked_database_gives_up_then_recovers(tmp_path):
    path = tmp_path / "jobs.db"
    store = SqliteRunStore(path, busy_timeout_ms=20, lock_retries=1)
    await store.setup()
    _hold_write_lock(path, 1.5)
    with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
        await store.create_run(new_run())
    await asyncio.sleep(1.6)
    # No transaction was left open: the next write works.
    assert (await store.create_run(new_run())).created
    await store.aclose()


async def test_reads_do_not_wait_for_the_writer(tmp_path):
    path = tmp_path / "jobs.db"
    store = SqliteRunStore(path, busy_timeout_ms=20, lock_retries=0)
    run = (await store.create_run(new_run())).run
    _hold_write_lock(path, 1.0)
    start = time.monotonic()
    assert (await store.get_run(run.run_id)).run_id == run.run_id  # WAL: readers never block
    assert time.monotonic() - start < 0.5
    await asyncio.sleep(1.1)
    await store.aclose()


# ---------------------------------------------------------------------- kill -9

KILLED_WRITER = """
import asyncio, sys
from tenxgraph_api.jobs.store.sqlite import SqliteRunStore

async def main(path, run_id):
    store = SqliteRunStore(path)
    await store.setup()

    async def half_done(conn):
        await conn.execute(
            "UPDATE tenxgraph_task_counters SET running = running + 5"
        )
        await conn.execute(
            "UPDATE tenxgraph_task_runs SET state = 'failed', attempt = 99 WHERE run_id = ?",
            (run_id,),
        )
        print("ready", flush=True)
        await asyncio.sleep(60)

    await store._write(half_done)

asyncio.run(main(sys.argv[1], sys.argv[2]))
"""


async def test_kill_9_mid_transaction_leaves_db_consistent(tmp_path):
    path = tmp_path / "jobs.db"
    store = SqliteRunStore(path)
    run = (await store.create_run(new_run(CAPPED))).run
    claim = await store.claim(run.run_id, worker_id="w1", lease_ttl=30, concurrency_limit=3)
    assert claim.status == ClaimStatus.CLAIMED
    other = (await store.create_run(new_run())).run

    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    child = subprocess.Popen(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", textwrap.dedent(KILLED_WRITER), str(path), run.run_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    try:
        line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), timeout=60)
        assert line.strip() == "ready", child.stderr.read()
        os.kill(child.pid, signal.SIGKILL)
        await asyncio.to_thread(child.wait, 10)
    finally:
        if child.poll() is None:
            child.kill()
        child.stdout.close()
        child.stderr.close()

    after = await store.get_run(run.run_id)
    assert after.state == RunState.RUNNING and after.attempt == 1
    assert await store.concurrency_count(CAPPED) == 1
    # The write lock died with the process: writes go through at once.
    start = time.monotonic()
    got = await store.claim_next([QUEUE], worker_id="w2", lease_ttl=30)
    assert got is not None and got.run_id == other.run_id
    assert time.monotonic() - start < 2.0
    done = await store.transition(
        run.run_id, attempt=1, worker_id="w1", outcome=AttemptOutcome(kind="succeeded")
    )
    assert done.state == RunState.SUCCEEDED
    assert await store.concurrency_count(CAPPED) == 0
    await store.aclose()
    with sqlite3.connect(str(path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ---------------------------------------------------------------------- version, clock, schema


def test_version_check_message():
    with pytest.raises(SqliteVersionError) as info:
        check_sqlite_version("3.34.1")
    text = str(info.value)
    assert "3.35.0" in text and "3.34.1" in text and "RETURNING" in text
    assert check_sqlite_version("3.35.0") == (3, 35, 0)


def test_store_refuses_old_sqlite(monkeypatch, tmp_path):
    monkeypatch.setattr(sqlite_store.sqlite3, "sqlite_version", "3.31.1")
    with pytest.raises(SqliteVersionError, match="3.31.1"):
        SqliteRunStore(tmp_path / "jobs.db")
    with pytest.raises(SqliteVersionError):
        SqliteTransport(tmp_path / "jobs.db")


def test_clock_expression_by_version():
    assert clock_sql("3.42.0") == "unixepoch('subsec')"
    assert "julianday" in clock_sql("3.41.2")


@pytest.mark.parametrize("version", ["3.41.2", "3.53.1"])
async def test_store_clock_is_epoch(tmp_path, version):
    linked = sqlite_store._version_tuple(sqlite3.sqlite_version)
    if sqlite_store._version_tuple(version) >= (3, 42, 0) > linked:
        pytest.skip("this SQLite has no unixepoch('subsec')")
    store = SqliteRunStore(tmp_path / "jobs.db")
    store._clock = clock_sql(version)
    assert abs(await store.now() - time.time()) < 1.0
    await store.aclose()


async def test_setup_creates_parent_and_is_idempotent(tmp_path):
    path = tmp_path / "nested" / ".10xgraph" / "jobs.db"
    for _ in range(2):
        store = SqliteRunStore(path)
        await store.setup()
        await store.setup()
        await store.aclose()
        await store.aclose()  # twice is a no-op
    assert path.exists()
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    with sqlite3.connect(str(path)) as conn:
        rows = dict(conn.execute("SELECT component, version FROM tenxgraph_task_schema"))
        assert rows == {"store": len(sqlite_store.STORE_MIGRATIONS)}
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


async def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "jobs.db"
    store = SqliteRunStore(path)
    await store.setup()
    await store.aclose()
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE tenxgraph_task_schema SET version = 99 WHERE component = 'store'")
    store = SqliteRunStore(path)
    with pytest.raises(SqliteSchemaError, match="99"):
        await store.setup()
    await store.aclose()


# ---------------------------------------------------------------------- transport


async def test_transport_across_connections_and_visibility_timeout(tmp_path):
    path = tmp_path / "jobs.db"
    producer = SqliteTransport(path)
    consumer = SqliteTransport(path, poll_interval=0.05, visibility_timeout=0.3)
    await producer.setup()
    await consumer.setup()
    message = TransportMessage(run_id="r1", queue=QUEUE, priority="high")

    async def later():
        await asyncio.sleep(0.1)
        await producer.publish(message)

    task = asyncio.create_task(later())
    got = await consumer.consume([QUEUE], max_messages=5, block=2.0)
    await task
    assert [d.message for d in got] == [message]
    # Not acked: hidden until the visibility timeout, then delivered again.
    assert await consumer.consume([QUEUE], max_messages=5) == []
    await asyncio.sleep(0.35)
    again = await consumer.consume([QUEUE], max_messages=5)
    assert [d.message for d in again] == [message]
    await consumer.ack(got)  # the stale receipt no longer matches
    assert await consumer.size() == 1
    await consumer.ack(again)
    assert await consumer.size() == 0
    await producer.aclose()
    await consumer.aclose()


async def test_store_and_transport_share_one_file(tmp_path):
    path = tmp_path / "jobs.db"
    store = SqliteRunStore(path)
    transport = SqliteTransport(path)
    await store.setup()
    await transport.setup()
    created = (await store.create_run(new_run())).run
    await transport.publish(TransportMessage(run_id=created.run_id, queue=QUEUE))
    (delivery,) = await transport.consume([QUEUE], max_messages=1, block=1.0)
    res = await store.claim(delivery.message.run_id, worker_id="w1", lease_ttl=30)
    await transport.ack([delivery])
    assert res.status == ClaimStatus.CLAIMED
    with sqlite3.connect(str(path)) as conn:
        rows = dict(conn.execute("SELECT component, version FROM tenxgraph_task_schema"))
    assert set(rows) == {"store", "transport"}
    await transport.aclose()
    await store.aclose()
