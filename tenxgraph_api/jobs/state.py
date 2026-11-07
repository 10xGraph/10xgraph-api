"""Job state kept in the API's cache store: status records and cancel flags.

Keys live under ``jobs:`` and expire after ``RESULT_TTL`` seconds. The store is the
``BaseCacheStore`` bound in the InjectQ container: the one ``setup_taskiq`` attached to the
broker in a worker, else the API's container.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError
from tenxgraph.core.authz import Identity


if TYPE_CHECKING:
    from injectq import InjectQ
    from taskiq import AsyncBroker

    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore


logger = logging.getLogger("tenxgraph_api.jobs")

# How long status records and cancel flags are kept, in seconds. jobs.config sets it from
# ``worker.result_ttl``.
RESULT_TTL: int = 86400
# Message label that carries the run config as a JSON string (labels keep only scalars).
CONFIG_LABEL = "tenxgraph_config"

Status = Literal["queued", "running", "success", "failure", "cancelled"]


class TaskStatus(BaseModel):
    """What ``GET /v1/tasks/{id}`` shows besides the result and progress."""

    task_id: str
    task_name: str
    status: Status
    owner: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


def owner_of(config: dict[str, Any]) -> str | None:
    """The run's owner as ``/v1/tasks`` compares it: ``identity_key`` of ``config["user"]``
    (``user_id`` and tenant values), else the ``user_id`` alone."""
    from tenxgraph_api.src.app.routers.graph.run_limits import identity_key

    user = config.get("user")
    if isinstance(user, dict):
        try:
            return identity_key(Identity.model_validate(user))
        except ValidationError:
            logger.warning("Ignoring a run config user that is not an Identity")
    user_id = config.get("user_id")
    return None if user_id is None else str(user_id)


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
    """The broker's container (``setup_taskiq``), else the current API container."""
    container = getattr(broker.state, "injectq_container", None) if broker else None
    if container is not None:
        return container
    from tenxgraph_api.src.app.core.utils.container import current_container

    return current_container()


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


async def update_status(
    store: BaseCacheStore, task_id: str, task_name: str, status: Status, owner: str | None
) -> None:
    """Set ``status``, keeping owner and created time of an existing record."""
    record = await read_status(store, task_id)
    if record is None:
        record = TaskStatus(task_id=task_id, task_name=task_name, status=status, owner=owner)
    record.status = status
    await write_status(store, record)


async def set_cancel_flag(store: BaseCacheStore, task_id: str) -> None:
    await store.set(_key("cancel", task_id), b"1", RESULT_TTL)


async def is_cancel_flag_set(store: BaseCacheStore, task_id: str) -> bool:
    return await store.get(_key("cancel", task_id)) is not None
