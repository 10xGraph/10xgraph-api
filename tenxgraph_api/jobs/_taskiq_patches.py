"""Local patches for TaskIQ problems, each removed once upstream fixes it.

Every patch names its entry in TASKIQ_UPSTREAM.md (workspace root). Versions: taskiq 0.13.0,
taskiq-redis 1.2.4.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from taskiq import AckableMessage, ScheduledTask, TaskiqMessage, TaskiqMiddleware, TaskiqResult
from taskiq.cli.scheduler.run import SchedulerLoop
from taskiq.receiver import Receiver
from taskiq.utils import maybe_awaitable


logger = logging.getLogger("tenxgraph_api.jobs")


# Scheduler


_original_ready = SchedulerLoop._is_schedule_ready_to_send
_original_mark = SchedulerLoop._mark_cron_tasks_as_already_run


def _schedule_key(task: ScheduledTask) -> tuple[Any, ...]:
    return (task.schedule_id, task.cron, task.cron_offset, task.interval, task.time)


def _ready_or_skip(self: SchedulerLoop, task: ScheduledTask, now: datetime) -> bool:
    """TASKIQ_UPSTREAM.md "Scheduler": the loop catches only ``ValueError`` from pycron, so
    one bad schedule (``*/0``, a huge interval) raised out of ``SchedulerLoop.run`` and stopped
    the scheduler for everyone. Log the schedule once and skip it; keep running."""
    broken: set[tuple[Any, ...]] = self.__dict__.setdefault("_broken_schedules", set())
    key = _schedule_key(task)
    if key in broken:
        return False
    try:
        return _original_ready(self, task, now)
    except Exception:
        broken.add(key)
        logger.exception(
            "Skipping schedule %s (task %s): it cannot be checked. Fix or cancel it.",
            task.schedule_id,
            task.task_name,
        )
        return False


def _mark_all_as_already_run(self: SchedulerLoop) -> None:
    """TASKIQ_UPSTREAM.md "Scheduler": ``--skip-first-run`` marks only cron schedules, so every
    interval schedule still runs at each scheduler start. Mark intervals as run now too: the
    first run is one interval after the start."""
    _original_mark(self)
    now = datetime.now(tz=UTC)
    for _, task_list in self.scheduled_tasks:
        for task in task_list:
            if task.interval is not None:
                self.interval_tasks_last_run[task.schedule_id] = now


def patch_scheduler_loop() -> None:
    """Apply the scheduler patches (idempotent). Called on import of ``jobs.scheduler``."""
    SchedulerLoop._is_schedule_ready_to_send = _ready_or_skip  # type: ignore[method-assign]
    SchedulerLoop._mark_cron_tasks_as_already_run = _mark_all_as_already_run  # type: ignore[method-assign]


# Worker / receiver


_original_callback = Receiver.callback


async def _callback_ending_lease(
    self: Receiver, message: bytes | AckableMessage, raise_err: bool = False
) -> None:
    """TASKIQ_UPSTREAM.md "Worker / receiver / broker": ``Receiver.callback`` can raise before
    the ack (a middleware ``pre_execute``, ``post_execute`` or ``on_error`` raising, a bad
    ``ack_type`` label, ``parse_params``, a dependency teardown). Nothing logs the error (the
    runner never reads the task's result) and the message stays unacked. Log it, then call the
    message's ``release`` (``redis_broker.LeasedMessage``) so the lease ends with the callback:
    a message that was not acked is released for redelivery instead of renewed forever."""
    try:
        await _original_callback(self, message, raise_err)
    except Exception:
        logger.exception("Task message callback failed")
        if raise_err:
            raise
    finally:
        release = getattr(message, "release", None)
        if release is not None:
            try:
                await release()
            except Exception:
                logger.exception("Ending the message lease failed")


_original_run_task = Receiver.run_task


async def _run_task_unless_aborted(
    self: Receiver, target: Any, message: TaskiqMessage, ack_controller: Any = None
) -> TaskiqResult[Any]:
    """TASKIQ_UPSTREAM.md "Worker / receiver / broker": ``Receiver.run_task`` resolves the
    task's dependencies before calling the function, so a generator dependency (a connection, a
    lock) ran its setup and teardown for a run that ``TaskMiddleware`` had already stopped
    (cancelled, refused). When it set an abort error, return the error result without resolving
    anything and run the ``on_error`` middlewares as TaskIQ does."""
    from tenxgraph_api.jobs.middleware import _ABORT

    error = _ABORT.get()
    if error is None:
        return await _original_run_task(self, target, message, ack_controller)
    result: TaskiqResult[Any] = TaskiqResult(
        is_err=True, log=None, return_value=None, execution_time=0, error=error,
        labels=message.labels,
    )  # fmt: skip
    for middleware in reversed(self.broker.middlewares):
        if middleware.__class__.on_error != TaskiqMiddleware.on_error:
            await maybe_awaitable(middleware.on_error(message, result, error))
    return result


def patch_receiver() -> None:
    """Apply the receiver patches (idempotent). Called on import of ``jobs.redis_broker`` and by
    ``wrap_tasks``."""
    Receiver.callback = _callback_ending_lease  # type: ignore[method-assign]
    Receiver.run_task = _run_task_unless_aborted  # type: ignore[method-assign]
