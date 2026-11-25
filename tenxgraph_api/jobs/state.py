"""Job state kept in the API's cache store: status records and cancel flags.

Keys live under ``jobs:`` and expire after ``RESULT_TTL`` seconds. The store is the
``BaseCacheStore`` bound in the current InjectQ container (the request's in the API, the task's
in a worker under ``setup_taskiq``), else the one ``setup_taskiq`` attached to the broker.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Literal

from injectq import InjectionError, InjectQ
from injectq.integrations.taskiq import get_container_taskiq
from pydantic import BaseModel, Field, ValidationError
from tenxgraph.core.authz import Identity


if TYPE_CHECKING:
    from taskiq import AsyncBroker

    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore


logger = logging.getLogger("tenxgraph_api.jobs")

# How long status records and cancel flags are kept, in seconds. jobs.config sets it from
# ``worker.result_ttl``.
RESULT_TTL: int = 86400
# Task ids of a schedule's recent runs kept for ``GET /v1/tasks/{schedule_id}``.
MAX_SCHEDULE_RUNS = 20
# Message label that carries the run config as a JSON string (labels keep only scalars).
CONFIG_LABEL = "tenxgraph_config"
# Set by the Redis stream broker on a message delivered more than ``max_deliveries`` times
# (the reason text): ``TaskMiddleware`` fails the task with it instead of running it.
DELIVERY_LIMIT_LABEL = "tenxgraph_delivery_limit"

Status = Literal["queued", "running", "success", "failure", "cancelled"]


class TaskStatus(BaseModel):
    """What ``GET /v1/tasks/{id}`` shows besides the result and progress."""

    task_id: str
    task_name: str
    status: Status
    # Why the run failed when no saved result can say so (the result could not be saved).
    error: str | None = None
    owner: str | None = None
    # The owner's user_id and tenant values: what ``can_reach`` compares for reach tenant/all.
    user_id: str | None = None
    tenant: dict[str, str] = {}
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


def owner_fields(config: dict[str, Any]) -> dict[str, Any]:
    """The run's owner as ``/v1/tasks`` compares it: ``owner`` is ``identity_key`` of
    ``config["user"]`` (``user_id`` and tenant values), else the ``user_id`` alone; ``user_id``
    and ``tenant`` are kept for reach checks."""
    from tenxgraph_api.src.app.routers.graph.run_limits import identity_key

    user = config.get("user")
    if isinstance(user, dict | Identity):
        try:
            identity = Identity.model_validate(user)
            return {
                "owner": identity_key(identity),
                "user_id": identity.user_id,
                "tenant": identity.tenant,
            }
        except ValidationError:
            logger.warning("Ignoring a run config user that is not an Identity")
    user_id = config.get("user_id")
    user_id = None if user_id is None else str(user_id)
    return {"owner": user_id, "user_id": user_id, "tenant": {}}


def parse_config(labels: dict[str, Any]) -> dict[str, Any]:
    """The run config from the message label; ``{}`` when missing or not a JSON object."""
    raw = labels.get(CONFIG_LABEL)
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        logger.warning("Ignoring a %s label that is not JSON", CONFIG_LABEL)
        return {}
    return value if isinstance(value, dict) else {}


def container_for(broker: AsyncBroker | None) -> InjectQ:
    """The current container (a request's or a task's), else the broker's, else the global one."""
    try:
        return get_container_taskiq(broker.state if broker else None)
    except InjectionError:  # no container current and none attached to the broker
        return InjectQ.get_instance()


def cache_store_for(broker: AsyncBroker | None) -> BaseCacheStore | None:
    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore

    return container_for(broker).try_get(BaseCacheStore)


def _key(kind: str, ident: str) -> str:
    return f"jobs:{kind}:{ident}"


async def read_status(store: BaseCacheStore, task_id: str) -> TaskStatus | None:
    raw = await store.get(_key("status", task_id))
    return TaskStatus.model_validate_json(raw) if raw else None


async def write_status(store: BaseCacheStore, record: TaskStatus) -> None:
    record.updated_at = time.time()
    await store.set(_key("status", record.task_id), record.model_dump_json().encode(), RESULT_TTL)


# A status only moves forward: a late ``queued`` never replaces ``running``, and a final status
# is replaced only by another final one (the worker's own, after a redelivery).
_RANK: dict[str, int] = {"queued": 0, "running": 1, "success": 2, "failure": 2, "cancelled": 2}


async def update_status(
    store: BaseCacheStore,
    task_id: str,
    task_name: str,
    status: Status,
    config: dict[str, Any],
    error: str | None = None,
    only_from: Status | None = None,
) -> None:
    """Set ``status``, keeping owner and created time of an existing record (a new record takes
    its owner from the run ``config``). A step back (see ``_RANK``) is skipped, and so is a
    change when ``only_from`` is given and the record has another status (cancel uses it)."""
    record = await read_status(store, task_id)
    if record is None:
        record = TaskStatus(
            task_id=task_id, task_name=task_name, status=status, **owner_fields(config)
        )
    elif _RANK[status] < _RANK[record.status] or only_from not in (None, record.status):
        return
    record.status = status
    record.error = error
    await write_status(store, record)


async def fail_unsaved(task_id: str, error: BaseException) -> None:
    """The result could not be saved (not JSON-serialisable, backend down): mark the task
    ``failure`` with a message GET shows. Only the error type is kept, as task values and
    backend details can be internal; the worker log has the rest."""
    store = cache_store_for(None)
    if store is None:
        return
    text = f"The result could not be saved ({type(error).__name__}); see the worker log"
    await update_status(store, task_id, "", "failure", {}, error=text)


async def set_cancel_flag(store: BaseCacheStore, task_id: str) -> None:
    await store.set(_key("cancel", task_id), b"1", RESULT_TTL)


async def is_cancel_flag_set(store: BaseCacheStore, task_id: str) -> bool:
    return await store.get(_key("cancel", task_id)) is not None


async def read_schedule_runs(store: BaseCacheStore, schedule_id: str) -> list[str]:
    """Task ids of the schedule's last ``MAX_SCHEDULE_RUNS`` runs, newest first."""
    raw = await store.get(_key("schedule-runs", schedule_id))
    return json.loads(raw) if raw else []


async def add_schedule_run(store: BaseCacheStore, schedule_id: str, task_id: str) -> None:
    """Remember a run the scheduler sent. Only the scheduler writes it, one send at a time."""
    runs = [task_id, *await read_schedule_runs(store, schedule_id)][:MAX_SCHEDULE_RUNS]
    await store.set(_key("schedule-runs", schedule_id), json.dumps(runs).encode(), RESULT_TTL)


async def delete_schedule_runs(store: BaseCacheStore, schedule_id: str) -> None:
    await store.delete(_key("schedule-runs", schedule_id))
