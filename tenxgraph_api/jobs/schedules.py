"""Dynamic schedules: add, change the time, delete.

TaskIQ has no update and no read-by-id (``get_schedules()`` also hides future one-time
schedules), so each schedule we create is also kept as a ``ScheduleRecord`` in the cache store.
A change deletes the TaskIQ schedule and adds it again with the same id. Never
``with_task_id`` on a schedule: every firing would reuse the id.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from taskiq import AsyncBroker, AsyncTaskiqDecoratedTask, ScheduledTask, ScheduleSource

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.arguments import check_arguments


if TYPE_CHECKING:
    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore


class ScheduleNotFoundError(LookupError):
    """No schedule with this id (deleted, expired or never created)."""


_source: ScheduleSource | None = None
_source_factory: Callable[[], ScheduleSource] | None = None


def set_schedule_source(
    source: ScheduleSource | None = None,
    factory: Callable[[], ScheduleSource] | None = None,
) -> None:
    """Use ``source`` (or build one lazily with ``factory``); tests pass a fake here."""
    global _source, _source_factory  # - one process-wide source
    _source, _source_factory = source, factory


def get_schedule_source() -> ScheduleSource:
    """The set source, else one built once from the ``worker`` config (Redis)."""
    global _source  # noqa: PLW0603
    if _source is None:
        _source = _source_factory() if _source_factory else _default_source()
    return _source


def _default_source() -> ScheduleSource:
    from tenxgraph_api.jobs.config import build_schedule_source
    from tenxgraph_api.jobs.worker import worker_config

    if worker_config is None:
        raise RuntimeError('Dynamic schedules need the "worker" key in 10xgraph.json')
    source: ScheduleSource = build_schedule_source(worker_config)
    return source


def _timing(cron: str | None, at: datetime | None, every: int | timedelta | None) -> dict[str, Any]:
    given = [value for value in (cron, at, every) if value is not None]
    if len(given) != 1:
        raise ValueError("Give exactly one of cron, at or every")
    return {"cron": cron, "cron_offset": None, "time": at, "interval": every}


def _store(broker: AsyncBroker | None) -> BaseCacheStore:
    store = state.cache_store_for(broker)
    if store is None:
        raise RuntimeError("No cache store is bound: schedules need one")
    return store


async def schedule(
    task: AsyncTaskiqDecoratedTask[Any, Any],
    *args: Any,
    cron: str | None = None,
    at: datetime | None = None,
    every: int | timedelta | None = None,
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> str:
    """Schedule ``task``: one of ``cron``, ``at`` (a datetime) or ``every`` (seconds); returns
    the schedule id. Each run acts as the user in ``config``."""
    timing = _timing(cron, at, every)
    check_arguments(task, args, kwargs)
    config = config or {}
    store = _store(task.broker)
    kicker = task.kicker().with_labels(**{state.CONFIG_LABEL: json.dumps(config)})
    source = get_schedule_source()
    if cron is not None:
        created = await kicker.schedule_by_cron(source, cron, *args, **kwargs)
    elif at is not None:
        created = await kicker.schedule_by_time(source, at, *args, **kwargs)
    else:
        created = await kicker.schedule_by_interval(source, timing["interval"], *args, **kwargs)
    record = state.ScheduleRecord(owner=state.owner_of(config), task=created.task)
    await state.write_schedule(store, record)
    return created.schedule_id


async def reschedule(
    schedule_id: str,
    *,
    cron: str | None = None,
    at: datetime | None = None,
    every: int | timedelta | None = None,
    broker: AsyncBroker | None = None,
) -> None:
    """Change only the time of a schedule (delete + add with the same id). ``broker`` picks
    the container (worker side); the API passes nothing."""
    timing = _timing(cron, at, every)
    store = _store(broker)
    record = await state.read_schedule(store, schedule_id)
    if record is None:
        raise ScheduleNotFoundError(schedule_id)
    new_task = ScheduledTask.model_validate({**record.task.model_dump(), **timing})
    source = get_schedule_source()
    await source.delete_schedule(schedule_id)
    await source.add_schedule(new_task)
    await state.write_schedule(store, record.model_copy(update={"task": new_task}))


async def unschedule(schedule_id: str, broker: AsyncBroker | None = None) -> None:
    """Delete a schedule (no error when it is already gone)."""
    await get_schedule_source().delete_schedule(schedule_id)
    await state.delete_schedule(_store(broker), schedule_id)
