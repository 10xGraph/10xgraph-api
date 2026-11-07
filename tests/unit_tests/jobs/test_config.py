import pytest
import taskiq_aio_kafka.broker as kafka_module
from aiokafka.coordinator.assignors.roundrobin import RoundRobinPartitionAssignor
from aiokafka.partitioner import DefaultPartitioner
from pydantic import ValidationError
from taskiq import InMemoryBroker
from taskiq_aio_kafka import AioKafkaBroker
from taskiq_aio_pika import AioPikaBroker
from taskiq_redis import ListRedisScheduleSource, RedisAsyncResultBackend

from tenxgraph_api.jobs import config as jobs_config
from tenxgraph_api.jobs.config import (
    WorkerConfig,
    build_broker,
    build_result_backend,
    build_schedule_source,
)
from tenxgraph_api.jobs.redis_broker import ReclaimingRedisStreamBroker
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


def test_defaults():
    cfg = WorkerConfig()
    assert cfg.broker.type == "redis_stream"
    assert cfg.result_ttl == 86400
    assert cfg.retries == 3


@pytest.mark.parametrize("bad", [{"broker": {"type": "nope"}}, {"retries": -1}, {"typo": 1}])
def test_invalid_values_refused(bad):
    with pytest.raises(ValidationError):
        WorkerConfig.model_validate(bad)


def test_memory_broker_has_no_url_or_backend():
    cfg = WorkerConfig.model_validate({"broker": {"type": "memory"}})
    assert isinstance(build_broker(cfg), InMemoryBroker)
    assert build_result_backend(cfg) is None


def test_redis_stream_broker_gets_default_redis_backend():
    cfg = WorkerConfig.model_validate({"broker": {"url": "redis://h:1/0"}, "result_ttl": 50})
    broker = build_broker(cfg)
    assert isinstance(broker, ReclaimingRedisStreamBroker)
    assert isinstance(broker.result_backend, RedisAsyncResultBackend)
    assert broker.result_backend.result_ex_time == 50


def test_redis_url_expands_env_and_falls_back_to_redis_url(monkeypatch):
    monkeypatch.setenv("MY_REDIS", "redis://example:1/2")
    cfg = WorkerConfig.model_validate({"broker": {"url": "${MY_REDIS}"}})
    assert isinstance(build_broker(cfg), ReclaimingRedisStreamBroker)
    monkeypatch.delenv("MY_REDIS")
    with pytest.raises(ValueError, match="environment variable that is not set"):
        build_broker(cfg)
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(ValueError, match="worker.broker.url is required"):
        build_broker(WorkerConfig())


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"broker": {"type": "nope"}}, "Other brokers are not supported yet"),
        ({"broker": {"type": "my.mod:broker"}}, "Other brokers are not supported yet"),
        ({"result_backend": {"type": "my.mod:backend"}}, "Other result backends are not"),
        ({"result_backend": {"type": "postgres"}}, "Other result backends are not"),
    ],
)
def test_unsupported_types_and_module_attr_refused(section, message):
    with pytest.raises(ValidationError, match=message) as info:
        WorkerConfig.model_validate(section)
    assert "redis" in str(info.value) and "memory" in str(info.value)


def test_rabbitmq_broker_gets_amqp_url_options_and_redis_results(monkeypatch):
    monkeypatch.setenv("AMQP", "amqp://u:p@rabbit:5672/")
    monkeypatch.setenv("REDIS_URL", "redis://results:1/3")
    cfg = WorkerConfig.model_validate(
        {"broker": {"type": "rabbitmq", "url": "${AMQP}", "options": {"qos": 4}}}
    )
    broker = build_broker(cfg)
    assert isinstance(broker, AioPikaBroker)
    assert broker.url == "amqp://u:p@rabbit:5672/"
    assert broker._qos == 4
    assert isinstance(broker.result_backend, RedisAsyncResultBackend)
    assert _redis_host(broker.result_backend) == ("results", 1, 3)


def test_rabbitmq_and_kafka_need_a_url():
    for kind in ("rabbitmq", "kafka"):
        with pytest.raises(ValueError, match="worker.broker.url is required"):
            build_broker(WorkerConfig.model_validate({"broker": {"type": kind}}))


@pytest.fixture
def kafka_admin(monkeypatch):
    """The Kafka broker's constructor builds a KafkaAdminClient, which connects at once."""
    seen: list[dict] = []
    monkeypatch.setattr(kafka_module, "KafkaAdminClient", lambda **kw: seen.append(kw) or kw)
    return seen


def test_kafka_broker_splits_servers_and_sets_a_consumer_group(kafka_admin):
    cfg = WorkerConfig.model_validate(
        {
            "broker": {
                "type": "kafka",
                "url": "k1:9092, k2:9092",
                "options": {"kafka_topic": "jobs", "producer": {"acks": "all"}},
            },
            "result_backend": {"url": "redis://results:1/4"},
        }
    )
    broker = build_broker(cfg)
    assert isinstance(broker, AioKafkaBroker)
    assert broker._bootstrap_servers == ["k1:9092", "k2:9092"]
    assert kafka_admin[0]["bootstrap_servers"] == ["k1:9092", "k2:9092"]
    assert broker._kafka_topic.name == "jobs"
    consumer = broker._aiokafka_consumer_params
    assert consumer.group_id == "10xgraph"
    # aiokafka's own assignor and partitioner: taskiq-aio-kafka's kafka-python ones fail.
    assert consumer.partition_assignment_strategy == (RoundRobinPartitionAssignor,)
    assert isinstance(broker._aiokafka_producer_params.partitioner, DefaultPartitioner)
    assert broker._aiokafka_producer_params.acks == "all"
    assert _redis_host(broker.result_backend) == ("results", 1, 4)


def test_kafka_kicker_labels_do_not_stick_to_the_task(kafka_admin, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://env:3/2")
    broker = build_broker(WorkerConfig.model_validate({"broker": {"type": "kafka", "url": "k:1"}}))

    @broker.task(task_name="t")
    async def t() -> None: ...

    t.kicker().with_labels(tenxgraph_config='{"user_id": "a"}')
    assert "tenxgraph_config" not in t.labels


def test_kafka_consumer_options_override_the_group(kafka_admin, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://env:3/2")
    options = {"consumer": {"group_id": "mine", "auto_offset_reset": "earliest"}}
    cfg = WorkerConfig.model_validate(
        {"broker": {"type": "kafka", "url": "k:1", "options": options}}
    )
    params = build_broker(cfg)._aiokafka_consumer_params
    assert (params.group_id, params.auto_offset_reset) == ("mine", "earliest")


def _redis_host(target) -> tuple:
    pool = getattr(target, "redis_pool", None) or target._connection_pool
    kwargs = pool.connection_kwargs
    return kwargs["host"], kwargs["port"], kwargs["db"]


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        # Redis broker: result_backend.url wins, else the broker URL.
        ({"broker": {"url": "redis://b:1/0"}, "result_backend": {"url": "redis://r:2/1"}}, "r"),
        ({"broker": {"url": "redis://b:1/0"}}, "b"),
        ({"broker": {"url": "redis://b:1/0"}, "result_backend": {"type": "redis"}}, "b"),
        # RabbitMQ: never the broker URL; result_backend.url, else REDIS_URL.
        ({"broker": {"type": "rabbitmq", "url": "amqp://a/"}}, "env"),
        (
            {
                "broker": {"type": "rabbitmq", "url": "amqp://a/"},
                "result_backend": {"url": "redis://r:2/1"},
            },
            "r",
        ),
    ],
)
def test_results_and_schedules_share_one_redis_url(monkeypatch, section, expected):
    monkeypatch.setenv("REDIS_URL", "redis://env:3/2")
    cfg = WorkerConfig.model_validate(section)
    assert _redis_host(build_result_backend(cfg))[0] == expected
    assert _redis_host(build_schedule_source(cfg))[0] == expected


def test_rabbitmq_without_any_redis_url_is_refused(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    cfg = WorkerConfig.model_validate({"broker": {"type": "rabbitmq", "url": "amqp://a/"}})
    with pytest.raises(ValueError, match="worker.result_backend.url is required"):
        build_broker(cfg)


@pytest.mark.parametrize(
    "section",
    [
        {"broker": {"type": "memory"}},
        {"broker": {"type": "memory"}, "result_backend": {"type": "redis"}},
        {"broker": {"type": "rabbitmq", "url": "amqp://a/"}, "result_backend": {"type": "memory"}},
    ],
)
def test_no_redis_results_means_no_schedule_source(section):
    cfg = WorkerConfig.model_validate(section)
    assert build_result_backend(cfg) is None
    assert build_schedule_source(cfg) is None


def test_schedule_source_uses_json():
    cfg = WorkerConfig.model_validate({"broker": {"url": "redis://h:1/0"}})
    source = build_schedule_source(cfg)
    assert isinstance(source, ListRedisScheduleSource)
    assert type(source._serializer).__name__ == "JSONSerializer"


@pytest.mark.parametrize(
    ("kind", "extra"),
    [("redis_stream", "jobs"), ("rabbitmq", "jobs-rabbitmq"), ("kafka", "jobs-kafka")],
)
def test_missing_package_names_the_extra(monkeypatch, kind, extra):
    real = jobs_config.importlib.import_module
    missing = {
        "redis_stream": "taskiq_redis",
        "rabbitmq": "taskiq_aio_pika",
        "kafka": "taskiq_aio_kafka",
    }

    def fake(name):
        if name == missing[kind]:
            raise ImportError(name)
        return real(name)

    monkeypatch.setattr(jobs_config.importlib, "import_module", fake)
    cfg = WorkerConfig.model_validate({"broker": {"type": kind, "url": "x://h:1/0"}})
    with pytest.raises(RuntimeError, match=rf"10xgraph-api\[{extra}\]"):
        build_broker(cfg)


def test_graph_config_worker_property(tmp_path):
    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "a:b", "worker": {"broker": {"type": "memory"}, "retries": 1}}')
    cfg = GraphConfig(str(path)).worker
    assert cfg is not None and cfg.retries == 1
    path.write_text('{"agent": "a:b"}')
    assert GraphConfig(str(path)).worker is None
