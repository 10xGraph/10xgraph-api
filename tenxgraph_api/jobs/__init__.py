"""Durable background tasks for 10xGraph (``tenxgraph_api.jobs``).

Public API (see ``JOBS_PLAN.md``)::

    from tenxgraph_api.jobs import task, TaskContext, RetryAfter, Fail, enqueue, get_task, runs

Delivery is at least once: a run may start again after a crash, so external side effects must
be idempotent (pass ``ctx.run_id`` to external APIs as the idempotency key).
"""

from tenxgraph_api.jobs.core.backoff import Backoff
from tenxgraph_api.jobs.core.client import RunHandle, enqueue, runs
from tenxgraph_api.jobs.core.context import TaskContext
from tenxgraph_api.jobs.core.hooks import TaskLifecycleHook
from tenxgraph_api.jobs.core.identity import system_identity
from tenxgraph_api.jobs.core.models import RunPage, RunState, TaskEvent, TaskRun
from tenxgraph_api.jobs.core.registry import get_task
from tenxgraph_api.jobs.core.task import Task, TaskCall, task
from tenxgraph_api.jobs.errors import (
    DuplicateTaskName,
    Fail,
    IdentityRequired,
    JobsError,
    JobsNotConfigured,
    PayloadTooLarge,
    RetryAfter,
    RunNotFound,
    TaskCancelled,
    TaskDefinitionError,
    TaskFailed,
    TaskNotFound,
    TaskValidationError,
)

from . import errors


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
