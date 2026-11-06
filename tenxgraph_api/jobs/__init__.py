"""Durable background tasks for 10xGraph (``tenxgraph_api.jobs``).

Public API (see ``JOBS_PLAN.md``)::

    from tenxgraph_api.jobs import task, TaskContext, RetryAfter, Fail, enqueue, get_task, runs

Delivery is at least once: a run may start again after a crash, so external side effects must
be idempotent (pass ``ctx.run_id`` to external APIs as the idempotency key).
"""

from . import errors
from .backoff import Backoff
from .client import RunHandle, enqueue, runs
from .context import TaskContext
from .control import Fail, RetryAfter
from .errors import (
    DuplicateTaskName,
    IdentityRequired,
    JobsError,
    JobsNotConfigured,
    PayloadTooLarge,
    RunNotFound,
    TaskCancelled,
    TaskDefinitionError,
    TaskFailed,
    TaskNotFound,
    TaskValidationError,
)
from .hooks import TaskLifecycleHook
from .identity import system_identity
from .models import RunPage, RunState, TaskEvent, TaskRun
from .registry import get_task
from .task import Task, TaskCall, task


__all__ = [
    "Backoff",
    "DuplicateTaskName",
    "Fail",
    "IdentityRequired",
    "JobsError",
    "JobsNotConfigured",
    "PayloadTooLarge",
    "RetryAfter",
    "RunHandle",
    "RunNotFound",
    "RunPage",
    "RunState",
    "Task",
    "TaskCall",
    "TaskCancelled",
    "TaskContext",
    "TaskDefinitionError",
    "TaskEvent",
    "TaskFailed",
    "TaskLifecycleHook",
    "TaskNotFound",
    "TaskRun",
    "TaskValidationError",
    "enqueue",
    "errors",
    "get_task",
    "runs",
    "system_identity",
    "task",
]
