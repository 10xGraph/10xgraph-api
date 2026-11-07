import pytest
from pydantic import ValidationError
from taskiq import InMemoryBroker
from taskiq_redis import ListQueueBroker, ListRedisScheduleSource, RedisAsyncResultBackend

from tenxgraph_api.jobs import config as jobs_config
from tenxgraph_api.jobs.config import (
    WorkerConfig,
    build_broker,
    build_result_backend,
    build_schedule_source,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


def test_defaults():
    cfg = WorkerConfig()
    assert cfg.broker.type == "redis_stream"
    assert cfg.result_ttl == 86400
    assert cfg.retries == 3
    assert cfg.scheduler_interval == 10
    assert cfg.schedules is False
    assert cfg.prometheus is None


@pytest.mark.parametrize("bad", [{"broker": {"type": "nope"}}, {"retries": -1}, {"typo": 1}])
def test_invalid_values_refused(bad):
    with pytest.raises(ValidationError):
        WorkerConfig.model_validate(bad)


def test_url_expands_env(monkeypatch):
    monkeypatch.setenv("MY_REDIS", "redis://example:1/2")
    cfg = WorkerConfig.model_validate({"broker": {"url": "${MY_REDIS}"}})
    assert cfg.broker_url() == "redis://example:1/2"


def test_unset_env_var_refused(monkeypatch):
    monkeypatch.delenv("NOT_SET_VAR", raising=False)
    cfg = WorkerConfig.model_validate({"broker": {"url": "${NOT_SET_VAR}"}})
    with pytest.raises(ValueError, match="environment variable that is not set"):
        cfg.broker_url()


def test_redis_url_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://fallback:6379/0")
    assert WorkerConfig().broker_url() == "redis://fallback:6379/0"
    monkeypatch.delenv("REDIS_URL")
    with pytest.raises(ValueError, match="worker.broker.url is required"):
        WorkerConfig().broker_url()


def test_memory_broker_has_no_url_or_backend():
    cfg = WorkerConfig.model_validate({"broker": {"type": "memory"}})
    assert isinstance(build_broker(cfg), InMemoryBroker)
    assert build_result_backend(cfg) is None


def test_redis_list_broker_gets_default_redis_backend():
    cfg = WorkerConfig.model_validate(
        {"broker": {"type": "redis_list", "url": "redis://h:1/0"}, "result_ttl": 50}
    )
    broker = build_broker(cfg)
    assert isinstance(broker, ListQueueBroker)
    assert isinstance(broker.result_backend, RedisAsyncResultBackend)
    assert broker.result_backend.result_ex_time == 50


def test_module_attr_broker_instance_and_class():
    cfg = WorkerConfig.model_validate(
        {
            "broker": {"type": "taskiq:InMemoryBroker"},
            "result_backend": {"type": "memory"},
        }
    )
    assert isinstance(build_broker(cfg), InMemoryBroker)


def test_schedule_source_uses_json(monkeypatch):
    cfg = WorkerConfig.model_validate({"broker": {"url": "redis://h:1/0"}})
    source = build_schedule_source(cfg)
    assert isinstance(source, ListRedisScheduleSource)
    assert type(source._serializer).__name__ == "JSONSerializer"


def test_missing_package_names_the_extra(monkeypatch):
    def boom(name):
        raise ImportError(name)

    monkeypatch.setattr(jobs_config.importlib, "import_module", boom)
    with pytest.raises(RuntimeError, match=r"10xgraph-api\[jobs\]"):
        build_broker(WorkerConfig())


def test_graph_config_worker_property(tmp_path):
    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "a:b", "worker": {"broker": {"type": "memory"}, "retries": 1}}')
    cfg = GraphConfig(str(path)).worker
    assert cfg is not None and cfg.retries == 1
    path.write_text('{"agent": "a:b"}')
    assert GraphConfig(str(path)).worker is None
