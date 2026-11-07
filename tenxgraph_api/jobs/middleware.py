"""Worker middlewares: ``TaskMiddleware`` (config, argument check, cancel, status) and
``MonitoringMiddleware`` (logs and publisher events).

A TaskIQ middleware cannot skip a task: ``pre_execute`` runs outside any ``try`` in the
receiver, so a raise there leaves the message unacked (redelivered forever on a stream) and the
caller waiting. Ours never raise. To stop a run, ``pre_execute`` puts the error in a ContextVar
and the guard that ``wrap_tasks`` puts around every task function raises it before the body
runs; the caller gets an error result. The ContextVar does not reach a process-pool executor.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

from taskiq import AsyncBroker, TaskiqMessage, TaskiqMiddleware, TaskiqResult
from taskiq.exceptions import NoResultError

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.arguments import ReservedArgumentError, check_arguments, injected_names
from tenxgraph_api.jobs.context import TaskCancelled
from tenxgraph_api.jobs.monitoring import MonitoringMiddleware


logger = logging.getLogger("tenxgraph_api.jobs")

__all__ = ["MonitoringMiddleware", "TaskMiddleware", "wrap_tasks"]

# The error the guard raises instead of running the task body (None: run it).
_ABORT: contextvars.ContextVar[BaseException | None] = contextvars.ContextVar(
    "tenxgraph_task_abort", default=None
)
_GUARD_MARK = "__tenxgraph_guard__"


def _guard(func: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_guard(*args: Any, **kwargs: Any) -> Any:
            if (error := _ABORT.get()) is not None:
                raise error
            return await func(*args, **kwargs)

        wrapper: Callable[..., Any] = async_guard
    else:

        @functools.wraps(func)
        def sync_guard(*args: Any, **kwargs: Any) -> Any:
            if (error := _ABORT.get()) is not None:
                raise error
            return func(*args, **kwargs)

        wrapper = sync_guard
    setattr(wrapper, _GUARD_MARK, True)
    return wrapper


def wrap_tasks(broker: AsyncBroker) -> None:
    """Put the guard around every registered task's function (the receiver calls
    ``task.original_func``). Safe to call more than once."""
    for task in broker.get_all_tasks().values():
        if not getattr(task.original_func, _GUARD_MARK, False):
            task.original_func = _guard(task.original_func)


class TaskMiddleware(TaskiqMiddleware):
    """Run config into ``config``, the reserved-argument re-check, cancel and the status record.

    A message whose kwargs break the reserved-argument rule is not run: injected names are
    removed from its kwargs (so injection wins even without the guard) and the guard raises
    ``ReservedArgumentError``. Failing the run is safer than dropping arguments and running
    the task with different input than the caller sent.
    """

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        _ABORT.set(None)
        try:
            await self._prepare(message)
        except Exception:
            logger.exception("Task %s (%s): pre_execute failed", message.task_name, message.task_id)
        return message

    async def _prepare(self, message: TaskiqMessage) -> None:
        task = self.broker.find_task(message.task_name)
        if task is None:
            return
        config = state.parse_config(message.labels)
        config["task_id"] = message.task_id
        # ``config`` always comes from the label: a retry re-sends the kwargs we set here.
        message.kwargs.pop("config", None)
        try:
            check_arguments(task, message.args, message.kwargs)
        except ReservedArgumentError as error:
            for name in injected_names(task.original_func) & set(message.kwargs):
                message.kwargs.pop(name)
            _ABORT.set(error)
        if "config" in inspect.signature(task.original_func).parameters:
            message.kwargs["config"] = config

        store = state.cache_store_for(self.broker)
        if store is None:
            return
        status: state.Status = "running"
        if await state.is_cancel_flag_set(store, message.task_id):
            _ABORT.set(TaskCancelled("cancelled before start"))
            status = "cancelled"
        owner = state.owner_of(config)
        await state.update_status(store, message.task_id, message.task_name, status, owner)

    async def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        _ABORT.set(None)  # the in-memory broker can run a task in the caller's context
        try:
            store = state.cache_store_for(self.broker)
            if store is None:
                return
            owner = state.owner_of(state.parse_config(message.labels))
            status = _final_status(result)
            await state.update_status(store, message.task_id, message.task_name, status, owner)
        except Exception:
            logger.exception("Task %s (%s): status not saved", message.task_name, message.task_id)


def _final_status(result: TaskiqResult[Any]) -> state.Status:
    if isinstance(result.error, NoResultError):
        return "queued"  # SimpleRetryMiddleware sent it again
    if isinstance(result.error, TaskCancelled):
        return "cancelled"
    return "failure" if result.is_err else "success"
