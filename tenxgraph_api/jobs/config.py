"""The ``worker`` key of 10xgraph.json and the factories that turn it into TaskIQ objects."""

import importlib
import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tenxgraph_api.src.app.core.config.graph_config import _expand_env


BROKER_TYPES = ("redis_stream", "rabbitmq", "kafka", "memory")
RESULT_BACKEND_TYPES = ("redis", "memory")
KEY = "worker.broker.url"
RESULT_KEY = "worker.result_backend.url"
# The pip extra that installs each TaskIQ package.
EXTRAS = {
    "taskiq": "jobs",
    "taskiq_redis": "jobs",
    "taskiq_aio_pika": "jobs-rabbitmq",
    "taskiq_aio_kafka": "jobs-kafka",
}
GLOB_MARKS = ("*", "?", "[", "/")


def is_glob(entry: str) -> bool:
    """A ``worker.tasks`` entry that is a file path or glob, not a module name."""
    return entry.endswith(".py") or any(mark in entry for mark in GLOB_MARKS)


def _check_type(key: str, value: str, allowed: tuple[str, ...], what: str) -> None:
    if value not in allowed:
        raise ValueError(
            f"{key} must be one of {', '.join(allowed)}; got {value!r}. "
            f"Other {what} are not supported yet."
        )


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrokerConfig(_Section):
    """``worker.broker``. ``url``: a Redis URL, an AMQP URL (rabbitmq) or the Kafka bootstrap
    servers, comma-separated. ``options`` go to the broker class."""

    type: str = "redis_stream"
    url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "BrokerConfig":
        _check_type("worker.broker.type", self.type, BROKER_TYPES, "brokers")
        return self


class ResultBackendConfig(_Section):
    """``worker.result_backend``; when omitted, Redis (RabbitMQ and Kafka store no results)."""

    type: str = "redis"
    url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "ResultBackendConfig":
        _check_type(
            "worker.result_backend.type", self.type, RESULT_BACKEND_TYPES, "result backends"
        )
        return self


class WorkerConfig(_Section):
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    result_backend: ResultBackendConfig | None = None
    tasks: list[str] = Field(default_factory=list)
    result_ttl: int = Field(default=86400, gt=0)
    retries: int = Field(default=3, ge=0)


def _expand(raw: str, key: str) -> str:
    try:
        return _expand_env(raw) or raw
    except ValueError:
        raise ValueError(f"{key}: {raw} uses an environment variable that is not set") from None


def _url(raw: str | None, key: str) -> str:
    if raw is None:
        raise ValueError(f"{key} is required: set it in 10xgraph.json")
    return _expand(raw, key)


def _redis_url(raw: str | None, key: str) -> str:
    """``raw`` with ``${VAR}`` expanded, else the ``REDIS_URL`` variable."""
    if raw is not None:
        return _expand(raw, key)
    env_url = os.environ.get("REDIS_URL", "").strip()
    if not env_url:
        raise ValueError(f"{key} is required: set it in 10xgraph.json or set REDIS_URL")
    return env_url


def _results_url(cfg: "WorkerConfig") -> str:
    """The Redis URL for results and schedules: ``result_backend.url``, else the broker URL
    for a Redis broker, else ``REDIS_URL``."""
    raw = cfg.result_backend.url if cfg.result_backend else None
    if raw is None and cfg.broker.type == "redis_stream":
        raw = cfg.broker.url
    return _redis_url(raw, RESULT_KEY)


def _redis_results(cfg: "WorkerConfig") -> bool:
    if cfg.broker.type == "memory":
        return False
    return cfg.result_backend is None or cfg.result_backend.type == "redis"


def _require(package: str) -> None:
    try:
        importlib.import_module(package)
    except ImportError:
        raise RuntimeError(
            f'The jobs layer needs {package}: pip install "10xgraph-api[{EXTRAS[package]}]"'
        ) from None


def build_broker(cfg: WorkerConfig) -> Any:
    """The TaskIQ broker for ``cfg``, with its result backend attached."""
    _require("taskiq")
    kind, options = cfg.broker.type, dict(cfg.broker.options)
    broker: Any
    if kind == "memory":
        from taskiq import InMemoryBroker

        return InMemoryBroker(**options)
    if kind == "redis_stream":
        _require("taskiq_redis")
        from tenxgraph_api.jobs.redis_broker import ReclaimingRedisStreamBroker

        options.setdefault("unacknowledged_lock_timeout", 60)
        broker = ReclaimingRedisStreamBroker(_redis_url(cfg.broker.url, KEY), **options)
    elif kind == "rabbitmq":
        _require("taskiq_aio_pika")
        from taskiq_aio_pika import AioPikaBroker

        broker = AioPikaBroker(_url(cfg.broker.url, KEY), **options)
    else:
        _require("taskiq_aio_kafka")
        from aiokafka.coordinator.assignors.roundrobin import RoundRobinPartitionAssignor
        from aiokafka.partitioner import DefaultPartitioner
        from taskiq import AsyncTaskiqDecoratedTask
        from taskiq_aio_kafka import AioKafkaBroker

        # Client settings go through configure_*. One consumer group, or every worker runs
        # every task. aiokafka's own assignor and partitioner: the kafka-python ones that
        # taskiq-aio-kafka 1.0.0 picks by default fail with aiokafka 0.15.
        consumer = {
            "group_id": "10xgraph",
            "partition_assignment_strategy": (RoundRobinPartitionAssignor,),
            **options.pop("consumer", {}),
        }
        producer = {"partitioner": DefaultPartitioner(), **options.pop("producer", {})}
        servers = [part.strip() for part in _url(cfg.broker.url, KEY).split(",")]
        broker = AioKafkaBroker([part for part in servers if part], **options)
        broker.configure_consumer(**consumer)
        broker.configure_producer(**producer)
        # AioKafkaDecoratedTask.kicker() shares the task's labels dict, so a config label set
        # with with_labels() would stick to the task; TaskIQ's own class copies it.
        broker.decorator_class = AsyncTaskiqDecoratedTask
    backend = build_result_backend(cfg)
    return broker if backend is None else broker.with_result_backend(backend)


def build_result_backend(cfg: WorkerConfig) -> Any | None:
    """The Redis result backend for ``cfg``; None for ``memory`` (results stay in process)."""
    if not _redis_results(cfg):
        return None
    _require("taskiq_redis")
    from taskiq_redis import RedisAsyncResultBackend

    options = dict(cfg.result_backend.options) if cfg.result_backend else {}
    options.setdefault("result_ex_time", cfg.result_ttl)
    return RedisAsyncResultBackend(_results_url(cfg), **options)


def build_schedule_source(cfg: WorkerConfig) -> Any | None:
    """The Redis schedule source (JSON, not the default pickle) for dynamic schedules, on the
    result backend's Redis; None without a Redis result backend."""
    if not _redis_results(cfg):
        return None
    _require("taskiq_redis")
    from taskiq.serializers import JSONSerializer
    from taskiq_redis import ListRedisScheduleSource

    return ListRedisScheduleSource(_results_url(cfg), serializer=JSONSerializer())
