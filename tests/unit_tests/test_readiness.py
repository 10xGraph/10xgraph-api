"""GET /health/ready checks the dependencies a request needs."""

# ruff: noqa: S101

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.storage.store import BaseStore

from tenxgraph_api.src.app.core.auth.route_guard import find_unprotected_routes
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore
from tenxgraph_api.src.app.core.config import setup_middleware as sm
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.middleware.rate_limit.middleware import HEALTH_CHECK_PATHS
from tenxgraph_api.src.app.routers.ping import readiness
from tenxgraph_api.src.app.routers.ping.router import router as ping_router


class _Broken:
    async def aping(self):
        raise ConnectionError("postgres://user:secret@db is down")


class _Pinged:
    def __init__(self):
        self.pings = 0

    async def aping(self):
        self.pings += 1


class _DownCache(InMemoryCacheStore):
    async def aping(self):
        raise ConnectionError("redis down")


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_configured_is_ready():
    assert await readiness.readiness(None, None, None) == {
        "checkpointer": "skipped",
        "store": "skipped",
        "cache": "skipped",
    }


@pytest.mark.asyncio
async def test_in_memory_components_answer_their_aping():
    checks = await readiness.readiness(InMemoryCheckpointer(), None, InMemoryCacheStore())
    assert checks == {"checkpointer": "ok", "store": "skipped", "cache": "ok"}


@pytest.mark.asyncio
async def test_failing_checkpointer_fails_without_leaking_the_reason(caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("tenxgraph_api"), "propagate", True)
    checks = await readiness.readiness(_Broken(), None, None)
    assert checks["checkpointer"] == "fail"
    assert "secret" in caplog.text  # logged for operators ...
    assert "secret" not in str(checks)  # ... never returned


@pytest.mark.asyncio
async def test_only_aping_is_called_on_each_component():
    checkpointer, store, cache = _Pinged(), _Pinged(), _Pinged()
    checks = await readiness.readiness(checkpointer, store, cache)
    assert checks == {"checkpointer": "ok", "store": "ok", "cache": "ok"}
    assert checkpointer.pings == store.pings == cache.pings == 1


@pytest.mark.asyncio
async def test_component_without_aping_is_skipped():
    assert (await readiness.readiness(None, object(), None))["store"] == "skipped"


@pytest.mark.asyncio
async def test_slow_dependency_times_out(monkeypatch):
    monkeypatch.setattr(readiness, "CHECK_TIMEOUT_SECONDS", 0.05)

    class _Hangs:
        async def aping(self):
            await asyncio.sleep(5)

    assert (await readiness.readiness(_Hangs(), None, None))["checkpointer"] == "fail"


def test_cache_counts_only_when_a_fail_closed_feature_needs_it():
    checks = {"checkpointer": "ok", "cache": "fail"}
    assert readiness.is_ready(checks, cache_is_required=False)
    assert not readiness.is_ready(checks, cache_is_required=True)
    assert not readiness.is_ready({"checkpointer": "fail", "cache": "ok"}, cache_is_required=False)
    assert not readiness.is_ready({"store": "fail"}, cache_is_required=False)

    on = SimpleNamespace(token_revocation=object(), rate_limit=None)
    assert readiness.cache_required_by(on) == ["token_revocation"]
    strict = SimpleNamespace(token_revocation=None, rate_limit=SimpleNamespace(fail_open=False))
    assert readiness.cache_required_by(strict) == ["rate_limit.fail_open=false"]
    lenient = SimpleNamespace(token_revocation=None, rate_limit=SimpleNamespace(fail_open=True))
    assert not readiness.cache_required(lenient)
    assert not readiness.cache_required(SimpleNamespace(token_revocation=None, rate_limit=None))
    assert not readiness.cache_required(None)


# ---------------------------------------------------------------------------
# the endpoint
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cache():
    readiness.reset_cache()
    yield
    readiness.reset_cache()


def _client(checkpointer=None, store=None, cache=None, config=None) -> TestClient:
    container = InjectQ()
    if checkpointer is not None:
        container.bind_instance(BaseCheckpointer, checkpointer)
    if store is not None:
        container.bind_instance(BaseStore, store)
    if cache is not None:
        container.bind_instance(BaseCacheStore, cache)
    if config is not None:
        container.bind_instance(GraphConfig, config)
    app = FastAPI()
    setup_fastapi(container, app)
    app.include_router(ping_router)
    return TestClient(app)


def test_endpoint_is_200_when_ready():
    resp = _client(InMemoryCheckpointer(), cache=InMemoryCacheStore()).get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": {"checkpointer": "ok", "cache": "ok"}}


def test_cache_outage_is_reported_and_fails_only_when_required(tmp_path):
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps({"agent": "graph.agent:app"}))
    resp = _client(InMemoryCheckpointer(), cache=_DownCache(), config=GraphConfig(str(path)))
    assert resp.get("/health/ready").status_code == 200
    assert resp.get("/health/ready").json()["checks"]["cache"] == "fail"

    readiness.reset_cache()
    path.write_text(json.dumps({"agent": "graph.agent:app", "token_revocation": True}))
    resp = _client(InMemoryCheckpointer(), cache=_DownCache(), config=GraphConfig(str(path)))
    assert resp.get("/health/ready").status_code == 503


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
        async def aping(self):
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
