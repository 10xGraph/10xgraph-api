"""The Redis source for dynamic schedules: taskiq-redis's list source with checked sends.

The scheduler keeps an in-memory copy of each schedule between refreshes. ``pre_send`` sends a
schedule only when Redis still holds that same schedule, so a schedule changed or deleted since
the last refresh does not run from the stale copy. ``post_send`` deletes a fired one-time
schedule only if Redis still holds the copy that fired: taskiq-redis 1.2.4 deletes by id, which
removed a schedule PATCHed to a new time just before its old time (S2, TASKIQ_UPSTREAM.md
"Scheduler"). ``add_schedule`` refuses cron and intervals the scheduler cannot run.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from redis.asyncio import Redis
from taskiq import ScheduledTask
from taskiq.exceptions import ScheduledTaskCancelledError
from taskiq_redis import ListRedisScheduleSource

from tenxgraph_api.jobs.schedule_rules import check_cron, check_every


logger = logging.getLogger("tenxgraph_api.jobs")


class CheckedScheduleSource(ListRedisScheduleSource):
    """``ListRedisScheduleSource`` that never sends a stale copy or deletes a newer one."""

    async def add_schedule(self, schedule: ScheduledTask) -> None:
        if schedule.cron is not None:
            schedule = schedule.model_copy(update={"cron": check_cron(schedule.cron)})
        if schedule.interval is not None:
            interval = schedule.interval
            check_every(interval.total_seconds() if isinstance(interval, timedelta) else interval)
        await super().add_schedule(schedule)

    async def stored(self, schedule_id: str) -> ScheduledTask | None:
        """The schedule Redis holds under ``schedule_id`` now, or None."""
        async with Redis(connection_pool=self._connection_pool) as redis:
            raw = await redis.get(self._get_data_key(schedule_id))
        if raw is None:
            return None
        return ScheduledTask.model_validate(self._serializer.loadb(raw))  # type: ignore[arg-type]

    async def pre_send(self, task: ScheduledTask) -> None:
        if await self.stored(task.schedule_id) != task:
            logger.info(
                "Schedule %s (task %s) was changed or deleted since the last refresh: "
                "not sending the old copy.",
                task.schedule_id,
                task.task_name,
            )
            raise ScheduledTaskCancelledError

    async def post_send(self, task: ScheduledTask) -> None:
        if task.time is not None and await self.stored(task.schedule_id) == task:
            await self.delete_schedule(task.schedule_id)
