"""Ping each jobs test service through the shared fixtures."""

from __future__ import annotations

import pytest
from redis import exceptions as redis_exc


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_redis8(redis_client, key_prefix):
    assert await redis_client.ping()
    await redis_client.set(key_prefix + "k", b"v")
    assert await redis_client.get(key_prefix + "k") == b"v"
    info = await redis_client.info("server")
    assert str(info["redis_version"]).startswith("8")


@pytest.mark.asyncio
async def test_redis7(redis7_client):
    assert await redis7_client.ping()
    info = await redis7_client.info("server")
    assert str(info["redis_version"]).startswith("7")


@pytest.mark.asyncio
async def test_postgres(pg_connection, pg_schema):
    assert await pg_connection.fetchval("SELECT 1") == 1
    await pg_connection.execute(f'CREATE TABLE "{pg_schema}".t (id int)')
    assert await pg_connection.fetchval(f'SELECT count(*) FROM "{pg_schema}".t') == 0


def test_sqlite_path(sqlite_path):
    import sqlite3

    conn = sqlite3.connect(sqlite_path)
    conn.execute("CREATE TABLE t (id int)")
    conn.close()
    assert sqlite_path.exists()


@pytest.mark.asyncio
async def test_rabbit(rabbit_connection):
    channel = await rabbit_connection.channel()
    assert not channel.is_closed


@pytest.mark.asyncio
async def test_fault_pause_and_kill(fault, redis7_client):
    fault.docker_pause("jobs-redis7", 1)
    assert await redis7_client.ping()
    fault.docker_kill("jobs-redis7")
    # The old socket died with the container; the client reconnects on a later call.
    for _ in range(5):
        try:
            assert await redis7_client.ping()
            break
        except redis_exc.RedisError:
            continue
    else:
        pytest.fail("client never reconnected")
