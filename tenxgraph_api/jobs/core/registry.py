"""Task registry: name -> ``Task``.

Filled by importing ``worker.modules`` at startup (Sprint 3), in the API process and in every
worker, so the same names exist everywhere. A duplicate name raises at registration.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

from tenxgraph_api.jobs.errors import DuplicateTaskName, TaskNotFound


if TYPE_CHECKING:
    from .task import Task


_tasks: dict[str, Task] = {}


def register(task: Task) -> None:
    existing = _tasks.get(task.name)
    if existing is not None:
        raise DuplicateTaskName(
            f"task name {task.name!r} is already used by {existing.qualname}; "
            f"set name= on {task.qualname}"
        )
    _tasks[task.name] = task


def get_task(name: str) -> Task:
    """The registered task (the decorated function object). Raises ``TaskNotFound``."""
    try:
        return _tasks[name]
    except KeyError:
        raise TaskNotFound(f"no task named {name!r} is registered") from None


def all_tasks() -> dict[str, Task]:
    return dict(_tasks)


def unregister(name: str) -> None:
    _tasks.pop(name, None)


@contextlib.contextmanager
def isolated_registry() -> Iterator[None]:
    """Restore the registry after the block (tests that declare tasks inside a function)."""
    saved = dict(_tasks)
    try:
        yield
    finally:
        _tasks.clear()
        _tasks.update(saved)
