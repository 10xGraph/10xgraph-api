"""Retries: TaskIQ's ``SimpleRetryMiddleware`` with ``retries`` meaning retries, and with the
failures a retry cannot fix left alone."""

from __future__ import annotations

from typing import Any

from taskiq import TaskiqMessage, TaskiqResult
from taskiq.exceptions import NoResultError
from taskiq.middlewares import SimpleRetryMiddleware

from tenxgraph_api.jobs.arguments import TaskArgumentError
from tenxgraph_api.jobs.context import TaskCancelled


class DeliveryLimitError(RuntimeError):
    """The message was delivered more than ``max_deliveries`` times (a task that keeps killing
    its worker): the run fails without executing."""


class RetryScheduled(NoResultError):
    """Set as the result error when a retry was sent: no result is saved for this attempt."""


# Cancelled, refused (``PermissionError``), bad arguments and over-delivered runs never ran the
# task body; running them again gives the same answer.
NEVER_RETRIED = (TaskCancelled, PermissionError, TaskArgumentError, DeliveryLimitError)


class TaskRetryMiddleware(SimpleRetryMiddleware):
    """``retries`` is the number of retries after the first attempt (``retries + 1`` runs).

    TaskIQ's ``max_retries`` counts runs minus one (``max_retries=3`` gives 2 retries), so the
    config value is passed as ``retries + 1``. A per-task ``max_retries`` label keeps TaskIQ's
    meaning.
    """

    def __init__(self, retries: int = 3) -> None:
        super().__init__(default_retry_count=retries + 1)

    async def on_error(
        self, message: TaskiqMessage, result: TaskiqResult[Any], exception: BaseException
    ) -> None:
        if isinstance(exception, NEVER_RETRIED):
            return
        await super().on_error(message, result, exception)
        if result.error is not exception:  # TaskIQ swapped it for NoResultError: a retry is sent
            result.error = RetryScheduled()
