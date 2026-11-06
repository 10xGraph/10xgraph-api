"""How an attempt ended, mapped to the ``AttemptOutcome`` the worker writes.

Pure functions over the attempt's state: no store calls. ``None`` means "write nothing" (the
lease was lost or the worker aborted).
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING, Any

from tenxgraph_api.jobs.core.backoff import Backoff
from tenxgraph_api.jobs.core.models import MAX_DEFERRALS, AttemptOutcome
from tenxgraph_api.jobs.errors import Fail, PayloadTooLarge, RetryAfter, TaskValidationError

from .logs import log_run


if TYPE_CHECKING:
    from .attempt import Attempt


logger = logging.getLogger("tenxgraph_api.jobs")

#: The error written when the store refuses a result or error value (``ValueNotStorable``).
NOT_SERIALIZABLE_ERROR: dict[str, Any] = {
    "code": "result_not_serializable",
    "type": "ValueNotStorable",
    "message": "the task result or error holds a value the run store cannot store "
    "(a NUL character, NaN or Infinity)",
}


def error_info(exc: BaseException, code: str = "error") -> dict[str, Any]:
    """The ``error`` stored on a run: code, exception type and message (no traceback)."""
    return {"code": code, "type": type(exc).__name__, "message": str(exc)}


def cancelled() -> AttemptOutcome:
    return AttemptOutcome(kind="cancelled", error={"code": "cancelled", "message": "cancelled"})


def interrupted(message: str = "worker shut down") -> AttemptOutcome:
    """Back to ``queued`` without using a retry (shutdown)."""
    return AttemptOutcome(kind="requeue", error={"code": "interrupted", "message": message})


def failed(error: dict[str, Any]) -> AttemptOutcome:
    return AttemptOutcome(kind="failed", error=error)


def stopped_before_body(attempt: Attempt) -> AttemptOutcome | None:
    """Outcome of an attempt stopped before its body ran."""
    if attempt.fenced_out or attempt.stop_reason in ("lost", "abort"):
        return None
    return cancelled() if attempt.stop_reason == "cancel" else interrupted()


def success(attempt: Attempt, value: Any, max_bytes: int) -> AttemptOutcome:
    """Validate and size-check the return value."""
    try:
        data = attempt.task.dump_result(value, max_bytes=max_bytes)
    except TaskValidationError as exc:
        code = "result_too_large" if isinstance(exc, PayloadTooLarge) else "result_invalid"
        return failed(error_info(exc, code))
    return AttemptOutcome(kind="succeeded", result=data)


def failure(attempt: Attempt, error: dict[str, Any]) -> AttemptOutcome:
    """A retry with the task's backoff while retries are left, else ``failed``."""
    task = attempt.task
    number = attempt.run.failure_count + 1
    if number > attempt.run.max_retries:
        return failed(error)
    try:
        delay = float(task.backoff(number))
        if not math.isfinite(delay):
            raise ValueError(f"backoff returned {delay}")
    except Exception:
        logger.exception("backoff of task %s failed; using the default backoff", task.name)
        delay = Backoff().delay(number)
    return AttemptOutcome(kind="retry", error=error, delay=max(0.0, delay))


def defer(attempt: Attempt, exc: RetryAfter) -> AttemptOutcome:
    """``RetryAfter``: run again later without using a retry, up to ``MAX_DEFERRALS``."""
    if attempt.run.defer_count >= MAX_DEFERRALS:
        return failed(
            {
                "code": "too_many_deferrals",
                "type": "RetryAfter",
                "message": f"RetryAfter used more than {MAX_DEFERRALS} times",
            }
        )
    return AttemptOutcome(kind="defer", delay=exc.seconds)


def classify_async(  # noqa: PLR0911 - one return per outcome
    attempt: Attempt, inner: asyncio.Task, max_bytes: int
) -> AttemptOutcome | None:
    """Map a finished ``async`` body (``inner``) to its outcome."""
    reason = attempt.stop_reason
    if reason in ("lost", "abort"):
        return None
    if reason == "shutdown" and (attempt.timed_out or inner.cancelled()):
        return interrupted()
    if attempt.timed_out:
        return failure(
            attempt,
            {
                "code": "timeout",
                "type": "TimeoutError",
                "message": f"attempt exceeded timeout of {attempt.task.timeout} s",
            },
        )
    if reason == "cancel":
        return cancelled()
    if inner.cancelled():
        return failure(attempt, {"code": "cancelled", "message": "task was cancelled"})
    exc = inner.exception()
    if exc is None:
        return success(attempt, inner.result(), max_bytes)
    if isinstance(exc, Fail):
        error = error_info(exc, "fail")
        if exc.data is not None:
            error["data"] = exc.data
        return failed(error)
    if isinstance(exc, RetryAfter):
        return defer(attempt, exc)
    log_run(logger, logging.WARNING, "attempt failed", attempt.run, exc_info=exc)
    return failure(attempt, error_info(exc))


def classify_execution(  # noqa: PLR0911 - one return per outcome
    attempt: Attempt, result: Any, max_bytes: int
) -> AttemptOutcome | None:
    """Map a ``thread`` / ``process`` executor result to its outcome."""
    reason = attempt.stop_reason
    if reason in ("lost", "abort") or attempt.fenced_out:
        return None
    kind = result.kind
    if reason == "shutdown" and kind not in ("returned", "raised"):
        return interrupted()
    if reason == "cancel":
        return cancelled()
    error = result.error or {"code": kind, "message": kind}
    if kind == "returned":
        if result.value_is_json:
            return AttemptOutcome(kind="succeeded", result=result.value)
        return success(attempt, result.value, max_bytes)
    if kind == "raised":
        exc = result.exception
        if isinstance(exc, Fail):
            return failed(error)
        if isinstance(exc, RetryAfter):
            return defer(attempt, exc)
        log_run(logger, logging.WARNING, "attempt failed", attempt.run, error=error.get("type"))
        return failure(attempt, error)
    if kind == "invalid":
        return failed(error)
    if kind in ("timeout", "crashed"):
        log_run(logger, logging.WARNING, f"attempt {kind}", attempt.run, error=error.get("code"))
        return failure(attempt, error)
    # "stopped" without a stop of our own: treat it as a failed attempt.
    return failure(attempt, {"code": "stopped", "message": "the attempt was stopped"})
