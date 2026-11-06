"""Errors raised by the jobs API.

``RetryAfter`` and ``Fail`` are control-flow signals a task raises on purpose to steer its own
run; every other exception here is raised *to* the caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from tenxgraph_api.jobs.core.models import TaskRun


class JobsError(Exception):
    """Base class of every jobs error."""


class JobsNotConfigured(JobsError, RuntimeError):
    """No jobs runtime is bound (no ``worker`` section, or outside ``jobs_harness``)."""


class TaskDefinitionError(JobsError, TypeError):
    """A ``@task`` declaration is invalid (bad option, bad signature)."""


class DuplicateTaskName(TaskDefinitionError):
    """Two tasks were registered under one name. Set ``name=`` on one of them."""


class TaskNotFound(JobsError, LookupError):
    """No task is registered under that name."""


class RunNotFound(JobsError, LookupError):
    """No run with that id exists (never created, or removed by retention)."""


class IdentityRequired(JobsError, ValueError):
    """``enqueue`` was called outside a task without ``identity=``."""


class TaskValidationError(JobsError, ValueError):
    """Kwargs (or a result) do not match the task's type hints.

    ``errors`` holds pydantic's error list when the failure came from validation.
    """

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class PayloadTooLarge(TaskValidationError):
    """Kwargs over the kwargs limit (256 KB by default) or a result over the result limit."""


class TaskFailed(JobsError):
    """``run.result()`` on a run that ended ``failed``. ``run`` is the final record."""

    def __init__(self, run: TaskRun) -> None:
        message = (run.error or {}).get("message", "task failed")
        super().__init__(f"run {run.run_id} ({run.task}) failed: {message}")
        self.run = run


class TaskCancelled(JobsError):
    """``run.result()`` on a run that ended ``cancelled``. ``run`` is the final record."""

    def __init__(self, run: TaskRun) -> None:
        super().__init__(f"run {run.run_id} ({run.task}) was cancelled")
        self.run = run


class ValueNotStorable(JobsError, ValueError):
    """A value cannot be stored by the run store, so nothing was written.

    Raised by every store (memory, SQLite, Postgres, Redis) before any write when a JSON field
    (``kwargs``, ``identity``, ``trigger``, ``result``, ``error``, ``progress``, event ``data``
    or event ``type``) holds NaN or Infinity, a string with a NUL character (``\\x00``, which
    Postgres ``jsonb`` and ``text`` refuse), or a value JSON cannot encode. ``field`` names the
    field and ``reason`` says why.
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field} cannot be stored: {reason}")
        self.field = field
        self.reason = reason


class RetryAfter(Exception):
    """Run again in ``seconds`` without using a retry (for a provider 429).

    The run goes back to ``queued`` with ``available_at = now + seconds``. A run may defer at
    most ``MAX_DEFERRALS`` (50) times; the next ``RetryAfter`` fails it.
    """

    def __init__(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("RetryAfter seconds must be >= 0")
        super().__init__(f"retry after {seconds} s")
        self.seconds = float(seconds)


class Fail(Exception):
    """Fail the run now, whether or not retries are left."""

    def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data
