"""The API side of the broker: import the task modules and start the broker for sending.

Called from the lifespan only when 10xgraph.json has a ``worker`` key. The API never runs the
worker's startup: ``broker.startup()`` fires ``WORKER_STARTUP`` only in a worker process. The
in-memory broker is the exception: it runs tasks inside the API and its ``startup()`` fires
the worker events too (loading the graph a second time), so for it the API attaches its own
container and the cancel guard instead.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from injectq import InjectQ
from injectq.integrations.taskiq import setup_taskiq
from taskiq import InMemoryBroker
from taskiq.cli.utils import import_tasks

from tenxgraph_api.jobs.config import is_glob


def _import_task_modules(config_path: str, tasks: list[str]) -> None:
    """``worker.tasks`` the way ``10xgraph worker`` passes them: module names, and file globs
    relative to the project folder (the config file's folder)."""
    modules = [t for t in tasks if not is_glob(t)]
    patterns = [t for t in tasks if is_glob(t)]
    project = Path(config_path).resolve().parent
    with contextlib.chdir(project):  # TaskIQ globs from the cwd and imports with it on sys.path
        import_tasks(modules, patterns, bool(patterns))


async def start_broker(config_path: str, tasks: list[str], container: InjectQ) -> None:
    from tenxgraph_api.jobs.middleware import wrap_tasks
    from tenxgraph_api.jobs.worker import broker

    _import_task_modules(config_path, tasks)
    if isinstance(broker, InMemoryBroker):
        setup_taskiq(container, broker)
        wrap_tasks(broker)
        return
    await broker.startup()


async def stop_broker() -> None:
    from tenxgraph_api.jobs.worker import broker

    if not isinstance(broker, InMemoryBroker):
        await broker.shutdown()
