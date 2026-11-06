"""``10xgraph worker``: run background tasks (the ``worker`` section of 10xgraph.json).

Loads the project the way the API server does (config, env file, ``MODE`` check, logging,
the InjectQ container, the ``observability`` setup, the graph), imports ``worker.modules``,
opens the jobs store and transport, then runs the worker loop and the sweeper until SIGTERM
or SIGINT, and drains (see ``tenxgraph_api/jobs/service.py``).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.constants import DEFAULT_CONFIG_FILE
from tenxgraph_api.cli.core.config import ConfigManager
from tenxgraph_api.cli.exceptions import ConfigurationError, ValidationError


class WorkerCommand(BaseCommand):
    """Run ``10xgraph worker``."""

    def execute(  # noqa: PLR0913 - one argument per CLI flag
        self,
        config: str = DEFAULT_CONFIG_FILE,
        queues: list[str] | None = None,
        concurrency: int = 16,
        threads: int | None = None,
        processes: int | None = None,
        max_tasks_per_child: int | None = None,
        max_memory_per_child: float | None = None,
        reserve: list[str] | None = None,
        scheduler: bool = True,
        shutdown_timeout: float | None = None,
        metrics_port: int | None = None,
        health_port: int | None = None,
        **kwargs: Any,
    ) -> int:
        try:
            from tenxgraph_api.jobs.config import WorkerConfigError
            from tenxgraph_api.jobs.service import WorkerOptions, parse_reserve

            try:
                options = WorkerOptions(
                    queues=list(dict.fromkeys(queues)) if queues else None,
                    concurrency=concurrency,
                    threads=threads,
                    processes=processes,
                    max_tasks_per_child=max_tasks_per_child,
                    max_memory_per_child=max_memory_per_child or None,
                    reserve=parse_reserve(reserve),
                    scheduler=scheduler,
                    shutdown_timeout=shutdown_timeout,
                    metrics_port=metrics_port,
                    health_port=health_port,
                )
                from tenxgraph_api.jobs.worker import check_reserve

                check_reserve(options.reserve, options.concurrency)
            except ValueError as exc:
                raise ValidationError(str(exc), field="reserve") from exc

            graph_config, config_path = self._load_project(config)
            try:
                cfg = graph_config.worker
            except WorkerConfigError as exc:
                raise ConfigurationError(str(exc), config_path=str(config_path)) from exc
            if cfg is None:
                raise ConfigurationError(
                    'No "worker" section in the config. Add one, for example "worker": '
                    '{"modules": "jobs", "backend": "local"}',
                    config_path=str(config_path),
                )
            container = self._load_container(graph_config)
            self.output.info(
                f"Starting 10xGraph worker ({config_path}); Ctrl+C or SIGTERM drains and stops",
                emoji=False,
            )
            from tenxgraph_api.jobs.service import run_worker_process

            try:
                return asyncio.run(
                    run_worker_process(
                        graph_config,
                        options,
                        container=container,
                        base_dir=config_path.parent,
                    )
                )
            except WorkerConfigError as exc:
                raise ConfigurationError(str(exc), config_path=str(config_path)) from exc
        except Exception as e:
            return self.handle_error(e)

    def _load_project(self, config: str) -> tuple[Any, Path]:
        manager = ConfigManager()
        config_path = manager.find_config_file(config)
        manager.load_config(str(config_path))
        env_file = manager.resolve_env_file()
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        from tenxgraph_api.src.app.core.config.settings import normalize_mode_value

        try:
            normalize_mode_value(os.environ.get("MODE"))
        except ValueError as e:
            raise ConfigurationError(str(e)) from e
        os.environ["GRAPH_PATH"] = str(config_path)
        root = str(config_path.resolve().parent)
        if root not in sys.path:
            sys.path.insert(0, root)

        from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

        try:
            graph_config = GraphConfig(str(config_path))
        except ValueError as exc:
            raise ConfigurationError(str(exc), config_path=str(config_path)) from exc
        return graph_config, config_path

    def _load_container(self, graph_config: Any) -> Any:
        """The project's InjectQ container (or the global one), with the same observability
        setup as the API so task and graph spans go to the same exporter."""
        from injectq import InjectQ

        from tenxgraph_api.src.app.core import get_settings, init_logger
        from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
        from tenxgraph_api.src.app.core.config.setup_middleware import _setup_observability
        from tenxgraph_api.src.app.loader import load_container

        init_logger(get_settings().LOG_LEVEL)
        container = load_container(graph_config.injectq_path) or InjectQ.get_instance()
        container.bind_instance(GraphConfig, graph_config)
        _setup_observability(container, graph_config)
        return container
