"""Redis client policy for the jobs backends (no server needed): clients built from a URL never
retry a command (Lua scripts are not idempotent), and a supplied client that retries is
reported with one WARNING."""

from __future__ import annotations

import logging

import pytest


pytest.importorskip("redis")

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff, NoBackoff

from tenxgraph_api.jobs.store.redis import (
    SHORT_CLIENT_OPTIONS,
    client_retries,
    jobs_client_for,
    open_jobs_redis,
)
from tenxgraph_api.jobs.transport.redis import READ_CLIENT_OPTIONS


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "url",
    [
        "redis://localhost:16379/0",
        "redis://localhost:16379/0?retry_on_timeout=true",
    ],
)
@pytest.mark.parametrize("options", [SHORT_CLIENT_OPTIONS, READ_CLIENT_OPTIONS])
async def test_built_clients_never_retry(url, options):
    client = jobs_client_for(url, 1, {**options, "retry": Retry(ExponentialBackoff(), 5)})
    try:
        assert client_retries(client) == 0
        conn = client.connection_pool.make_connection()
        assert conn.retry.get_retries() == 0
    finally:
        await client.aclose()


async def test_supplied_client_with_retries_warns(caplog):
    retrying = Redis(host="localhost", port=16379, retry=Retry(ExponentialBackoff(), 3))
    quiet = Redis(host="localhost", port=16379, retry=Retry(NoBackoff(), 0))
    try:
        assert client_retries(retrying) == 3
        assert client_retries(quiet) == 0
        with caplog.at_level(logging.WARNING, logger="tenxgraph_api.jobs"):
            await open_jobs_redis(
                url=None, client=retrying, db=1, key_prefix="", options={}, what="run store"
            )
            await open_jobs_redis(
                url=None, client=quiet, db=1, key_prefix="", options={}, what="run store"
            )
        warnings = [r for r in caplog.records if "retries failed commands" in r.getMessage()]
        assert len(warnings) == 1
    finally:
        await retrying.aclose()
        await quiet.aclose()
