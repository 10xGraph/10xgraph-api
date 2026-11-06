"""Run identity: who a run acts for.

Until AUTH_PLAN's ``Identity`` lands, an identity is the same dict the API puts in
``config["user"]``: at least a non-empty ``user_id``, plus any claims. Every run stores a JSON
snapshot of it (J9). There is no default identity: ``enqueue`` needs ``identity=`` or runs
inside a task, whose identity it inherits.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from tenxgraph.storage.store.base_store import system_user_id

from .errors import IdentityRequired


if TYPE_CHECKING:
    from .context import TaskContext


#: The TaskContext of the task running in this asyncio task, if any.
current_task_context: ContextVar[TaskContext | None] = ContextVar(
    "tenxgraph_jobs_task_context", default=None
)


def normalize_identity(identity: Any) -> dict[str, Any]:
    """A JSON dict with a string ``user_id`` from a dict, a pydantic model or an object with a
    ``user_id`` attribute. Raises ``IdentityRequired`` when there is no usable ``user_id``."""
    if identity is None:
        raise IdentityRequired("identity is required")
    if isinstance(identity, Mapping):
        data = dict(identity)
    elif hasattr(identity, "model_dump"):
        data = identity.model_dump(mode="json")
    elif hasattr(identity, "user_id"):
        data = {"user_id": identity.user_id}
    else:
        raise IdentityRequired(
            f"identity must be a dict with user_id or have a user_id attribute, got "
            f"{type(identity).__name__}"
        )
    user_id = data.get("user_id")
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        user_id = str(user_id)
    if not isinstance(user_id, str) or not user_id.strip():
        raise IdentityRequired("identity needs a non-empty user_id")
    data["user_id"] = user_id
    try:
        return json.loads(json.dumps(data))
    except (TypeError, ValueError) as exc:
        raise IdentityRequired(f"identity must be JSON-serializable: {exc}") from exc


def resolve_identity(explicit: Any) -> dict[str, Any]:
    """``explicit`` if given, else the running task's identity, else ``IdentityRequired``."""
    if explicit is not None:
        return normalize_identity(explicit)
    ctx = current_task_context.get()
    if ctx is not None:
        return dict(ctx.identity)
    raise IdentityRequired(
        "enqueue outside a task needs identity=...: pass the user, or system_identity(name)"
    )


def user_identity(user_id: str, **claims: Any) -> dict[str, Any]:
    """An identity for ``user_id`` with optional extra claims."""
    return normalize_identity({"user_id": user_id, **claims})


def system_identity(name: str) -> dict[str, Any]:
    """An identity for server-side work (webhooks, cron), owner ``__system__:<name>``."""
    return {"user_id": system_user_id(name)}
