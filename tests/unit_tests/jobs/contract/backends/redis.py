"""Redis run store and Redis Streams transport for the contract suites.

Run only with ``--integration`` and a reachable Redis: ``JOBS_TEST_REDIS_URL`` (default the
Redis 8 compose service, ``redis://localhost:16379/1``) and, for the store, also
``JOBS_TEST_REDIS7_URL`` (Redis 7, port 16380) as ``redis7``. Each store or transport gets a
unique key prefix (no braces, so the jobs hash tag stays in every key) and deletes its keys on
close. Port 6379 is refused. ``advance`` sleeps (real Redis clock).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
from urllib.parse import urlparse

import pytest

from tenxgraph_api.jobs.core.models import PRIORITIES
from tenxgraph_api.jobs.store.redis import RedisRunStore
from tenxgraph_api.jobs.transport.redis import RedisStreamsTransport
from tests.unit_tests.jobs.contract.backends import integration_enabled


DEFAULT_REDIS_URL = "redis://localhost:16379/1"
DEFAULT_REDIS7_URL = "redis://localhost:16380/1"
CONNECT_TIMEOUT = 2.0


def redis_url() -> str:
    return os.environ.get("JOBS_TEST_REDIS_URL", DEFAULT_REDIS_URL)


def redis7_url() -> str:
    return os.environ.get("JOBS_TEST_REDIS7_URL", DEFAULT_REDIS7_URL)


def unique_prefix() -> str:
    return f"jt-{uuid.uuid4().hex[:12]}:"


async def require_redis(url: str, name: str) -> None:
    """Skip unless ``--integration`` is on and Redis at ``url`` answers; refuse port 6379."""
    if not integration_enabled():
        pytest.skip(f"{name} needs --integration")
    if (urlparse(url).port or 6379) == 6379:
        raise RuntimeError(f"refusing to use a Redis on port 6379: {url}")
    redis_asyncio = pytest.importorskip("redis.asyncio")
    client = redis_asyncio.from_url(url, socket_connect_timeout=CONNECT_TIMEOUT, socket_timeout=5)
    try:
        await asyncio.wait_for(client.ping(), CONNECT_TIMEOUT + 1)
    except Exception as exc:
        pytest.skip(f"{name} not reachable at {url}: {exc!r}")
    finally:
        await client.aclose()


async def delete_prefix(client: Any, prefix: str) -> None:
    keys = [k async for k in client.scan_iter(match=f"{prefix}*", count=500)]
    for i in range(0, len(keys), 500):
        await client.delete(*keys[i : i + 500])


class CleanRedisRunStore(RedisRunStore):
    """Deletes its keys when closed."""

    async def aclose(self) -> None:
        if self._conn is not None and not self._closed:
            await delete_prefix(self._conn.client, self._conn.prefix)
        await super().aclose()


class CleanRedisStreamsTransport(RedisStreamsTransport):
    """Deletes its streams when closed."""

    async def aclose(self) -> None:
        conn = self._conn
        await super().aclose()
        if conn is not None and self._url:
            import redis.asyncio as redis_asyncio

            client = redis_asyncio.from_url(self._url, db=conn.db, socket_timeout=5)
            try:
                await delete_prefix(client, conn.prefix)
            finally:
                await client.aclose()


async def _store(url: str, name: str):
    from tests.unit_tests.jobs.contract.conftest import StoreUnderTest

    await require_redis(url, name)

    async def advance(seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    return StoreUnderTest(
        store=CleanRedisRunStore(url=url, key_prefix=unique_prefix()), advance=advance
    )


async def redis_store():
    return await _store(redis_url(), "jobs-redis")


async def redis7_store():
    return await _store(redis7_url(), "jobs-redis7")


async def redis_transport():
    from tests.unit_tests.jobs.contract.conftest import TransportUnderTest

    url = redis_url()
    await require_redis(url, "jobs-redis")
    transport = CleanRedisStreamsTransport(
        url=url, key_prefix=unique_prefix(), consumer=f"contract-{uuid.uuid4().hex[:8]}"
    )

    async def redeliver() -> int:
        return await transport.reclaim(min_idle=0.0)

    async def inject_raw(wire: dict[str, Any]) -> None:
        priority = wire.get("p")
        stream = transport.stream_key(
            str(wire.get("queue", "default")),
            priority if priority in PRIORITIES else "normal",
        )
        await transport.publish_raw(stream, wire)

    return TransportUnderTest(transport=transport, redeliver=redeliver, inject_raw=inject_raw)


STORE_FACTORIES = {"redis": redis_store, "redis7": redis7_store}
TRANSPORT_FACTORIES = {"redis": redis_transport}
