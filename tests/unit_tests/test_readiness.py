"""GET /health/ready checks the dependencies a request needs."""

# ruff: noqa: S101

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.storage.store import BaseStore

from tenxgraph_api.src.app.core.auth.route_guard import find_unprotected_routes
from tenxgraph_api.src.app.core.config import setup_middleware as sm
from tenxgraph_api.src.app.core.middleware.rate_limit.middleware import HEALTH_CHECK_PATHS
from tenxgraph_api.src.app.routers.ping import readiness
from tenxgraph_api.src.app.routers.ping.router import router as ping_router


class _Broken:
    async def aget_thread_owner(self, thread_id):
        raise ConnectionError("postgres://user:secret@db is down")


class _PgLike:
    def __init__(self, fail_redis=False):
        self.queries = []
        self.redis = SimpleNamespace(ping=self._ping)
        self._fail_redis = fail_redis

    async def _run_query(self, fn):
        conn = SimpleNamespace(fetchval=self._fetchval)
        return await fn(conn)

    async def _fetchval(self, sql):
        self.queries.append(sql)
        return 1

    async def _ping(self):
        if self._fail_redis:
            raise ConnectionError("redis down")
        return True


class _SelfChecking:
    def __init__(self):
        self.called = False

    async def readiness_check(self):
        self.called = True


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_configured_is_ready():
    assert await readiness.readiness(None, None, None) == {
        "checkpointer": "skipped",
        "redis": "skipped",
        "store": "skipped",
    }


@pytest.mark.asyncio
async def test_in_memory_checkpointer_answers_a_lookup():
    checks = await readiness.readiness(InMemoryCheckpointer(), None, None)
    assert checks["checkpointer"] == "ok"


@pytest.mark.asyncio
async def test_failing_checkpointer_fails_without_leaking_the_reason(caplog):
    checks = await readiness.readiness(_Broken(), None, None)
    assert checks["checkpointer"] == "fail"
    assert "secret" in caplog.text  # logged for operators ...
    assert "secret" not in str(checks)  # ... never returned


@pytest.mark.asyncio
async def test_pg_style_checkpointer_runs_select_1_and_pings_its_redis():
    pg = _PgLike()
    assert (await readiness.readiness(pg, None, None))["checkpointer"] == "ok"
    assert pg.queries == ["SELECT 1"]
    assert (await readiness.readiness(_PgLike(fail_redis=True), None, None))[
        "checkpointer"
    ] == "fail"


@pytest.mark.asyncio
async def test_a_components_own_check_is_preferred():
    checkpointer, store = _SelfChecking(), _SelfChecking()
    checks = await readiness.readiness(checkpointer, store, None)
    assert checks["checkpointer"] == checks["store"] == "ok"
    assert checkpointer.called and store.called


@pytest.mark.asyncio
async def test_store_without_a_probe_is_skipped_and_ping_is_used():
    assert (await readiness.readiness(None, object(), None))["store"] == "skipped"

    async def aping():
        return True

    assert (await readiness.readiness(None, SimpleNamespace(aping=aping), None))["store"] == "ok"


@pytest.mark.asyncio
async def test_slow_dependency_times_out(monkeypatch):
    monkeypatch.setattr(readiness, "CHECK_TIMEOUT_SECONDS", 0.05)

    class _Hangs:
        async def readiness_check(self):
            await asyncio.sleep(5)

    assert (await readiness.readiness(_Hangs(), None, None))["checkpointer"] == "fail"


@pytest.mark.asyncio
async def test_redis_is_pinged_and_closed(monkeypatch):
    calls = []

    class _Client:
        async def ping(self):
            calls.append("ping")

        async def aclose(self):
            calls.append("closed")

    monkeypatch.setattr(
        "tenxgraph_api.src.app.core.redis_client.create_redis", lambda url, **kw: _Client()
    )
    assert (await readiness.readiness(None, None, "redis://cache"))["redis"] == "ok"
    assert calls == ["ping", "closed"]


# ---------------------------------------------------------------------------
# the endpoint
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cache():
    readiness.reset_cache()
    yield
    readiness.reset_cache()


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """The endpoint tests do not configure Redis, whatever the environment says."""
    from tenxgraph_api.src.app.core.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: SimpleNamespace(REDIS_URL=None))


def _client(checkpointer=None, store=None) -> TestClient:
    container = InjectQ()
    if checkpointer is not None:
        container.bind_instance(BaseCheckpointer, checkpointer)
    if store is not None:
        container.bind_instance(BaseStore, store)
    app = FastAPI()
    setup_fastapi(container, app)
    app.include_router(ping_router)
    return TestClient(app)


def test_endpoint_is_200_when_ready():
    resp = _client(InMemoryCheckpointer()).get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert resp.json()["checks"]["checkpointer"] == "ok"


def test_endpoint_is_503_when_a_dependency_fails():
    resp = _client(_Broken()).get("/health/ready")
    assert resp.status_code == 503
    # Only configured components are listed, and never the reason.
    assert resp.json() == {"status": "not_ready", "checks": {"checkpointer": "fail"}}


def test_probe_is_public_unlimited_and_host_exempt():
    app = FastAPI()
    app.include_router(ping_router)
    assert find_unprotected_routes(app) == []
    assert "/health/ready" in HEALTH_CHECK_PATHS
    assert sm._is_health_check({"path": "/health/ready", "root_path": ""})
    assert sm._is_health_check({"path": "/api/health/ready", "root_path": "/api"})
    assert not sm._is_health_check({"path": "/v1/graph", "root_path": ""})


def test_k8s_template_uses_the_readiness_probe():
    from pathlib import Path

    from tenxgraph_api.cli.templates import defaults

    source = Path(defaults.__file__).read_text()
    assert '"              path: /health/ready",' in source


@pytest.mark.asyncio
async def test_results_are_cached_and_shared_by_concurrent_callers(monkeypatch):
    calls = []

    class _Counting:
        async def readiness_check(self):
            calls.append(1)
            await asyncio.sleep(0.01)

    probe = _Counting()
    results = await asyncio.gather(
        *(readiness.cached_readiness(probe, None, None) for _ in range(20))
    )
    assert all(r == {"checkpointer": "ok"} for r in results)
    assert len(calls) == 1  # a flood of probes is one round of checks
    await readiness.cached_readiness(probe, None, None)
    assert len(calls) == 1

    monkeypatch.setattr(readiness, "CACHE_SECONDS", 0.0)
    await readiness.cached_readiness(probe, None, None)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# schema version (G-164)
# ---------------------------------------------------------------------------


class _PgWithVersion(_PgLike):
    def __init__(self, found, expected=4):
        super().__init__()
        self._found, self._expected = found, expected

    async def aget_schema_version(self):
        return self._found

    def _get_current_schema_version(self):
        return self._expected


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [0, 3])
async def test_old_or_missing_schema_is_not_ready(found):
    checks = await readiness.readiness(_PgWithVersion(found), None, None)
    assert checks["checkpointer"] == "fail"


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [4, 5])
async def test_current_schema_is_ready(found):
    checks = await readiness.readiness(_PgWithVersion(found), None, None)
    assert checks["checkpointer"] == "ok"
