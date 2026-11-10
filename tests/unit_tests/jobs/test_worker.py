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


METRICS_DATA = {
    "agent": "a:b",
    "worker": {"broker": {"type": "memory"}},
    "observability": {"otlp": {"enabled": True, "use_metrics_endpoint": True}},
    "metrics": {"enabled": True, "export": "otlp", "otlp": {"endpoint": "http://c:4318"}},
}


@pytest.mark.asyncio
async def test_worker_applies_observability_and_metrics_then_flushes(monkeypatch, tmp_path):
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.config import metrics as metrics_module
    from tenxgraph_api.src.app.core.config import setup_middleware as sm

    worker = load_worker(monkeypatch, tmp_path, METRICS_DATA)
    mine = InjectQ()
    order = []
    provider = object()
    roles = []

    class FakeGraph:
        async def aclose(self, **kwargs):
            order.append("graph closed")

    async def fake_attach(config, cont):
        order.append("graph built")
        return FakeGraph()

    from tenxgraph_api.src.app import loader
    from tenxgraph_api.src.app.core.config.settings import get_settings

    monkeypatch.setattr(loader, "attach_all_modules", fake_attach)
    monkeypatch.setattr(loader, "load_container", lambda path: mine)
    monkeypatch.setattr(worker, "wrap_tasks", lambda b: None)
    monkeypatch.setenv("STARTUP_CHECKS", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(
        metrics_module, "setup_metrics", lambda cfg, role: roles.append(role) or provider
    )
    monkeypatch.setattr(
        sm, "_setup_observability", lambda c, cfg: order.append(("observability", c is mine))
    )

    async def fake_flush(p):
        order.append(("telemetry flushed", p))

    monkeypatch.setattr(metrics_module, "flush_telemetry", fake_flush)

    class FakeCacheStore:
        async def aclose(self):
            order.append("cache store closed")

    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore

    state = SimpleNamespace()
    await worker.worker_startup(state)
    mine.bind_instance(BaseCacheStore, FakeCacheStore())
    await worker.worker_shutdown(state)

    assert roles == ["worker"]
    assert state.meter_provider is provider
    assert order == [
        ("observability", True),
        "graph built",
        "graph closed",
        "cache store closed",
        ("telemetry flushed", provider),  # last: a slow collector cuts nothing else
    ]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_worker_with_legacy_otel_and_metrics_installs_one_meter_provider(
    monkeypatch, tmp_path
):
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.config import metrics as metrics_module
    from tenxgraph_api.src.app.core.config import setup_middleware as sm
    from tenxgraph_api.src.app.core.config.settings import get_settings

    worker = load_worker(monkeypatch, tmp_path, METRICS_DATA)
    _patch_loader(monkeypatch, container=InjectQ())
    monkeypatch.setattr(worker, "wrap_tasks", lambda b: None)
    monkeypatch.setenv("OTEL_ENABLED", "true")
    get_settings.cache_clear()
    provider = object()
    monkeypatch.setattr(metrics_module, "setup_metrics", lambda cfg, role: provider)
    monkeypatch.setattr(sm, "_setup_observability", lambda c, cfg: None)
    monkeypatch.setattr(sm, "_attach_otel_publisher", lambda *a: None)
    seen = []
    monkeypatch.setattr(
        sm,
        "setup_otel_provider",
        lambda settings, *, metrics_owned=False: seen.append(metrics_owned),
    )

    await worker.worker_startup(SimpleNamespace())

    assert seen == [True]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_worker_without_metrics_block_sets_no_provider(monkeypatch, tmp_path):
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.config.settings import get_settings

    data = {"agent": "a:b", "worker": {"broker": {"type": "memory"}}}
    worker = load_worker(monkeypatch, tmp_path, data)
    _patch_loader(monkeypatch, container=InjectQ())
    monkeypatch.setattr(worker, "wrap_tasks", lambda b: None)
    state = SimpleNamespace()
    await worker.worker_startup(state)
    assert state.meter_provider is None
    await worker.worker_shutdown(state)
    get_settings.cache_clear()
