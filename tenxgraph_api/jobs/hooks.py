"""Lifecycle hooks, shaped like core's ``GraphLifecycleHook`` (``tenxgraph/utils/callbacks.py``).

Every method is an optional async no-op. A hook error is logged and never changes the run.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Sequence
from typing import Any

from .models import TaskEvent, TaskRun


logger = logging.getLogger("tenxgraph_api.jobs")


class TaskLifecycleHook:
    """Base class for task lifecycle hooks; override only what you need.

    ``on_task_queued`` runs where the enqueue happens; the rest run in the worker. Every hook
    runs after the store write it reports, so the ``run`` it gets is the stored record.

    Example::

        class Audit(TaskLifecycleHook):
            async def on_task_failure(self, run, error):
                log.warning("task %s failed: %s", run.task, error["message"])
    """

    async def on_task_queued(self, run: TaskRun) -> None:
        """A new run was written (not called for a deduplicated enqueue)."""

    async def on_task_start(self, run: TaskRun) -> None:
        """An attempt was claimed and is about to run."""

    async def on_task_progress(self, run: TaskRun, event: TaskEvent | dict[str, Any]) -> None:
        """A progress event was flushed to the store."""

    async def on_task_retry(self, run: TaskRun, error: dict[str, Any]) -> None:
        """An attempt failed and the run will run again (``retrying``)."""

    async def on_task_success(self, run: TaskRun, result: Any) -> None:
        """The run ended ``succeeded``."""

    async def on_task_failure(self, run: TaskRun, error: dict[str, Any]) -> None:
        """The run ended ``failed``."""

    async def on_task_cancel(self, run: TaskRun) -> None:
        """The run ended ``cancelled``."""

    async def on_task_lost(self, run: TaskRun) -> None:
        """The worker holding the run stopped renewing its lease; the run was recovered."""


class HookRunner:
    """Calls every hook, logging (never raising) their errors."""

    def __init__(self, hooks: Sequence[TaskLifecycleHook] = ()) -> None:
        self.hooks = list(hooks)

    async def emit(self, name: str, *args: Any) -> None:
        for hook in self.hooks:
            method = getattr(hook, name, None)
            if method is None:
                continue
            await call_safely(method, *args, label=f"{type(hook).__name__}.{name}")


async def call_safely(fn: Callable[..., Any], *args: Any, label: str) -> None:
    """Call a sync or async hook; log any ``Exception`` it raises."""
    try:
        result = fn(*args)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("jobs hook %s failed (ignored)", label)
