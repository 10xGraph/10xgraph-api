"""What tasks and callers use: ``TaskContext``, ``enqueue``, ``cancel`` and the schedule helpers.

Python callers start a task with ``await jobs.enqueue(task, *args, config=config, **kwargs)``:
the run config travels in the ``tenxgraph_config`` label (a JSON string, labels keep only
scalars) and our worker middleware passes it to tasks that declare ``config``.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from taskiq import AsyncBroker, AsyncTaskiqDecoratedTask, AsyncTaskiqTask, Context, TaskiqDepends
from taskiq.depends.progress_tracker import TaskProgress, TaskState

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.arguments import ReservedArgumentError, check_arguments
from tenxgraph_api.jobs.schedules import (
    ScheduleNotFoundError,
    reschedule,
    schedule,
    set_schedule_source,
    unschedule,
)


logger = logging.getLogger("tenxgraph_api.jobs")

__all__ = [
    "ReservedArgumentError",
    "ScheduleNotFoundError",
    "TaskCancelled",
    "TaskContext",
    "cancel",
    "check_arguments",
    "enqueue",
    "reschedule",
    "schedule",
    "set_schedule_source",
    "unschedule",
]


class TaskCancelled(Exception):  # - public name from the plan
    """The task was cancelled before it started (the caller sees it as the error result)."""


class TaskContext:
    """Optional task parameter: ``ctx: Annotated[TaskContext, TaskiqDepends()]``.

    A plain class on purpose: TaskIQ builds it from ``__init__`` (a pydantic model does not
    work). ``progress`` and ``cancelled`` are async, so use them from ``async def`` tasks.
    """

    def __init__(self, context: Annotated[Context, TaskiqDepends()]) -> None:
        self._context = context
        self.task_id: str = context.message.task_id
        self.task_name: str = context.message.task_name
        # The same value a ``config`` parameter gets (see TaskMiddleware).
        self.config: dict[str, Any] = state.parse_config(context.message.labels)
        self.config["task_id"] = self.task_id

    async def progress(self, meta: Any) -> None:
        """Save progress; ``GET /v1/tasks/{id}`` shows it."""
        progress: TaskProgress[Any] = TaskProgress(state=TaskState.STARTED, meta=meta)
        await self._context.broker.result_backend.set_progress(self.task_id, progress)

    async def cancelled(self) -> bool:
        """True after ``jobs.cancel`` / ``POST /v1/tasks/{id}/cancel`` for this task."""
        store = state.cache_store_for(self._context.broker)
        return store is not None and await state.is_cancel_flag_set(store, self.task_id)


async def enqueue(
    task: AsyncTaskiqDecoratedTask[Any, Any],
    *args: Any,
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> AsyncTaskiqTask[Any]:
    """Check the arguments, record the task as ``queued`` and send it; return TaskIQ's handle.

    The record is written before sending, so a fast worker's ``running`` is never overwritten.
    """
    check_arguments(task, args, kwargs)
    config = config or {}
    task_id = task.broker.id_generator()
    store = state.cache_store_for(task.broker)
    if store is not None:
        record = state.TaskStatus(
            task_id=task_id,
            task_name=task.task_name,
            status="queued",
            owner=state.owner_of(config),
        )
        await state.write_status(store, record)
    kicker = task.kicker().with_task_id(task_id)
    kicker = kicker.with_labels(**{state.CONFIG_LABEL: json.dumps(config)})
    return await kicker.kiq(*args, **kwargs)


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
        record.status = "cancelled"
        await state.write_status(store, record)
