"""The startup gate refuses to boot while a backend the server needs is unreachable."""

# ruff: noqa: S101, SLF001

import asyncio
import json
import logging
import socket
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.storage.store import BaseStore

from tenxgraph_api.src.app import main as app_main
from tenxgraph_api.src.app.core.cache_store import (
    BaseCacheStore,
    InMemoryCacheStore,
    RedisCacheStore,
)
from tenxgraph_api.src.app.core.cache_store.loader import (
    CacheStoreSource,
    bind_cache_store,
    load_and_bind_cache_store,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.routers.ping import readiness, startup_checks
from tenxgraph_api.src.app.routers.ping.router import router as ping_router
from tenxgraph_api.src.app.routers.ping.startup_checks import (
    HINT,
    StartupCheckError,
    Target,
    check_backends_at_startup,
    startup_targets,
    wait_for_backends,
)


class _Pinged:
    """A checkpointer or store: ``aping`` raises ``error`` when one is set."""

    def __init__(self, error: Exception | None = None, where: str | None = None):
        self.error = error
        self.pings = 0
        self._where = where

    async def aping(self):
        self.pings += 1
        if self.error:
            raise self.error

    def describe(self):
        return self._where


class _NoPing:
    pass


class _DownCache(InMemoryCacheStore):
    """A cache store that does not answer; describes itself as ``cache:6379``."""

    async def aping(self):
        raise ConnectionError("Error 111 connecting to cache:6379. Connection refused.")

    def describe(self):
        return "cache:6379"


# Token revocation counts only with auth on; a custom backend is parsed, never loaded, here.
AUTH = {"auth": {"method": "custom", "path": "auth.backend:auth"}}


def _store(**settings) -> dict:
    """``{"cache_store": {...}}``: the in-memory store unless ``type`` says otherwise."""
    return {"cache_store": settings}


def _config(tmp_path, data: dict) -> GraphConfig:
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps({"agent": "graph.agent:app", **data}))
    return GraphConfig(str(path))


def _container(*, checkpointer=None, store=None, cache=None, source=None, config=None) -> InjectQ:
    container = InjectQ()
    if checkpointer is not None:
        container.bind_instance(BaseCheckpointer, checkpointer)
    if store is not None:
        container.bind_instance(BaseStore, store)
    if cache is not None:
        bind_cache_store(container, cache, source)
    if config is not None:
        container.bind_instance(GraphConfig, config)
    return container


def _closed_port() -> int:
    """A local port nothing listens on: bound, then closed."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _no_sleep(_delay):
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    """The app's logger stops propagating once logging is set up; caplog needs it on."""
    monkeypatch.setattr(logging.getLogger("tenxgraph_api"), "propagate", True)


@pytest.fixture(autouse=True)
def _fresh_readiness():
    readiness.reset_cache()
    yield
    readiness.reset_cache()


@pytest.fixture
def unreachable_redis_url(monkeypatch):
    """A Redis URL at a closed local port, with short client timeouts."""
    settings = get_settings()
    monkeypatch.setattr(settings, "REDIS_CONNECT_TIMEOUT", 0.5)
    monkeypatch.setattr(settings, "REDIS_SOCKET_TIMEOUT", 0.5)
    monkeypatch.setattr(settings, "REDIS_POOL_TIMEOUT", 0.5)
    return f"redis://:s3cret-pass@127.0.0.1:{_closed_port()}/0"


# ---------------------------------------------------------------- targets


def test_unbound_components_are_left_out():
    assert startup_targets(_container()) == []
    assert [t.name for t in startup_targets(_container(store=_Pinged()))] == ["store"]


def test_cache_is_optional_unless_a_fail_closed_feature_uses_it(tmp_path):
    def cache_target(config):
        targets = startup_targets(_container(cache=_DownCache(), config=config))
        return next(t for t in targets if t.name == "cache")

    assert not cache_target(None).required
    assert not cache_target(_config(tmp_path, {})).required
    assert not cache_target(_config(tmp_path, _store(rate_limit={"enabled": True}))).required
    assert not cache_target(
        _config(tmp_path, _store(rate_limit={"enabled": False, "fail_open": False}))
    ).required
    assert not cache_target(_config(tmp_path, _store(token_revocation=False))).required

    assert not cache_target(_config(tmp_path, _store(token_revocation=True))).required  # no auth
    target = cache_target(_config(tmp_path, {**AUTH, **_store(token_revocation=True)}))
    assert target.required
    assert target.details[-1] == "required by token_revocation"
    target = cache_target(
        _config(tmp_path, _store(rate_limit={"enabled": True, "fail_open": False}))
    )
    assert target.required
    assert target.details[-1] == "required by rate_limit.fail_open=false"


def test_details_are_the_class_source_and_described_endpoint():
    container = _container(
        checkpointer=_Pinged(where="db:5432"),
        store=_Pinged(),
        cache=_DownCache(),
        source="cache_store.url",
    )
    by_name = {t.name: t.details for t in startup_targets(container)}
    assert by_name == {
        "checkpointer": ["_Pinged", "db:5432"],
        "store": ["_Pinged"],
        "cache": ["_DownCache", "cache_store.url", "cache:6379"],
    }


# ---------------------------------------------------------------- gate


@pytest.mark.asyncio
async def test_boot_check_passes_when_every_backend_answers(caplog):
    checkpointer, store = _Pinged(), _Pinged()
    container = _container(checkpointer=checkpointer, store=store, cache=InMemoryCacheStore())
    with caplog.at_level(logging.INFO, logger="tenxgraph_api.startup"):
        await check_backends_at_startup(container, timeout=0)
    assert checkpointer.pings == store.pings == 1
    assert "Startup check passed: checkpointer, store, cache" in caplog.text


@pytest.mark.asyncio
async def test_failed_checkpointer_names_component_and_class(caplog):
    error = ConnectionRefusedError("Connect call failed ('db', 5432)")
    container = _container(checkpointer=_Pinged(error, where="db:5432"))
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.startup"):
        with pytest.raises(StartupCheckError) as info:
            await check_backends_at_startup(container, timeout=0)
    message = str(info.value)
    assert "checkpointer (_Pinged, db:5432): ConnectionRefusedError" in message
    assert message.endswith(HINT)
    assert HINT == (
        "Start the backend, fix its URL, or set STARTUP_CHECKS=false to skip this check."
    )
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert any("attempt 1" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_unreachable_optional_cache_is_one_warning_and_boot_goes_on(caplog):
    checkpointer = _Pinged()
    container = _container(checkpointer=checkpointer, cache=_DownCache(), source="cache_store.url")
    with caplog.at_level(logging.INFO, logger="tenxgraph_api.startup"):
        await check_backends_at_startup(container, timeout=0)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].startswith(
        "Startup check: cache store _DownCache (cache_store.url, cache:6379) unreachable: locks "
        "and "
        "caps fall back to per process"
    )
    assert "Startup check passed: checkpointer" in caplog.text
    assert "passed: checkpointer, cache" not in caplog.text


@pytest.mark.asyncio
async def test_required_cache_refuses_boot_naming_cache_and_source(tmp_path):
    container = _container(
        cache=_DownCache(),
        source="cache_store.url",
        config=_config(tmp_path, {**AUTH, **_store(token_revocation=True)}),
    )
    with pytest.raises(StartupCheckError) as info:
        await check_backends_at_startup(container, timeout=0)
    assert (
        "cache (_DownCache, cache_store.url, cache:6379, required by token_revocation): "
        "ConnectionError" in str(info.value)
    )


@pytest.mark.asyncio
async def test_check_that_recovers_within_the_window_passes():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("not up yet")

    await wait_for_backends([Target("store", flaky)], timeout=30, sleep=_no_sleep)
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
async def test_component_without_aping_is_skipped_with_info(caplog):
    container = _container(store=_NoPing())
    with caplog.at_level(logging.INFO, logger="tenxgraph_api.startup"):
        await check_backends_at_startup(container, timeout=0)
    assert "store has nothing to check; skipped" in caplog.text


# ---------------------------------------------------------------- real Redis client


@pytest.mark.asyncio
async def test_redis_cache_store_at_a_closed_port_names_the_endpoint_not_the_password(
    tmp_path, unreachable_redis_url, caplog
):
    config = _config(
        tmp_path,
        _store(
            type="redis",
            url=unreachable_redis_url,
            rate_limit={"enabled": True, "fail_open": False},
        ),
    )
    container = _container(config=config)
    store = load_and_bind_cache_store(container, config)
    assert isinstance(store, RedisCacheStore)
    try:
        with caplog.at_level(logging.WARNING, logger="tenxgraph_api.startup"):
            with pytest.raises(StartupCheckError) as info:
                await check_backends_at_startup(container, timeout=0, attempt_timeout=1.0)
    finally:
        await store.aclose()
    message = str(info.value)
    port = unreachable_redis_url.rsplit(":", 1)[1].split("/")[0]
    assert f"cache (RedisCacheStore, cache_store.url, 127.0.0.1:{port}" in message
    assert "required by rate_limit.fail_open=false" in message
    assert "s3cret" not in message
    assert "s3cret" not in caplog.text


# ---------------------------------------------------------------- lifespan


def _lifespan_patches(monkeypatch, container, attach=None):
    monkeypatch.setattr(app_main, "container", container)
    monkeypatch.setattr(app_main, "attach_all_modules", attach or AsyncMock(return_value=None))
    monkeypatch.setattr(app_main, "_prepare_checkpointer", AsyncMock())
    monkeypatch.setattr(app_main, "_cleanup_temp_media_cache", AsyncMock())
    monkeypatch.setattr(app_main.settings, "STARTUP_CHECK_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(app_main.settings, "STARTUP_CHECKS", True)


def _attach(config, checkpointer=None):
    """Stand-in for ``attach_all_modules``: bind what the real one binds for these tests."""

    async def attach(graph_config, container):
        container.bind_instance(BaseCheckpointer, checkpointer or InMemoryCheckpointer())
        load_and_bind_cache_store(container, config)

    return attach


@pytest.mark.asyncio
async def test_lifespan_refuses_to_start_when_the_checkpointer_is_down(monkeypatch):
    container = _container(checkpointer=_Pinged(ConnectionError("db down")))
    _lifespan_patches(monkeypatch, container)
    with pytest.raises(StartupCheckError, match=r"checkpointer \(_Pinged\)"):
        async with app_main.lifespan(FastAPI()):
            pytest.fail("the app must not start serving")


@pytest.mark.asyncio
async def test_startup_checks_false_skips_the_gate(monkeypatch, caplog):
    _lifespan_patches(monkeypatch, _container(checkpointer=_Pinged(ConnectionError("down"))))
    monkeypatch.setattr(app_main.settings, "STARTUP_CHECKS", False)
    gate = AsyncMock()
    monkeypatch.setattr(startup_checks, "check_backends_at_startup", gate)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api"):
        async with app_main.lifespan(FastAPI()):
            pass
    gate.assert_not_called()
    assert "STARTUP_CHECKS is off" in caplog.text


@pytest.mark.asyncio
async def test_redis_url_alone_does_not_select_redis(
    monkeypatch, tmp_path, unreachable_redis_url, caplog
):
    """The rc3 bug: a memory config plus a stale REDIS_URL refused to boot in production.

    The store comes only from ``cache_store``, so REDIS_URL is never read: the server boots
    on the in-memory store and says nothing about Redis.
    """
    monkeypatch.setenv("REDIS_URL", unreachable_redis_url)
    config = _config(
        tmp_path,
        _store(type="memory", rate_limit={"enabled": True, "requests": 100, "window": 60}),
    )
    container = _container(config=config)
    _lifespan_patches(monkeypatch, container, attach=_attach(config))
    monkeypatch.setattr(app_main.settings, "MODE", "production")
    app = FastAPI()
    with caplog.at_level(logging.INFO, logger="tenxgraph_api"):
        async with app_main.lifespan(app):
            assert isinstance(container.get(BaseCacheStore), InMemoryCacheStore)
            assert container.get(CacheStoreSource).text == "cache_store.type"
            assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert "redis" not in caplog.text.lower()

            setup_fastapi(container, app)
            app.include_router(ping_router)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": {"checkpointer": "ok", "cache": "ok"}}


@pytest.mark.asyncio
async def test_token_revocation_with_auth_off_does_not_need_the_cache_store(
    monkeypatch, tmp_path, unreachable_redis_url, caplog
):
    """The loader does not enable revocation without auth, so the cache store stays optional."""
    config = _config(
        tmp_path,
        {"auth": None, **_store(type="redis", url=unreachable_redis_url, token_revocation=True)},
    )
    assert readiness.cache_required_by(config) == []
    container = _container(config=config)
    _lifespan_patches(monkeypatch, container, attach=_attach(config))
    app = FastAPI()
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.startup"):
        async with app_main.lifespan(app):
            warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
            assert len(warnings) == 1
            assert "No configured feature needs it, so the server starts." in warnings[0]

            setup_fastapi(container, app)
            app.include_router(ping_router)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"]["cache"] == "fail"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({**AUTH, "token_revocation": True}, "required by token_revocation"),
        ({"rate_limit": {"enabled": True, "fail_open": False}}, "rate_limit.fail_open=false"),
    ],
)
async def test_fail_closed_feature_with_unreachable_redis_refuses_boot(
    monkeypatch, tmp_path, unreachable_redis_url, data, reason
):
    auth = {"auth": data.pop("auth")} if "auth" in data else {}
    config = _config(tmp_path, {**auth, **_store(type="redis", url=unreachable_redis_url, **data)})
    container = _container(config=config)
    _lifespan_patches(monkeypatch, container, attach=_attach(config))
    with pytest.raises(StartupCheckError) as info:
        async with app_main.lifespan(FastAPI()):
            pytest.fail("the app must not start serving")
    message = str(info.value)
    assert "cache (RedisCacheStore, cache_store.url, 127.0.0.1:" in message
    assert reason in message
    assert "s3cret" not in message
