"""Worker middlewares: ``TaskMiddleware`` (config, argument check, cancel, status) and
``MonitoringMiddleware`` (logs and publisher events).

A TaskIQ middleware cannot skip a task: ``pre_execute`` runs outside any ``try`` in the
receiver, so a raise there leaves the message unacked (redelivered forever on a stream) and the
caller waiting. Ours never raise. To stop a run, ``pre_execute`` puts the error in a ContextVar
and the guard that ``wrap_tasks`` puts around every task function raises it before the body
runs; the caller gets an error result. The ContextVar does not reach a process-pool executor.

With auth on, the run executes as the identity its config names, rebuilt from the current auth
config (A12): a role removed or a permission taken away since the run was queued stops it.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from taskiq import AsyncBroker, TaskiqMessage, TaskiqMiddleware, TaskiqResult
from taskiq.exceptions import NoResultError
from tenxgraph.core.authz import Identity

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.arguments import ReservedArgumentError, check_arguments, injected_names
from tenxgraph_api.jobs.context import TaskCancelled
from tenxgraph_api.jobs.monitoring import MonitoringMiddleware


if TYPE_CHECKING:
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.auth.auth_config import SystemIdentityConfig
    from tenxgraph_api.src.app.core.auth.rbac import RbacConfig


logger = logging.getLogger("tenxgraph_api.jobs")

__all__ = ["MonitoringMiddleware", "TaskMiddleware", "wrap_tasks"]

# The error the guard raises instead of running the task body (None: run it).
_ABORT: contextvars.ContextVar[BaseException | None] = contextvars.ContextVar(
    "tenxgraph_task_abort", default=None
)
_GUARD_MARK = "__tenxgraph_guard__"
# Task label naming the ``auth.system_identities`` entry a run without a caller runs as.
IDENTITY_LABEL = "identity"
_REFUSED = "This run's identity may not run this task"
_warned_auth_off: set[str] = set()


def _guard(func: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_guard(*args: Any, **kwargs: Any) -> Any:
            if (error := _ABORT.get()) is not None:
                raise error
            return await func(*args, **kwargs)

        wrapper: Callable[..., Any] = async_guard
    else:

        @functools.wraps(func)
        def sync_guard(*args: Any, **kwargs: Any) -> Any:
            if (error := _ABORT.get()) is not None:
                raise error
            return func(*args, **kwargs)

        wrapper = sync_guard
    setattr(wrapper, _GUARD_MARK, True)
    return wrapper


def wrap_tasks(broker: AsyncBroker) -> None:
    """Put the guard around every registered task's function (the receiver calls
    ``task.original_func``). Safe to call more than once."""
    for task in broker.get_all_tasks().values():
        if not getattr(task.original_func, _GUARD_MARK, False):
            task.original_func = _guard(task.original_func)


class TaskMiddleware(TaskiqMiddleware):
    """Run config into ``config``, the reserved-argument re-check, cancel and the status record.

    A message whose kwargs break the reserved-argument rule is not run: injected names are
    removed from its kwargs (so injection wins even without the guard) and the guard raises
    ``ReservedArgumentError``. Failing the run is safer than dropping arguments and running
    the task with different input than the caller sent.
    """

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        _ABORT.set(None)
        try:
            await self._prepare(message)
        except Exception:
            logger.exception("Task %s (%s): pre_execute failed", message.task_name, message.task_id)
        return message

    async def _prepare(self, message: TaskiqMessage) -> None:
        task = self.broker.find_task(message.task_name)
        if task is None:
            return
        config = state.parse_config(message.labels)
        config["task_id"] = message.task_id
        # ``config`` always comes from the label: a retry re-sends the kwargs we set here.
        message.kwargs.pop("config", None)
        try:
            check_arguments(task, message.args, message.kwargs)
        except ReservedArgumentError as error:
            for name in injected_names(task.original_func) & set(message.kwargs):
                message.kwargs.pop(name)
            _ABORT.set(error)
        try:
            if await _run_identity(state.container_for(self.broker), task, config):
                # TaskContext reads the label: give it the rebuilt identity too.
                message.labels[state.CONFIG_LABEL] = json.dumps(config, default=str)
        except PermissionError as error:
            logger.warning("Task %s (%s) not run: %s", task.task_name, message.task_id, error)
            _ABORT.set(_ABORT.get() or error)
        except Exception:  # fail closed: an identity that cannot be checked does not run
            logger.exception("Task %s (%s): identity check failed", task.task_name, message.task_id)
            _ABORT.set(_ABORT.get() or PermissionError(_REFUSED))
        if "config" in inspect.signature(task.original_func).parameters:
            message.kwargs["config"] = config

        store = state.cache_store_for(self.broker)
        if store is None:
            return
        status: state.Status = "running"
        if await state.is_cancel_flag_set(store, message.task_id):
            _ABORT.set(TaskCancelled("cancelled before start"))
            status = "cancelled"
        await state.update_status(store, message.task_id, message.task_name, status, config)

    async def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        _ABORT.set(None)  # the in-memory broker can run a task in the caller's context
        try:
            store = state.cache_store_for(self.broker)
            if store is None:
                return
            config = state.parse_config(message.labels)
            status = _final_status(result)
            await state.update_status(store, message.task_id, message.task_name, status, config)
        except Exception:
            logger.exception("Task %s (%s): status not saved", message.task_name, message.task_id)


def _final_status(result: TaskiqResult[Any]) -> state.Status:
    if isinstance(result.error, NoResultError):
        return "queued"  # SimpleRetryMiddleware sent it again
    if isinstance(result.error, TaskCancelled):
        return "cancelled"
    return "failure" if result.is_err else "success"


async def _run_identity(container: InjectQ, task: Any, config: dict[str, Any]) -> bool:
    """Set ``config["user"]`` to the identity this run executes as; True when it was set.

    With a ``BaseAuth`` bound: a stored identity is rebuilt with the current role config
    (``resolve`` on its claims; a system identity from its current ``auth.system_identities``
    entry), and a run without one takes the task's ``identity`` label. The label never replaces
    a caller's identity. Then ``authorize(identity, "tasks:trigger", task_name)``. Token expiry
    and revocation are not checked: there is no live token. With auth off, nothing changes.

    Raises:
        PermissionError: the run must not execute.
    """
    from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
    from tenxgraph_api.src.app.core.auth.rbac import InvalidClaimsError, resolve
    from tenxgraph_api.src.app.core.auth.reserved_identity import (
        SYSTEM_USER_PREFIX,
        identity_is_reserved,
    )

    user = config.get("user")
    label = task.labels.get(IDENTITY_LABEL)
    auth = container.try_get(BaseAuth)
    if auth is None:
        if user is None and label and task.task_name not in _warned_auth_off:
            _warned_auth_off.add(task.task_name)
            logger.warning(
                "Task %s has identity=%r but auth is off: it runs without an identity",
                task.task_name,
                label,
            )
        return False
    if user is None and not label:
        return False  # no caller and no system identity: runs as before

    try:
        if user is None:
            identity = _system_identity(container, str(label), auth.rbac)
        else:
            stored = Identity.model_validate(user)
            if stored.kind == "system":
                name = stored.user_id.removeprefix(f"{SYSTEM_USER_PREFIX}:")
                if name == stored.user_id:
                    raise PermissionError(_REFUSED)
                identity = _system_identity(container, name, auth.rbac)
            else:
                identity = resolve(dict(stored), auth.rbac)
                if identity_is_reserved(identity):
                    raise PermissionError(_REFUSED)
    except (ValueError, InvalidClaimsError) as exc:  # ValidationError is a ValueError
        logger.warning("Task %s: stored identity is not valid: %s", task.task_name, exc)
        raise PermissionError(_REFUSED) from None
    if not await auth.authorize(identity, "tasks:trigger", task.task_name):
        raise PermissionError(_REFUSED)
    config["user"] = identity.model_dump(mode="json")
    return True


def _system_identity(container: InjectQ, name: str, rbac: RbacConfig | None) -> Identity:
    """``Identity(kind="system", user_id="__system__:<name>")`` with the roles and tenant of the
    current ``auth.system_identities`` entry, resolved like a token holding those roles."""
    from tenxgraph_api.src.app.core.auth.rbac import resolve
    from tenxgraph_api.src.app.core.auth.reserved_identity import SYSTEM_USER_PREFIX
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

    graph_config = container.try_get(GraphConfig)
    auth_config = graph_config.auth_config() if graph_config is not None else None
    entries: dict[str, SystemIdentityConfig] = auth_config.system_identities if auth_config else {}
    entry = entries.get(name)
    if entry is None:
        raise PermissionError(f"System identity {name!r} is not configured")
    roles_claim = (rbac.roles_claim if rbac is not None else None) or "roles"
    user_id = f"{SYSTEM_USER_PREFIX}:{name}"
    claims = {**entry.tenant, roles_claim: list(entry.roles), "user_id": user_id}
    return resolve(claims, rbac).model_copy(update={"kind": "system"})
