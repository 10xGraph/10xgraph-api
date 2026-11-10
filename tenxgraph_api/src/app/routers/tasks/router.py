"""``/v1/tasks``: run or schedule an ``http=True`` task, read it, change a schedule, cancel.

Mounted only when 10xgraph.json has a ``worker`` key. Same auth as the graph routes. Reading and
cancelling widen by the caller's reach (``tenant``, ``all``); running and changing a schedule
stay ``own`` (A13). Anything else answers 404, the same as an unknown id.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from taskiq import AsyncTaskiqDecoratedTask, ScheduleSource
from tenxgraph.core.authz import Identity, Reach, can_reach

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.arguments import ReservedArgumentError, check_arguments
from tenxgraph_api.jobs.context import cancel
from tenxgraph_api.jobs.scheduler import sources
from tenxgraph_api.jobs.worker import broker
from tenxgraph_api.src.app.core.audit import audit_event
from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.core.validation.request_config import ClientInputError
from tenxgraph_api.src.app.routers.graph.run_limits import identity_key
from tenxgraph_api.src.app.utils.response_helper import success_response

from .schemas import Schedule, ScheduleChange, ScheduleRecord, TaskCall


router = APIRouter(tags=["Tasks"])

# The Redis source for dynamic schedules (with a Redis result backend); None: no scheduling.
schedule_source: ScheduleSource | None = sources[1] if len(sources) > 1 else None

NOT_FOUND = "Task not found"


def _store() -> BaseCacheStore:
    store = state.cache_store_for(None)
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No cache store is bound")
    return store


def _visible(
    record: state.TaskStatus | ScheduleRecord, identity: Identity | None, *, widen: bool
) -> bool:
    """The caller's own task or schedule (``identity_key``); with ``widen`` (reads, cancel)
    also one the caller's reach covers."""
    if record.owner == identity_key(identity):
        return True
    if not widen or identity is None or identity.reach is Reach.OWN:
        return False
    return can_reach({"user": identity}, record.user_id, record.tenant)


def _http_task(name: str) -> AsyncTaskiqDecoratedTask[Any, Any]:
    task = broker.find_task(name)
    if task is None or task.labels.get("http") is not True:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return task


def _schedule_key(schedule_id: str) -> str:
    return f"jobs:schedule:{schedule_id}"


async def _read_schedule(store: BaseCacheStore, schedule_id: str) -> ScheduleRecord | None:
    raw = await store.get(_schedule_key(schedule_id))
    return ScheduleRecord.model_validate_json(raw) if raw else None


async def _save_schedule(store: BaseCacheStore, record: ScheduleRecord) -> None:
    # Recurring schedules live until cancelled: no TTL. A one-time one is kept until its
    # run's result expires.
    ttl = None
    if record.schedule.at is not None:
        at = record.schedule.at
        at = at if at.tzinfo else at.replace(tzinfo=UTC)  # the Redis source reads naive as UTC
        ttl = max(at.timestamp() - datetime.now(UTC).timestamp(), 0) + state.RESULT_TTL
    await store.set(_schedule_key(record.schedule_id), record.model_dump_json().encode(), ttl)


def _source() -> ScheduleSource:
    if schedule_source is None:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            'Scheduling needs Redis: a broker other than "memory" with the "redis" result backend',
        )
    return schedule_source


async def _add_schedule(task: AsyncTaskiqDecoratedTask[Any, Any], record: ScheduleRecord) -> None:
    kicker = (
        task.kicker()
        .with_schedule_id(record.schedule_id)
        .with_labels(**{state.CONFIG_LABEL: json.dumps(record.config, default=str)})
    )
    when: Schedule = record.schedule
    args, kwargs = record.args, record.kwargs
    if when.cron is not None:
        await kicker.schedule_by_cron(_source(), when.cron, *args, **kwargs)
    elif when.at is not None:
        await kicker.schedule_by_time(_source(), when.at, *args, **kwargs)
    else:
        await kicker.schedule_by_interval(_source(), when.every or 0, *args, **kwargs)


def _error_text(error: Any) -> str | None:
    if error is None:
        return None
    if not isinstance(error, BaseException):
        return str(error)
    if get_settings().MODE == "production":
        return type(error).__name__  # task errors may carry internal details
    return f"{type(error).__name__}: {error}"


@router.post("/v1/tasks/{name}", status_code=status.HTTP_202_ACCEPTED, summary="Run a task")
async def start_task(
    request: Request,
    name: str,
    body: TaskCall,
    identity: Identity | None = Depends(RequirePermission("tasks", "trigger")),
):
    """Run the task now (``{task_id}``), or schedule it when ``schedule`` is set
    (``{schedule_id}``)."""
    task = _http_task(name)
    try:
        check_arguments(task, body.args, body.kwargs)
    except ReservedArgumentError as exc:
        raise ClientInputError(str(exc)) from None
    store = _store()
    config: dict[str, Any] = {"user_id": identity.user_id if identity is not None else "anonymous"}
    if identity is not None:
        # JSON for the worker, which rebuilds the Identity from it.
        config["user"] = identity.model_dump(mode="json")
    owner = state.owner_fields(config)

    if body.schedule is not None:
        _source()  # refuse before writing a record
        record = ScheduleRecord(
            schedule_id=broker.id_generator(),
            task_name=task.task_name,
            **owner,
            schedule=body.schedule,
            args=body.args,
            kwargs=body.kwargs,
            config=config,
        )
        await _save_schedule(store, record)
        try:
            await _add_schedule(task, record)
        except Exception:
            await store.delete(_schedule_key(record.schedule_id))
            raise
        await audit_event(
            "change",
            "success",
            action="tasks:trigger",
            resource=f"schedule/{record.schedule_id}",
            identity=identity,
        )
        return success_response({"schedule_id": record.schedule_id}, request, status_code=202)

    task_id = broker.id_generator()
    # Written before the send, so a fast worker never finds the task without its owner.
    await state.write_status(
        store,
        state.TaskStatus(task_id=task_id, task_name=task.task_name, status="queued", **owner),
    )
    try:
        await (
            task.kicker()
            .with_task_id(task_id)
            .with_labels(**{state.CONFIG_LABEL: json.dumps(config, default=str)})
            .kiq(*body.args, **body.kwargs)
        )
    except Exception:
        await state.update_status(store, task_id, task.task_name, "failure", config)
        raise
    await audit_event(
        "change",
        "success",
        action="tasks:trigger",
        resource=f"task/{task_id}",
        identity=identity,
    )
    return success_response({"task_id": task_id}, request, status_code=202)


@router.get("/v1/tasks/{task_id}", summary="Read a task or schedule")
async def get_task(
    request: Request,
    task_id: str,
    identity: Identity | None = Depends(RequirePermission("tasks", "read")),
):
    """A schedule: its task, time and arguments. A task: status, progress, result, error."""
    store = _store()
    schedule = await _read_schedule(store, task_id)
    if schedule is not None and _visible(schedule, identity, widen=True):
        return success_response(schedule.model_dump(mode="json", exclude={"config"}), request)
    record = await state.read_status(store, task_id)
    if schedule is not None or record is None or not _visible(record, identity, widen=True):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)

    data: dict[str, Any] = record.model_dump(mode="json")
    data.update(progress=None, result=None, error=None)
    backend = broker.result_backend
    progress = await backend.get_progress(task_id)
    if progress is not None:
        data["progress"] = progress.model_dump(mode="json")
    if await backend.is_result_ready(task_id):
        result = await backend.get_result(task_id)
        data["result"] = None if result.is_err else result.return_value
        data["error"] = _error_text(result.error)
    return success_response(data, request)


@router.patch("/v1/tasks/{schedule_id}", summary="Change a schedule's time")
async def change_schedule(
    request: Request,
    schedule_id: str,
    body: ScheduleChange,
    identity: Identity | None = Depends(RequirePermission("tasks", "trigger")),
):
    """Delete and add again with the same id, arguments and run config (TaskIQ has no
    update)."""
    store = _store()
    record = await _read_schedule(store, schedule_id)
    if record is None or not _visible(record, identity, widen=False):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    task = broker.find_task(record.task_name)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    source = _source()
    await source.delete_schedule(schedule_id)
    record.schedule = body.schedule
    await _save_schedule(store, record)
    await _add_schedule(task, record)
    await audit_event(
        "change",
        "success",
        action="tasks:trigger",
        resource=f"schedule/{schedule_id}",
        identity=identity,
    )
    return success_response(record.model_dump(mode="json", exclude={"config"}), request)


@router.post("/v1/tasks/{task_id}/cancel", summary="Cancel a task or delete a schedule")
async def cancel_task(
    request: Request,
    task_id: str,
    identity: Identity | None = Depends(RequirePermission("tasks", "cancel")),
):
    """A schedule is deleted. A queued task is not run; a running one sees
    ``ctx.cancelled()``."""
    store = _store()
    schedule = await _read_schedule(store, task_id)
    if schedule is not None and _visible(schedule, identity, widen=True):
        await _source().delete_schedule(task_id)
        await store.delete(_schedule_key(task_id))
        await audit_event(
            "change",
            "success",
            action="tasks:cancel",
            resource=f"schedule/{task_id}",
            identity=identity,
        )
        return success_response({"schedule_id": task_id, "deleted": True}, request)
    record = await state.read_status(store, task_id)
    if schedule is not None or record is None or not _visible(record, identity, widen=True):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    await cancel(task_id)
    record = await state.read_status(store, task_id) or record
    await audit_event(
        "change",
        "success",
        action="tasks:cancel",
        resource=f"task/{task_id}",
        identity=identity,
    )
    return success_response({"task_id": task_id, "status": record.status}, request)
