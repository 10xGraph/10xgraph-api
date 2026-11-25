"""The Redis result backend with one addition: a result that cannot be saved fails the task."""

from __future__ import annotations

import logging
from typing import Any

from taskiq import TaskiqResult
from taskiq_redis import RedisAsyncResultBackend

from tenxgraph_api.jobs import state


logger = logging.getLogger("tenxgraph_api.jobs")


class StatusResultBackend(RedisAsyncResultBackend):
    """TaskIQ logs a failed ``set_result`` and moves on, so the task stayed ``running`` (or was
    marked ``success`` with no result). Here the task is marked ``failure`` with a message that
    ``GET /v1/tasks/{id}`` shows, and the error is raised again for TaskIQ to log (it then skips
    ``post_save``, which would write the final status)."""

    async def set_result(self, task_id: str, result: TaskiqResult[Any]) -> None:
        try:
            await super().set_result(task_id, result)
        except Exception as error:
            try:
                await state.fail_unsaved(task_id, error)
            except Exception:  # the cache store is down too
                logger.exception("Task %s: failure status not saved", task_id)
            raise
