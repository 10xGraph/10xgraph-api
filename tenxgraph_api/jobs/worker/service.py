"""The ``10xgraph worker`` process: config, graph, worker loop, sweeper, probes, shutdown.

Startup and shutdown order: JOBS_DESIGN "Config" and "Worker behaviour".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tenxgraph_api.jobs.config.config import WorkerConfig, WorkerConfigError
from tenxgraph_api.jobs.config.factory import build_runtime, close_runtime, import_task_modules
from tenxgraph_api.jobs.core.models import PRIORITIES
from tenxgraph_api.jobs.core.runtime import JobsRuntime, set_runtime

from .health import HealthServer
from .sweeper import sweep_once
from .worker import Worker, check_reserve, default_worker_id


logger = logging.getLogger("tenxgraph_api.jobs")

#: Seconds past ``shutdown_timeout`` before the stragglers are abandoned (their leases expire).
SHUTDOWN_EXTRA = 15.0
#: Read block of the worker loop.
POLL_BLOCK = 1.0
DEFAULT_THREADS = 4
DEFAULT_PROCESSES = 2
#: Seconds the executors get to finish after the worker's own drain (then kill / abandon).
EXECUTOR_CLOSE_GRACE = 5.0


class WorkerOptions(BaseModel):
    """Command-line options of ``10xgraph worker``."""

    model_config = ConfigDict(extra="forbid")

    #: Queues to consume; None = every queue of the registered tasks plus ``default``.
    queues: list[str] | None = None
    concurrency: int = Field(default=16, ge=1)
    #: Slots of the thread / process executors. None: built with the default size (4 / 2)
    #: only when a task this worker consumes needs it.
    threads: int | None = Field(default=None, ge=1)
    processes: int | None = Field(default=None, ge=1)
    max_tasks_per_child: int | None = Field(default=None, ge=1)
    max_memory_per_child: float | None = Field(default=None, gt=0)
    reserve: dict[str, int] = Field(default_factory=dict)
    scheduler: bool = True
    #: None = ``worker.shutdown_timeout`` from the config.
    shutdown_timeout: float | None = Field(default=None, ge=0)
    metrics_port: int | None = Field(default=None, ge=0, le=65535)
    health_port: int | None = Field(default=None, ge=0, le=65535)
    #: ``--health-host``; set ``0.0.0.0`` for probes from the kubelet (off-host).
    health_host: str = "127.0.0.1"
    sweep_interval: float = Field(default=1.0, gt=0)
    worker_id: str | None = None


def parse_reserve(values: list[str] | None) -> dict[str, int]:
    """``["high=2"]`` -> ``{"high": 2}``. Raises ``ValueError`` with the bad entry."""
    out: dict[str, int] = {}
    for raw in values or []:
        level, sep, count = raw.partition("=")
        level = level.strip().lower()
        if not sep or level not in PRIORITIES or not count.strip().isdigit():
            raise ValueError(f"--reserve {raw!r}: write PRIORITY=N, for example high=2")
        out[level] = out.get(level, 0) + int(count)
    return out


async def load_worker_graph(graph_config: Any, container: Any) -> Any:
    """The ``agent`` graph, set up as the API does for running it: the configured
    ``id_generator`` and the ``store`` binding. None when no ``agent`` is set."""
    if not graph_config.data.get("agent"):
        return None
    from tenxgraph.storage.store import BaseStore

    from tenxgraph_api.src.app.loader import apply_id_generator, load_graph, load_store

    graph = await load_graph(graph_config.graph_path)
    if graph is not None:
        apply_id_generator(graph, graph_config, container)
    if container is not None and graph_config.store_path:
        container.bind_instance(BaseStore, load_store(graph_config.store_path), allow_none=True)
    return graph


class WorkerService:
    """One ``10xgraph worker`` process. ``start()`` then ``run()``; ``run()`` returns the exit
    code after a signal (or ``request_stop()``) and a drain."""

    def __init__(
        self,
        graph_config: Any,
        cfg: WorkerConfig,
        options: WorkerOptions,
        *,
        container: Any = None,
        base_dir: Path | None = None,
        graph: Any = None,
        load_graph: bool = True,
    ) -> None:
        self.graph_config = graph_config
        self.cfg = cfg
        self.options = options
        self.container = container
        self.base_dir = base_dir
        self.graph = graph
        self._load_graph = load_graph and graph is None
        self.runtime: JobsRuntime | None = None
        self.worker: Worker | None = None
        self.health: HealthServer | None = None
        self.draining = False
        self._stop = asyncio.Event()
        self._run_task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None
        self._signals: list[int] = []
        self.executors: dict[str, Any] = {}
        self.shutdown_timeout = (
            options.shutdown_timeout
            if options.shutdown_timeout is not None
            else cfg.shutdown_timeout
        )

    # ------------------------------------------------------------------ startup

    async def start(self) -> None:
        options = self.options
        check_reserve(options.reserve, options.concurrency)
        self._note_deferred_options()
        if self._load_graph:
            self.graph = await load_worker_graph(self.graph_config, self.container)
        import_task_modules(self.cfg.modules)
        worker_id = options.worker_id or default_worker_id()
        self.runtime = await build_runtime(
            self.cfg, base_dir=self.base_dir, allow_memory=False, consumer=worker_id
        )
        set_runtime(self.runtime)
        self.worker = Worker(
            self.runtime,
            queues=options.queues,
            concurrency=options.concurrency,
            worker_id=worker_id,
            lease_ttl=self.cfg.lease_ttl,
            reserve=options.reserve,
        )
        self.executors = await self._start_executors(self.worker.queues())
        self.worker.executors = dict(self.executors)
        if options.health_port is not None:
            self.health = HealthServer(
                alive=self.alive,
                ready=self.ready,
                host=options.health_host,
                port=options.health_port,
            )
            await self.health.start()
        logger.info(
            "jobs worker %s started: queues=%s concurrency=%d reserve=%s store=%s transport=%s",
            worker_id,
            ",".join(self.worker.queues()),
            options.concurrency,
            options.reserve or "-",
            self.cfg.store_type,
            self.cfg.transport_type,
        )

    async def _start_executors(self, queues: list[str]) -> dict[str, Any]:
        """The thread and process executors this worker needs: one when its flag is given, or
        when a registered task on one of ``queues`` uses it (default size)."""
        from tenxgraph_api.jobs.core import registry

        kinds = {t.executor for t in registry.all_tasks().values() if t.queue in queues}
        options = self.options
        built: dict[str, Any] = {}
        if options.threads is not None or "thread" in kinds:
            from tenxgraph_api.jobs.executors import ThreadExecutor

            built["thread"] = ThreadExecutor(options.threads or DEFAULT_THREADS)
        if options.processes is not None or "process" in kinds:
            from tenxgraph_api.jobs.executors import ProcessExecutor

            built["process"] = ProcessExecutor(
                options.processes or DEFAULT_PROCESSES,
                preload=list(self.cfg.modules),
                max_tasks_per_child=options.max_tasks_per_child,
                max_memory_per_child=options.max_memory_per_child,
                cancel_grace=5.0,
            )
        started: dict[str, Any] = {}
        try:
            for kind, executor in built.items():
                await executor.astart()
                started[kind] = executor
                logger.info("jobs %s executor: %d slots", kind, executor.capacity)
        except BaseException:
            for executor in started.values():
                await executor.aclose(grace=0)
            raise
        return started

    def _note_deferred_options(self) -> None:
        options = self.options
        if options.metrics_port is not None:
            logger.warning("--metrics-port is accepted; metrics arrive with jobs Sprint 6")
        if not options.scheduler:
            logger.info("--no-scheduler: this worker will not take part in cron (Sprint 4)")
        if self.cfg.store_type == "sqlite" and os.environ.get("MODE", "").lower() in (
            "production",
            "prod",
        ):
            logger.warning(
                'worker.backend "local" (SQLite) is for one machine in development; use '
                "Postgres or Redis with MODE=production"
            )

    # ------------------------------------------------------------------ probes

    def alive(self) -> bool:
        worker = self.worker
        task = self._run_task
        if worker is None or task is None or task.done():
            return False
        if worker.last_tick is None:
            return True
        return time.monotonic() - worker.last_tick < max(15.0, worker.lease_ttl)

    async def ready(self) -> bool:
        if self.draining or self.runtime is None or not self.alive():
            return False
        await self.runtime.store.now()
        await self.runtime.transport.setup()
        return True

    # ------------------------------------------------------------------ run and stop

    def request_stop(self, reason: str = "stop requested") -> None:
        if self._stop.is_set():
            logger.warning(
                "jobs worker already draining (%s); send SIGKILL to stop at once", reason
            )
            return
        logger.info("jobs worker: %s, draining", reason)
        self._stop.set()

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop, signal.Signals(sig).name)
            except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
                continue
            self._signals.append(sig)

    def _remove_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in self._signals:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)
        self._signals.clear()

    async def _sweep_loop(self) -> None:
        assert self.runtime is not None  # noqa: S101 - started
        interval = self.options.sweep_interval
        while True:
            try:
                stats = await sweep_once(self.runtime)
                if stats.lost or stats.purged:
                    logger.info(
                        "jobs sweeper: lost=%d published=%d purged=%d",
                        stats.lost,
                        stats.published,
                        stats.purged,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("jobs sweeper pass failed; retrying", exc_info=True)
            # Jitter spreads the passes of many pods (each pass is safe to repeat anyway).
            await asyncio.sleep(interval * random.uniform(0.8, 1.2))  # noqa: S311 - not crypto

    async def run(self, *, install_signals: bool = True) -> int:
        """Run until a signal or ``request_stop()``, then drain. Returns the exit code."""
        if self.worker is None:
            await self.start()
        assert self.worker is not None  # noqa: S101 - started
        if install_signals:
            self._install_signals()
        self._run_task = asyncio.create_task(self.worker.run(block=POLL_BLOCK), name="jobs-worker")
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="jobs-sweeper")
        stop_wait = asyncio.create_task(self._stop.wait())
        code = 0
        try:
            await asyncio.wait({stop_wait, self._run_task}, return_when=asyncio.FIRST_COMPLETED)
            if self._run_task.done() and not self._stop.is_set():
                code = 1
                exc = None if self._run_task.cancelled() else self._run_task.exception()
                logger.error("jobs worker loop ended unexpectedly: %r", exc)
        finally:
            stop_wait.cancel()
            await self.shutdown()
            if install_signals:
                self._remove_signals()
        return code

    async def shutdown(self) -> None:
        """Drain and close everything. Safe to call once ``start()`` ran (even partly)."""
        self.draining = True
        if self.worker is not None:
            await self._drain(self.worker)
        for kind, executor in self.executors.items():
            try:
                await executor.aclose(grace=EXECUTOR_CLOSE_GRACE)
            except Exception:
                logger.warning("jobs %s executor close failed", kind, exc_info=True)
        self.executors = {}
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            await asyncio.gather(self._sweep_task, return_exceptions=True)
        if self.health is not None:
            await self.health.aclose()
        if self.runtime is not None:
            await close_runtime(self.runtime)
            set_runtime(None)
        if self.graph is not None:
            try:
                stats = await self.graph.aclose()
                logger.debug("graph closed: %s", stats)
            except Exception:
                logger.warning("graph close did not finish cleanly", exc_info=True)
        logger.info("jobs worker stopped")

    async def _drain(self, worker: Worker) -> None:
        """Give running attempts ``shutdown_timeout`` s, abandon stragglers, end the loop."""
        logger.info(
            "jobs worker draining: %d runs in flight, waiting up to %.0f s",
            len(worker.inflight),
            self.shutdown_timeout,
        )
        try:
            await asyncio.wait_for(
                worker.aclose(grace=self.shutdown_timeout),
                self.shutdown_timeout + SHUTDOWN_EXTRA,
            )
        except TimeoutError:
            logger.error(
                "jobs worker: runs did not stop in time; abandoning them (their leases "
                "expire and another worker takes them)"
            )
            await worker.abort()
        if self._run_task is None:
            return
        try:
            await asyncio.wait_for(self._run_task, SHUTDOWN_EXTRA)
        except TimeoutError:
            self._run_task.cancel()
            await asyncio.gather(self._run_task, return_exceptions=True)
        except Exception:  # noqa: S110 - reported by run()
            pass


async def run_worker_process(
    graph_config: Any,
    options: WorkerOptions,
    *,
    container: Any = None,
    base_dir: Path | None = None,
) -> int:
    """What ``10xgraph worker`` runs inside ``asyncio.run``."""
    cfg = graph_config.worker
    if cfg is None:
        raise WorkerConfigError(
            'no "worker" section in 10xgraph.json: add one, for example {"worker": '
            '{"modules": "jobs", "backend": "local"}}'
        )
    service = WorkerService(graph_config, cfg, options, container=container, base_dir=base_dir)
    try:
        await service.start()
    except BaseException:
        await service.shutdown()
        raise
    return await service.run()
