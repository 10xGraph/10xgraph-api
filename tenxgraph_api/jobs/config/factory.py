"""Build the jobs runtime (store, transport, hooks) from the ``worker`` section.

Only called when ``10xgraph.json`` has a ``worker`` section, so without one no table, file or
Redis key is ever created. Own-storage rules (JOBS_PLAN "Production extras"):

- Redis store and transport use database ``worker.redis_db`` (default 1, not the cache
  store's usual 0). A server that refuses it (Redis Cluster, some managed services) falls back
  to database 0 with every key under ``10xgraph:jobs:``, logged (``store/redis.py``).
- Postgres gets its own DSN (``worker.store.url``) and a small pool (``min_size=1``,
  ``max_size=5``); ``auto_migrate`` creates or upgrades the ``tenxgraph_task_*`` tables under
  an advisory lock, false refuses tables older than the code.
- SQLite (``backend: "local"``) uses one file, ``.10xgraph/jobs.db`` next to 10xgraph.json,
  for both the store and the transport.
- ``memory`` works only inside one process (tests, scripts); the API server and
  ``10xgraph worker`` refuse it (``allow_memory=False``).
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from tenxgraph_api.jobs.core import registry
from tenxgraph_api.jobs.core.runtime import JobsRuntime, get_runtime, set_runtime
from tenxgraph_api.jobs.store.base import BaseRunStore
from tenxgraph_api.jobs.transport.base import BaseTransport

from .config import WorkerConfig, WorkerConfigError


logger = logging.getLogger("tenxgraph_api.jobs")

#: Pool of the Postgres run store: small, it is a second pool next to the app's own.
POSTGRES_POOL = {"min_size": 1, "max_size": 5}


def _resolve(path: str, base_dir: Path | None) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute() and base_dir is not None:
        target = base_dir / target
    return target


def _refuse_memory(cfg: WorkerConfig) -> None:
    if cfg.store_type == "memory" or cfg.transport_type == "memory":
        raise WorkerConfigError(
            'worker "memory" store and transport work only inside one process (tests, '
            "scripts): the API server and `10xgraph worker` are separate processes. Use "
            '"backend": "local" for local development'
        )


def build_store(cfg: WorkerConfig, *, base_dir: Path | None = None) -> BaseRunStore:
    """The run store ``cfg`` names (not set up yet)."""
    kind = cfg.store_type
    if kind == "sqlite":
        from tenxgraph_api.jobs.store.sqlite import SqliteRunStore

        return SqliteRunStore(_resolve(cfg.store_path, base_dir))
    if kind == "memory":
        from tenxgraph_api.jobs.store.memory import MemoryRunStore

        return MemoryRunStore()
    store_cfg = cfg.store
    assert store_cfg is not None  # noqa: S101 - backend local handled above
    url = store_cfg.expanded_url()
    if kind == "postgres":
        try:
            import asyncpg  # noqa: F401 - checked here for a clear message
        except ImportError as exc:
            raise WorkerConfigError(
                'worker.store "postgres" needs asyncpg: pip install "10xgraph-api[jobs]"'
            ) from exc
        from tenxgraph_api.jobs.store.postgres import PostgresRunStore

        return PostgresRunStore(
            dsn=url,
            schema=store_cfg.db_schema,
            pool_config=dict(POSTGRES_POOL),
            auto_migrate=store_cfg.auto_migrate,
        )
    _require_redis("worker.store")
    from tenxgraph_api.jobs.store.redis import RedisRunStore

    return RedisRunStore(url=url, db=cfg.redis_db)


def build_transport(
    cfg: WorkerConfig, *, base_dir: Path | None = None, consumer: str | None = None
) -> BaseTransport:
    """The transport ``cfg`` names (not set up yet). ``consumer`` is the Redis consumer name
    (the worker id), so ``XGROUP DELCONSUMER`` on close removes this worker's entry."""
    kind = cfg.transport_type
    if kind == "sqlite":
        from tenxgraph_api.jobs.transport.sqlite import SqliteTransport

        return SqliteTransport(_resolve(cfg.transport_path, base_dir))
    if kind == "memory":
        from tenxgraph_api.jobs.transport.memory import MemoryTransport

        return MemoryTransport()
    transport_cfg = cfg.transport
    assert transport_cfg is not None  # noqa: S101 - backend local handled above
    url = transport_cfg.expanded_url()
    _require_redis("worker.transport")
    from tenxgraph_api.jobs.transport.redis import RedisStreamsTransport

    return RedisStreamsTransport(url=url, db=cfg.redis_db, consumer=consumer)


def _require_redis(where: str) -> None:
    try:
        import redis  # noqa: F401 - checked here for a clear message
    except ImportError as exc:
        raise WorkerConfigError(
            f'{where} "redis" needs the redis package: pip install "10xgraph-api[redis]"'
        ) from exc


def _import_attr(path: str) -> Any:
    module_name, _, attr = path.partition(":")
    module = importlib.import_module(module_name)
    obj: Any = module
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def load_hooks(paths: list[str]) -> list[Any]:
    """``worker.hooks`` entries: a ``TaskLifecycleHook`` class (built with no arguments) or
    instance, by ``"module:attr"``. An import error refuses the boot."""
    hooks: list[Any] = []
    for path in paths:
        try:
            obj = _import_attr(path)
        except (ImportError, AttributeError) as exc:
            raise WorkerConfigError(f"worker.hooks: cannot load {path!r}: {exc}") from exc
        hooks.append(obj() if isinstance(obj, type) else obj)
    return hooks


def import_task_modules(modules: list[str]) -> int:
    """Import ``worker.modules`` so every ``@task`` registers; returns how many tasks exist.

    An import error refuses the boot (with the module named); zero tasks logs a warning.
    """
    for name in modules:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            raise WorkerConfigError(f"worker.modules: cannot import {name!r}: {exc}") from exc
    count = len(registry.all_tasks())
    if count == 0:
        logger.warning(
            "jobs: no tasks are registered after importing worker.modules %s; add @task "
            "functions to them",
            modules,
        )
    return count


async def build_runtime(
    cfg: WorkerConfig,
    *,
    base_dir: Path | None = None,
    allow_memory: bool = True,
    consumer: str | None = None,
    setup: bool = True,
) -> JobsRuntime:
    """Store, transport and hooks from ``cfg``, set up (tables, file, connection) unless
    ``setup`` is false. On a setup error both are closed again and the error raised."""
    if not allow_memory:
        _refuse_memory(cfg)
    hooks = load_hooks(cfg.hooks)
    store = build_store(cfg, base_dir=base_dir)
    try:
        transport = build_transport(cfg, base_dir=base_dir, consumer=consumer)
    except BaseException:
        await store.aclose()
        raise
    if setup:
        try:
            await store.setup()
            await transport.setup()
        except BaseException:
            await _close_quietly(transport, store)
            raise
    logger.info(
        "Jobs: store %s, transport %s%s",
        type(store).__name__,
        type(transport).__name__,
        f" (redis db {cfg.redis_db})" if "redis" in (cfg.store_type, cfg.transport_type) else "",
    )
    return JobsRuntime(
        store=store,
        transport=transport,
        hooks=hooks,
        result_ttl=cfg.result_ttl,
        lease_ttl=cfg.lease_ttl,
    )


async def _close_quietly(transport: BaseTransport, store: BaseRunStore) -> None:
    for closer in (transport.aclose, store.aclose):
        try:
            await closer()
        except Exception:
            logger.warning("jobs: closing %s failed", closer.__qualname__, exc_info=True)


async def close_runtime(runtime: JobsRuntime) -> None:
    """Close the transport (a Redis worker leaves its consumer groups) then the store."""
    await _close_quietly(runtime.transport, runtime.store)


# ---------------------------------------------------------------------- API process


async def start_api_jobs(graph_config: Any, *, base_dir: Path | None = None) -> JobsRuntime | None:
    """API side, at startup: with a ``worker`` section, import ``worker.modules`` (so enqueue
    by name works), build the store and transport for enqueue only (no worker loop) and bind
    them process-wide. Without one: None, and nothing is imported or created."""
    cfg = graph_config.worker
    if cfg is None:
        return None
    count = import_task_modules(cfg.modules)
    runtime = await build_runtime(cfg, base_dir=base_dir, allow_memory=False)
    set_runtime(runtime)
    logger.info("Jobs: %d tasks, enqueue only (tasks run in `10xgraph worker`)", count)
    return runtime


async def stop_api_jobs(runtime: JobsRuntime | None) -> None:
    """API side, at shutdown: unbind and close what ``start_api_jobs`` built."""
    if runtime is None:
        return
    try:
        if get_runtime() is runtime:
            set_runtime(None)
    except Exception:  # noqa: S110 - nothing bound
        pass
    await close_runtime(runtime)
