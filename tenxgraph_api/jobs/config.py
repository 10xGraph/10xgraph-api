"""The ``worker`` key of 10xgraph.json and the factories that turn it into TaskIQ objects."""

import importlib
import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tenxgraph_api.src.app.core.config.graph_config import _expand_env


BROKER_TYPES = ("redis_stream", "redis_list", "memory")
RESULT_BACKEND_TYPES = ("redis", "memory")
KEY = "worker.broker.url"
INSTALL_HINT = 'The jobs layer needs TaskIQ: pip install "10xgraph-api[jobs]"'


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrokerConfig(_Section):
    """``worker.broker``: ``type`` is a built-in name or ``"module:attr"``."""

    type: str = "redis_stream"
    url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_type(self) -> "BrokerConfig":
        if self.type not in BROKER_TYPES and ":" not in self.type:
            raise ValueError(
                f'worker.broker.type must be one of {", ".join(BROKER_TYPES)} or "module:attr"; '
                f"got {self.type!r}"
            )
        return self

    @property
    def is_redis(self) -> bool:
        return self.type in ("redis_stream", "redis_list")


class ResultBackendConfig(_Section):
    """``worker.result_backend``; when omitted, Redis on the broker URL for a Redis broker."""

    type: str = "redis"
    url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_type(self) -> "ResultBackendConfig":
        if self.type not in RESULT_BACKEND_TYPES and ":" not in self.type:
            raise ValueError(
                f'worker.result_backend.type must be one of {", ".join(RESULT_BACKEND_TYPES)} '
                f'or "module:attr"; got {self.type!r}'
            )
        return self


class PrometheusConfig(_Section):
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 9000


class WorkerConfig(_Section):
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    result_backend: ResultBackendConfig | None = None
    tasks: list[str] = Field(default_factory=list)
    schedules: bool = False
    result_ttl: int = Field(default=86400, gt=0)
    retries: int = Field(default=3, ge=0)
    scheduler_interval: int = Field(default=10, gt=0)
    prometheus: PrometheusConfig | None = None

    def broker_url(self) -> str | None:
        """The broker URL with ``${VAR}`` expanded; ``REDIS_URL`` for a Redis broker without one."""
        if self.broker.is_redis:
            return _redis_url(self.broker.url, "worker.broker.url")
        return None if self.broker.url is None else _expand(self.broker.url, "worker.broker.url")


def _expand(raw: str, key: str) -> str:
    try:
        return _expand_env(raw) or raw
    except ValueError:
        raise ValueError(f"{key}: {raw} uses an environment variable that is not set") from None


def _redis_url(raw: str | None, key: str) -> str:
    """``raw`` with ``${VAR}`` expanded, else the ``REDIS_URL`` variable."""
    if raw is not None:
        return _expand(raw, key)
    env_url = os.environ.get("REDIS_URL", "").strip()
    if not env_url:
        raise ValueError(f"{key} is required: set it in 10xgraph.json or set REDIS_URL")
    return env_url


def _require(package: str) -> None:
    try:
        importlib.import_module(package)
    except ImportError:
        raise RuntimeError(INSTALL_HINT) from None


def _load(path: str) -> Any:
    module, attr = path.split(":", 1)
    return getattr(importlib.import_module(module), attr)


def _make(target: Any, url: str | None, options: dict[str, Any]) -> Any:
    """A class is called with the URL (when there is one) and the options; an instance is kept."""
    if not isinstance(target, type):
        return target
    return target(url, **options) if url else target(**options)


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
    elif kind == "redis_list":
        _require("taskiq_redis")
        from taskiq_redis import ListQueueBroker

        broker = ListQueueBroker(_redis_url(cfg.broker.url, KEY), **options)
    else:
        broker = _make(_load(kind), cfg.broker_url(), options)
    backend = build_result_backend(cfg)
    return broker if backend is None else broker.with_result_backend(backend)


def build_result_backend(cfg: WorkerConfig) -> Any | None:
    """The result backend for ``cfg``; None when the broker stores its own results."""
    section = cfg.result_backend
    if section is None:
        if not cfg.broker.is_redis:
            return None
        section = ResultBackendConfig()
    if section.type == "memory":
        return None
    options = dict(section.options)
    if section.type == "redis":
        _require("taskiq_redis")
        from taskiq_redis import RedisAsyncResultBackend

        url = _redis_url(section.url or cfg.broker.url, "worker.result_backend.url")
        options.setdefault("result_ex_time", cfg.result_ttl)
        return RedisAsyncResultBackend(url, **options)
    other = section.url and _expand(section.url, "worker.result_backend.url")
    return _make(_load(section.type), other, options)


def build_schedule_source(cfg: WorkerConfig) -> Any:
    """The Redis schedule source (JSON, not the default pickle) for dynamic schedules."""
    _require("taskiq_redis")
    from taskiq.serializers import JSONSerializer
    from taskiq_redis import ListRedisScheduleSource

    url = _redis_url(cfg.broker.url, "worker.broker.url")
    return ListRedisScheduleSource(url, serializer=JSONSerializer())
