"""What tasks and callers use: ``TaskContext``, ``TaskCancelled`` and ``cancel``.

Python callers start a task with TaskIQ's own ``await task.kiq(...)``. To pass a run config, put
it in the ``tenxgraph_config`` label (a JSON string, labels keep only scalars); our worker
middleware hands it to tasks that declare ``config``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from taskiq import AsyncBroker, Context, TaskiqDepends

from tenxgraph_api.jobs import state


logger = logging.getLogger("tenxgraph_api.jobs")

__all__ = ["TaskCancelled", "TaskContext", "cancel"]


class TaskCancelled(Exception):  # - public name from the plan
    """Raise it to end a task as cancelled with an error result. The worker raises it for a task
    cancelled before it started. A task that sees ``ctx.cancelled()`` may also just return: it
    still ends as ``cancelled``, with what it returned as its result."""


class TaskContext:
    """Optional task parameter: ``ctx: Annotated[TaskContext, TaskiqDepends()]``.

    A plain class on purpose: TaskIQ builds it from ``__init__`` (a pydantic model does not
    work). ``cancelled`` is async, so use it from ``async def`` tasks. For progress use TaskIQ's own
    ``ProgressTracker`` (``from taskiq.depends.progress_tracker import ProgressTracker``).
    """

    def __init__(self, context: Annotated[Context, TaskiqDepends()]) -> None:
        self._context = context
        self.task_id: str = context.message.task_id
        self.task_name: str = context.message.task_name
        # The same value a ``config`` parameter gets (see TaskMiddleware).
        self.config: dict[str, Any] = state.parse_config(context.message.labels)
        self.config["task_id"] = self.task_id

    async def cancelled(self) -> bool:
        """True after ``jobs.cancel`` / ``POST /v1/tasks/{id}/cancel`` for this task."""
        store = state.cache_store_for(self._context.broker)
        return store is not None and await state.is_cancel_flag_set(store, self.task_id)


async def cancel(task_id: str, broker: AsyncBroker | None = None) -> None:
    """Set the cancel flag; a queued task is not run, a running one sees ``ctx.cancelled()``.

    ``broker`` picks the container (worker side); the API passes nothing.
    """
    store = state.cache_store_for(broker)
    if store is None:
        raise RuntimeError("No cache store is bound: cancel needs one")
    await state.set_cancel_flag(store, task_id)
    record = await state.read_status(store, task_id)
    if record is not None and record.status == "queued":
        # Only from ``queued``, checked on a fresh read: a worker that started the task in
        # between keeps ``running``, a finished one keeps its final status.
        await state.update_status(
            store, task_id, record.task_name, "cancelled", {}, only_from="queued"
        )
