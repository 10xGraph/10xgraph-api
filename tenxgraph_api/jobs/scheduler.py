"""The TaskIQ scheduler: ``10xgraph scheduler`` runs ``tenxgraph_api.jobs.scheduler:scheduler``.

Sources: static ``schedule=`` labels on tasks, plus the Redis source for dynamic schedules when
the result backend is Redis (any broker but ``memory``). All schedules are UTC.

A run of an HTTP schedule gets its task id here: the scheduler writes its ``queued`` status
(owner from the schedule's run config) and adds the id to the schedule's recent runs before the
send, so ``GET`` and cancel by task id work for it like for ``POST /v1/tasks/{name}``.

Run exactly one scheduler. With Redis, the scheduler holds a lease (``LEASE_KEY``): a second
one logs a warning and waits as a standby, sending nothing until the first stops or its lease
expires. A standby (not a refusal) so a restart or a rolling deploy takes over by itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from typing import Any

from taskiq import AsyncBroker, ScheduledTask, ScheduleSource, TaskiqScheduler
from taskiq.exceptions import ScheduledTaskCancelledError
from taskiq.kicker import AsyncKicker
from taskiq.schedule_sources import LabelScheduleSource
from taskiq.utils import maybe_awaitable

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs._taskiq_patches import patch_scheduler_loop
from tenxgraph_api.jobs.config import (
    SCHEDULE_REDIS_TIMEOUT,
    build_schedule_source,
    schedule_redis_url,
)
from tenxgraph_api.jobs.schedule_rules import HTTP_SCHEDULE_LABEL
from tenxgraph_api.jobs.worker import broker, graph_config, worker_config
from tenxgraph_api.src.app.core.redis_client import prefixed


logger = logging.getLogger("tenxgraph_api.jobs")

LEASE_KEY = "10xgraph:scheduler:lease"  # REDIS_KEY_PREFIX goes in front (``lease_key``)
LEASE_TTL = 30  # seconds; a crashed scheduler's lease frees itself after this
LEASE_RENEW_EVERY = 10
HTTP_LABEL = "http"


class LeasedScheduler(TaskiqScheduler):
    """Sends schedules only while it holds the Redis lease; without Redis it always sends."""

    def __init__(
        self, broker: AsyncBroker, sources: list[ScheduleSource], lease_url: str | None
    ) -> None:
        super().__init__(broker, sources)
        self.lease_url = lease_url
        self.lease_key = prefixed(LEASE_KEY)
        self.cache_store: Any = None  # the API's cache store, for the runs of HTTP schedules
        self.token = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.lease_until = 0.0  # time.monotonic() when the held lease may have expired
        self.redis: Any = None
        self._renewer: asyncio.Task[None] | None = None
        self._standby_logged = False

    @property
    def leader(self) -> bool:
        return time.monotonic() < self.lease_until

    async def startup(self) -> None:
        await super().startup()
        if graph_config is not None:
            from tenxgraph_api.src.app.core.cache_store.loader import build_cache_store

            self.cache_store, _ = build_cache_store(graph_config)
        if self.lease_url is None:
            logger.warning(
                "No Redis for the scheduler lease: a second scheduler cannot be detected. "
                "Run exactly one scheduler."
            )
            self.lease_until = float("inf")
            return
        from redis.asyncio import Redis

        self.redis = Redis.from_url(
            self.lease_url,
            socket_timeout=SCHEDULE_REDIS_TIMEOUT,
            socket_connect_timeout=SCHEDULE_REDIS_TIMEOUT,
        )
        await self.hold_lease()
        self._renewer = asyncio.create_task(self._keep_lease())

    async def hold_lease(self) -> None:
        """Take the lease, or renew it when this scheduler holds it; log every change."""
        was_leader = self.leader
        started = time.monotonic()
        holder: bytes | None = None
        try:
            if await self.redis.set(self.lease_key, self.token, nx=True, ex=LEASE_TTL):
                held = True
            else:
                holder = await self.redis.get(self.lease_key)
                held = holder == self.token.encode() and bool(
                    await self.redis.expire(self.lease_key, LEASE_TTL)
                )
        except Exception:
            # Keep sending until the lease could have expired, then stop.
            logger.exception("Cannot renew the scheduler lease in Redis")
            return
        if held:
            self.lease_until = started + LEASE_TTL
            if not was_leader:
                logger.info("Scheduler lease taken (%s): sending schedules.", self.token)
            self._standby_logged = False
            return
        self.lease_until = 0.0
        if was_leader:
            logger.error("Scheduler lease lost to %s: this scheduler stops sending.", _text(holder))
        elif not self._standby_logged:
            logger.warning(
                "Another scheduler is running (lease %s held by %s). Run exactly one "
                "scheduler: this one waits as a standby and sends nothing until that one stops.",
                self.lease_key,
                _text(holder),
            )
        self._standby_logged = True

    async def _keep_lease(self) -> None:
        while True:
            await asyncio.sleep(LEASE_RENEW_EVERY)
            await self.hold_lease()

    async def on_ready(self, source: ScheduleSource, task: ScheduledTask) -> None:
        if not self.leader:
            logger.debug("Standby: schedule %s not sent.", task.schedule_id)
            return
        if task.labels.get(HTTP_SCHEDULE_LABEL) and not _http_task(self.broker, task.task_name):
            logger.warning(
                "Schedule %s not sent: task %s is gone or no longer has http=True. "
                "Cancel the schedule through POST /v1/tasks/%s/cancel.",
                task.schedule_id,
                task.task_name,
                task.schedule_id,
            )
            return
        try:
            await self._send(source, task)
        except Exception:
            logger.exception(
                "Sending schedule %s (task %s) failed", task.schedule_id, task.task_name
            )

    async def _send(self, source: ScheduleSource, task: ScheduledTask) -> None:
        """``TaskiqScheduler.on_ready`` with the task id chosen here, so a run of an HTTP
        schedule is recorded (``_record_run``) before it is sent."""
        try:
            await maybe_awaitable(source.pre_send(task))
        except ScheduledTaskCancelledError:
            logger.info("Scheduled task %s has been cancelled.", task.task_name)
            return
        task_id = task.task_id or self.broker.id_generator()
        if task.labels.get(HTTP_SCHEDULE_LABEL):
            await self._record_run(task, task_id)
        await (
            AsyncKicker(task.task_name, self.broker, task.labels)
            .with_labels(schedule_id=task.schedule_id)
            .with_task_id(task_id=task_id)
            .kiq(*task.args, **task.kwargs)
        )
        await maybe_awaitable(source.post_send(task))

    async def _record_run(self, task: ScheduledTask, task_id: str) -> None:
        """The run's ``queued`` status (owner from the schedule's run config) and its id in
        the schedule's recent runs. A failure is logged and the run is still sent."""
        if self.cache_store is None:
            return
        try:
            config = state.parse_config(task.labels)
            await state.write_status(
                self.cache_store,
                state.TaskStatus(
                    task_id=task_id,
                    task_name=task.task_name,
                    status="queued",
                    **state.owner_fields(config),
                ),
            )
            await state.add_schedule_run(self.cache_store, task.schedule_id, task_id)
        except Exception:
            logger.exception(
                "Run %s of schedule %s not recorded: GET /v1/tasks/%s answers 404 until the "
                "worker starts it",
                task_id,
                task.schedule_id,
                task_id,
            )

    async def shutdown(self) -> None:
        if self._renewer is not None:
            self._renewer.cancel()
        if self.cache_store is not None:
            try:
                await self.cache_store.aclose()
            except Exception:
                logger.exception("Cannot close the cache store")
        if self.redis is not None:
            try:
                if await self.redis.get(self.lease_key) == self.token.encode():
                    await self.redis.delete(self.lease_key)  # a restart takes over at once
            except Exception:
                logger.exception("Cannot release the scheduler lease")
            await self.redis.aclose()
        await super().shutdown()


def _text(holder: bytes | None) -> str:
    return holder.decode(errors="replace") if holder else "unknown"


def _http_task(broker: AsyncBroker, name: str) -> bool:
    task = broker.find_task(name)
    return task is not None and task.labels.get(HTTP_LABEL) is True


patch_scheduler_loop()

sources: list[ScheduleSource] = [LabelScheduleSource(broker)]
redis_source = build_schedule_source(worker_config) if worker_config is not None else None
if redis_source is not None:
    sources.append(redis_source)

scheduler = LeasedScheduler(
    broker, sources, schedule_redis_url(worker_config) if worker_config is not None else None
)
