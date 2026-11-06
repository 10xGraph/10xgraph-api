"""Fixtures for the jobs integration tests (real Redis, Postgres, RabbitMQ, SQLite).

Start the services with the compose file next to this module (see README.md). Every
fixture checks reachability with a short timeout and skips with a clear reason if the
service is down. Tests using these fixtures must carry ``@pytest.mark.integration``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import pytest_asyncio


COMPOSE_PROJECT = "tenxgraph-jobs-test"
COMPOSE_FILE = Path(__file__).with_name("docker-compose.yml")

DEFAULT_REDIS_URL = "redis://localhost:16379/1"
DEFAULT_REDIS7_URL = "redis://localhost:16380/1"
DEFAULT_PG_DSN = "postgresql://postgres:jobs_test_pw@localhost:15432/postgres"
DEFAULT_RABBIT_URL = "amqp://guest:guest@localhost:15673/"

CONNECT_TIMEOUT = 2.0


def _refuse_shared_port(url: str) -> None:
    """Never let a test run against a default-port (shared) Redis."""
    parsed = urlparse(url)
    if parsed.scheme.startswith("redis") and (parsed.port or 6379) == 6379:
        raise RuntimeError(f"refusing to use a Redis on port 6379: {url}")


# ---------------------------------------------------------------- Redis


async def _redis_client(url: str, name: str) -> Any:
    _refuse_shared_port(url)
    redis_asyncio = pytest.importorskip("redis.asyncio")
    client = redis_asyncio.from_url(
        url,
        socket_connect_timeout=CONNECT_TIMEOUT,
        socket_timeout=10,
        decode_responses=False,
    )
    try:
        await asyncio.wait_for(client.ping(), CONNECT_TIMEOUT + 1)
    except Exception as exc:
        await client.aclose()
        pytest.skip(f"{name} not reachable at {url}: {exc!r}")
    return client


async def _redis_isolated(url: str, name: str) -> AsyncIterator[Any]:
    client = await _redis_client(url, name)
    await client.flushdb()
    try:
        yield client
    finally:
        try:
            await client.flushdb()
        finally:
            await client.aclose()


@pytest.fixture
def redis_url() -> str:
    return os.environ.get("JOBS_TEST_REDIS_URL", DEFAULT_REDIS_URL)


@pytest.fixture
def redis7_url() -> str:
    return os.environ.get("JOBS_TEST_REDIS7_URL", DEFAULT_REDIS7_URL)


@pytest.fixture
def key_prefix() -> str:
    """Unique key prefix for one test (belt and braces next to the DB flush)."""
    return f"jt:{uuid.uuid4().hex[:12]}:"


@pytest_asyncio.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Any]:
    """redis.asyncio client on the Redis 8 service, logical DB flushed before and after."""
    async for client in _redis_isolated(redis_url, "jobs-redis"):
        yield client


@pytest_asyncio.fixture
async def redis7_client(redis7_url: str) -> AsyncIterator[Any]:
    """redis.asyncio client on the Redis 7 service, logical DB flushed before and after."""
    async for client in _redis_isolated(redis7_url, "jobs-redis7"):
        yield client


# ---------------------------------------------------------------- Postgres


@pytest.fixture
def pg_dsn() -> str:
    return os.environ.get("JOBS_TEST_PG_DSN", DEFAULT_PG_DSN)


@pytest_asyncio.fixture
async def pg_connection(pg_dsn: str) -> AsyncIterator[Any]:
    """Plain asyncpg connection (no schema isolation); skips if Postgres is down."""
    asyncpg = pytest.importorskip("asyncpg")
    try:
        conn = await asyncio.wait_for(asyncpg.connect(pg_dsn, timeout=CONNECT_TIMEOUT), 4)
    except Exception as exc:
        pytest.skip(f"jobs-pg not reachable: {exc!r}")
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def pg_schema(pg_dsn: str, pg_connection: Any) -> AsyncIterator[str]:
    """A fresh schema ``jobs_t_<uuid>`` created for the test and dropped afterwards.

    Point the store at it with ``search_path`` (or schema-qualify its tables).
    """
    schema = f"jobs_t_{uuid.uuid4().hex}"
    await pg_connection.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield schema
    finally:
        await pg_connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


# ---------------------------------------------------------------- RabbitMQ


@pytest.fixture
def rabbit_url() -> str:
    return os.environ.get("JOBS_TEST_RABBIT_URL", DEFAULT_RABBIT_URL)


@pytest_asyncio.fixture
async def rabbit_connection(rabbit_url: str) -> AsyncIterator[Any]:
    """aio-pika robust connection; skips if aio-pika is missing or RabbitMQ is down."""
    aio_pika = pytest.importorskip("aio_pika")
    try:
        conn = await asyncio.wait_for(
            aio_pika.connect_robust(rabbit_url, timeout=CONNECT_TIMEOUT), 5
        )
    except Exception as exc:
        pytest.skip(f"jobs-rabbit not reachable: {exc!r}")
    try:
        yield conn
    finally:
        await conn.close()


# ---------------------------------------------------------------- SQLite


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    """Temp SQLite file path (the file is not created)."""
    return tmp_path / "jobs.db"


# ---------------------------------------------------------------- Fault injection


def _docker(*args: str, timeout: float = 60) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class Fault:
    """Fault-injection helper. Only touches containers of the jobs test compose project."""

    def __init__(self) -> None:
        self._paused: set[str] = set()

    def container(self, service: str) -> str:
        """Resolve a compose service to its container name and verify the project."""
        out = _docker(
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={COMPOSE_PROJECT}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--format",
            "{{.Names}}",
        )
        names = [n for n in out.splitlines() if n]
        if len(names) != 1:
            pytest.skip(f"compose service {service!r} not running (found {names})")
        name = names[0]
        assert name.startswith(COMPOSE_PROJECT), f"refusing to touch container {name!r}"
        return name

    def docker_pause(self, service: str, seconds: float) -> None:
        """Freeze the container for ``seconds`` (blocking), then unpause."""
        name = self.container(service)
        _docker("pause", name)
        self._paused.add(name)
        try:
            time.sleep(seconds)
        finally:
            _docker("unpause", name)
            self._paused.discard(name)

    def docker_kill(self, service: str, *, restart: bool = True, wait: float = 60) -> None:
        """SIGKILL the container; by default start it again and wait until healthy."""
        name = self.container(service)
        _docker("kill", name)
        if restart:
            _docker("start", name)
            self.wait_healthy(service, wait)

    def wait_healthy(self, service: str, timeout: float = 60) -> None:
        name = self.container(service)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = _docker("inspect", "-f", "{{.State.Health.Status}}", name)
            if status == "healthy":
                return
            time.sleep(0.5)
        raise TimeoutError(f"{name} not healthy after {timeout}s")

    @staticmethod
    def kill_process(pid: int, sig: int = signal.SIGKILL) -> None:
        """Send SIGKILL, SIGSTOP or SIGCONT (or any signal) to a worker subprocess."""
        os.kill(pid, sig)

    def cleanup(self) -> None:
        for name in list(self._paused):
            try:
                _docker("unpause", name)
            except RuntimeError:
                pass
        self._paused.clear()


@pytest.fixture
def fault() -> Iterator[Fault]:
    helper = Fault()
    try:
        _docker("version", "--format", "{{.Server.Version}}", timeout=5)
    except Exception as exc:
        pytest.skip(f"docker not available: {exc!r}")
    yield helper
    helper.cleanup()
