import importlib
import json
import logging
import sys
from types import SimpleNamespace

import pytest
from taskiq import InMemoryBroker
from taskiq.middlewares import SimpleRetryMiddleware

from tenxgraph_api.jobs import middleware as mw
from tenxgraph_api.jobs import monitoring


def load_worker(monkeypatch, tmp_path, data):
    """Import a fresh ``tenxgraph_api.jobs.worker`` reading a 10xgraph.json made from ``data``."""
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("GRAPH_PATH", str(path))
    sys.modules.pop("tenxgraph_api.jobs.worker", None)
    return importlib.import_module("tenxgraph_api.jobs.worker")


def test_no_worker_key_gives_memory_broker_and_warns(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.jobs"):
        worker = load_worker(monkeypatch, tmp_path, {"agent": "a:b"})
    assert isinstance(worker.broker, InMemoryBroker)
    assert sum("in-memory broker" in r.message for r in caplog.records) == 1
    assert not worker.broker.event_handlers


def test_missing_config_file_gives_memory_broker(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPH_PATH", str(tmp_path / "missing.json"))
    sys.modules.pop("tenxgraph_api.jobs.worker", None)
    worker = importlib.import_module("tenxgraph_api.jobs.worker")
    assert isinstance(worker.broker, InMemoryBroker)


def test_middlewares_in_order(monkeypatch, tmp_path):
    data = {"agent": "a:b", "worker": {"broker": {"type": "memory"}, "retries": 5}}
    worker = load_worker(monkeypatch, tmp_path, data)
    kinds = [type(m) for m in worker.broker.middlewares]
    assert kinds == [SimpleRetryMiddleware, monitoring.MonitoringMiddleware, mw.TaskMiddleware]
    assert worker.broker.middlewares[0].default_retry_count == 5


@pytest.mark.asyncio
async def test_startup_and_shutdown(monkeypatch, tmp_path):
    data = {"agent": "a:b", "worker": {"broker": {"type": "memory"}}}
    worker = load_worker(monkeypatch, tmp_path, data)
    closed = []

    class FakeGraph:
        async def aclose(self, **kwargs):
            closed.append(kwargs)

    async def fake_attach(config, container):
        return FakeGraph()

    from tenxgraph_api.src.app import loader

    monkeypatch.setattr(loader, "attach_all_modules", fake_attach)
    monkeypatch.setattr(loader, "load_container", lambda path: None)
    monkeypatch.setattr(worker, "wrap_tasks", lambda b: closed.append("wrapped"))
    monkeypatch.setenv("STARTUP_CHECKS", "false")
    from tenxgraph_api.src.app.core.config.settings import get_settings

    get_settings.cache_clear()

    state = SimpleNamespace()
    await worker.worker_startup(state)
    assert isinstance(state.graph, FakeGraph)
    assert "wrapped" in closed
    assert worker.broker.state.injectq_container is not None

    await worker.worker_shutdown(state)
    assert any(isinstance(c, dict) and "grace" in c for c in closed)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_shutdown_survives_failing_graph(monkeypatch, tmp_path):
    worker = load_worker(monkeypatch, tmp_path, {"agent": "a:b"})

    class BadGraph:
        async def aclose(self, **kwargs):
            raise RuntimeError("boom")

    await worker.worker_shutdown(SimpleNamespace(graph=BadGraph()))


def _patch_loader(monkeypatch, container=None, closed=None):
    from tenxgraph_api.src.app import loader
    from tenxgraph_api.src.app.core.config.settings import get_settings

    class FakeGraph:
        async def aclose(self, **kwargs):
            if closed is not None:
                closed.append(kwargs)

    async def fake_attach(config, cont):
        return FakeGraph()

    monkeypatch.setattr(loader, "attach_all_modules", fake_attach)
    monkeypatch.setattr(loader, "load_container", lambda path: container)
    monkeypatch.setenv("STARTUP_CHECKS", "false")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_shutdown_uses_container_from_startup(monkeypatch, tmp_path):
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore

    data = {"agent": "a:b", "worker": {"broker": {"type": "memory"}}}
    worker = load_worker(monkeypatch, tmp_path, data)
    mine = InjectQ()
    _patch_loader(monkeypatch, container=mine)
    monkeypatch.setattr(worker, "wrap_tasks", lambda b: None)
    state = SimpleNamespace()
    await worker.worker_startup(state)
    assert state.container is mine

    closed = []

    class FakeCache:
        async def aclose(self):
            closed.append("cache")

    mine.bind_instance(BaseCacheStore, FakeCache())
    await worker.worker_shutdown(state)
    assert closed == ["cache"]


def test_result_ttl_set_from_config(monkeypatch, tmp_path):
    from tenxgraph_api.jobs import state as job_state

    monkeypatch.setattr(job_state, "RESULT_TTL", 86400)
    data = {"agent": "a:b", "worker": {"broker": {"type": "memory"}, "result_ttl": 123}}
    load_worker(monkeypatch, tmp_path, data)
    assert job_state.RESULT_TTL == 123


@pytest.fixture(autouse=True)
def _restore_worker_module():
    yield
    sys.modules.pop("tenxgraph_api.jobs.worker", None)
