"""The startup gate refuses to boot while a configured backend is unreachable."""

# ruff: noqa: S101, SLF001

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from injectq import InjectQ
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.storage.store import BaseStore

from tenxgraph_api.src.app import main as app_main
from tenxgraph_api.src.app.core import redis_client
from tenxgraph_api.src.app.core.config import settings as settings_mod
from tenxgraph_api.src.app.routers.ping import startup_checks
from tenxgraph_api.src.app.routers.ping.startup_checks import (
    StartupCheckError,
    Target,
    check_backends_at_startup,
    host_port,
    startup_targets,
    wait_for_backends,
)


REDIS_URL = "redis://:s3cret-pass@127.0.0.1:6399/0"


class _FakeRedis:
    def __init__(self, error: Exception | None = None, host="cache", port=6379):
        self.error = error
        self.connection_pool = SimpleNamespace(
            connection_kwargs={"host": host, "port": port, "password": "s3cret-pass"}
        )

    async def ping(self):
        if self.error:
            raise self.error
        return True

    async def aclose(self):
        return None


class _FakePgCheckpointer:
    """Looks like PgCheckpointer to the readiness probe."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self._pg_pool_config = {"postgres_dsn": "postgresql://app:s3cret-pass@db:5432/app"}
        self.redis = _FakeRedis()

    async def _run_query(self, fn):
        if self.error:
            raise self.error
        return 1


class _PingStore:
    def __init__(self):
        self.pings = 0

    async def aping(self):
        self.pings += 1


class _NoPingStore:
    pass


def _container(monkeypatch, *, checkpointer=None, store=None, redis_url=None) -> InjectQ:
    monkeypatch.setattr(settings_mod, "get_settings", lambda: SimpleNamespace(REDIS_URL=redis_url))
    container = InjectQ()
    if checkpointer is not None:
        container.bind_instance(BaseCheckpointer, checkpointer)
    if store is not None:
        container.bind_instance(BaseStore, store)
    return container


def _unreachable_redis(monkeypatch):
    error = ConnectionError("Error 111 connecting to 127.0.0.1:6399. Connection refused.")
    monkeypatch.setattr(redis_client, "create_redis", lambda url: _FakeRedis(error))


async def _no_sleep(_delay):
    await asyncio.sleep(0)


# ---------------------------------------------------------------- endpoints


def test_host_port_drops_credentials_path_and_query():
    assert host_port(REDIS_URL) == "127.0.0.1:6399"
    assert host_port("postgresql://u:p@db.internal:5432/app?sslmode=require") == "db.internal:5432"
    assert host_port("redis://[::1]:6379") == "[::1]:6379"
    assert host_port("not a url") is None
    assert host_port(None) is None


def test_unconfigured_components_are_left_out(monkeypatch):
    assert startup_targets(_container(monkeypatch)) == []
    names = [t.name for t in startup_targets(_container(monkeypatch, store=_PingStore()))]
    assert names == ["store"]


def test_rate_limiter_redis_is_checked_only_when_it_has_its_own_url(monkeypatch):
    container = _container(monkeypatch, redis_url=REDIS_URL)
    same = SimpleNamespace(redis_url=REDIS_URL)
    other = SimpleNamespace(redis_url="redis://limits:6380/1")
    bound_client = SimpleNamespace(redis_url=None)
    assert [t.name for t in startup_targets(container, same)] == ["redis"]
    assert [t.name for t in startup_targets(container, other)] == ["redis", "rate_limit redis"]
    assert [t.name for t in startup_targets(container, bound_client)] == ["redis"]


# ---------------------------------------------------------------- gate


@pytest.mark.asyncio
async def test_boot_check_passes_when_every_backend_answers(monkeypatch):
    monkeypatch.setattr(redis_client, "create_redis", lambda url: _FakeRedis())
    store = _PingStore()
    container = _container(
        monkeypatch, checkpointer=_FakePgCheckpointer(), store=store, redis_url=REDIS_URL
    )
    await check_backends_at_startup(container, timeout=0)
    assert store.pings == 1


@pytest.mark.asyncio
async def test_boot_check_fails_naming_component_and_host_without_password(monkeypatch, caplog):
    _unreachable_redis(monkeypatch)
    container = _container(monkeypatch, redis_url=REDIS_URL)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.startup"):
        with pytest.raises(StartupCheckError) as info:
            await check_backends_at_startup(container, timeout=0)
    message = str(info.value)
    assert "redis (127.0.0.1:6399)" in message
    assert "Connection refused" in message
    assert "STARTUP_CHECKS=false" in message
    assert "s3cret" not in message
    assert "s3cret" not in caplog.text
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert any("attempt 1" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_failed_checkpointer_names_postgres_and_redis_hosts(monkeypatch):
    error = ConnectionRefusedError("Connect call failed ('db', 5432)")
    container = _container(monkeypatch, checkpointer=_FakePgCheckpointer(error))
    with pytest.raises(StartupCheckError) as info:
        await check_backends_at_startup(container, timeout=0)
    message = str(info.value)
    assert "checkpointer (postgres db:5432, redis cache:6379)" in message
    assert "ConnectionRefusedError" in message
    assert "s3cret" not in message


@pytest.mark.asyncio
async def test_check_that_recovers_within_the_window_passes(monkeypatch):
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("not up yet")

    await wait_for_backends([Target("redis", flaky, ["r:6379"])], timeout=30, sleep=_no_sleep)
    assert calls == 3


@pytest.mark.asyncio
async def test_check_keeps_failing_until_the_window_closes(monkeypatch):
    calls = 0
    loop = asyncio.get_running_loop()
    clock = [loop.time()]
    monkeypatch.setattr(loop, "time", lambda: clock[0])

    async def down():
        nonlocal calls
        calls += 1
        raise ConnectionError("down")

    async def advance(delay):
        clock[0] += delay

    with pytest.raises(StartupCheckError, match="after 3s"):
        await wait_for_backends([Target("store", down)], timeout=3, sleep=advance)
    # 0.5 + 1 + 1.5 (the rest of the window): four rounds, then it gives up.
    assert calls == 4


@pytest.mark.asyncio
async def test_each_attempt_is_bounded_by_the_per_check_timeout():
    async def hang():
        await asyncio.sleep(30)

    with pytest.raises(StartupCheckError, match="timed out after 0.01s"):
        await wait_for_backends([Target("store", hang)], timeout=0, attempt_timeout=0.01)


@pytest.mark.asyncio
async def test_store_without_ping_is_skipped_with_info(monkeypatch, caplog):
    container = _container(monkeypatch, store=_NoPingStore())
    with caplog.at_level(logging.INFO, logger="tenxgraph_api.startup"):
        await check_backends_at_startup(container, timeout=0)
    assert "store has nothing to check; skipped" in caplog.text


# ---------------------------------------------------------------- lifespan


def _lifespan_patches(monkeypatch, container):
    monkeypatch.setattr(app_main, "container", container)
    monkeypatch.setattr(app_main, "attach_all_modules", AsyncMock(return_value=None))
    monkeypatch.setattr(app_main, "_cleanup_temp_media_cache", AsyncMock())
    monkeypatch.setattr(app_main.settings, "STARTUP_CHECK_TIMEOUT_SECONDS", 0.0)


@pytest.mark.asyncio
async def test_lifespan_refuses_to_start_when_redis_is_unreachable(monkeypatch):
    _unreachable_redis(monkeypatch)
    _lifespan_patches(monkeypatch, _container(monkeypatch, redis_url=REDIS_URL))
    monkeypatch.setattr(app_main.settings, "STARTUP_CHECKS", True)
    with pytest.raises(StartupCheckError, match=r"redis \(127\.0\.0\.1:6399\)"):
        async with app_main.lifespan(FastAPI()):
            pytest.fail("the app must not start serving")


@pytest.mark.asyncio
async def test_startup_checks_false_skips_the_gate(monkeypatch, caplog):
    _unreachable_redis(monkeypatch)
    _lifespan_patches(monkeypatch, _container(monkeypatch, redis_url=REDIS_URL))
    monkeypatch.setattr(app_main.settings, "STARTUP_CHECKS", False)
    gate = AsyncMock()
    monkeypatch.setattr(startup_checks, "check_backends_at_startup", gate)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api"):
        async with app_main.lifespan(FastAPI()):
            pass
    gate.assert_not_called()
    assert "STARTUP_CHECKS is off" in caplog.text
