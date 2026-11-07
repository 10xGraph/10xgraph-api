"""The reserved-argument check: a caller may set only a task's plain arguments.

TaskIQ applies caller kwargs over injected values (``kwargs.update(message.kwargs)`` in its
receiver), so ``graph=`` from a caller would replace the injected graph. This is a security
check, used by the HTTP routes and the worker middleware.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from typing import Any

from taskiq import AsyncTaskiqDecoratedTask
from taskiq_dependencies.dependency import Dependency


# Run-config keys only the server writes (``config`` itself is filled from the message label).
RESERVED_NAMES = frozenset({"config", "user", "user_id", "authz"})

_POSITIONAL = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)


class ReservedArgumentError(ValueError):
    """A caller tried to set an injected or reserved argument (Python rejects unknown or
    repeated ones at call time)."""


def injected_names(func: Callable[..., Any]) -> set[str]:
    """Parameters TaskIQ fills: ``TaskiqDepends()`` / ``InjectTaskiq(...)`` in ``Annotated``
    metadata or as the default (``Context`` and ``TaskContext`` params use one of these)."""
    try:
        hints = typing.get_type_hints(func, include_extras=True)
    except Exception:  # unresolvable forward reference: fall back to raw annotations
        hints = {}
    names = set()
    for name, param in inspect.signature(func).parameters.items():
        annotation = hints.get(name, param.annotation)
        metadata = getattr(annotation, "__metadata__", ())
        if isinstance(param.default, Dependency) or any(
            isinstance(item, Dependency) for item in metadata
        ):
            names.add(name)
    return names


def _max_positional(params: list[inspect.Parameter], blocked: set[str]) -> int | None:
    """How many positional args can be given before one would land on a blocked parameter
    (None: unlimited, through ``*args``)."""
    count = 0
    for param in params:
        if param.kind in _POSITIONAL:
            if param.name in blocked:
                return count
            count += 1
        elif param.kind is inspect.Parameter.VAR_POSITIONAL:
            return None
    return count


def check_arguments(
    task: AsyncTaskiqDecoratedTask[Any, Any], args: tuple[Any, ...] | list[Any], kwargs: dict
) -> None:
    """Raise ``ReservedArgumentError`` unless ``args`` / ``kwargs`` set only plain arguments."""
    func = task.original_func
    params = list(inspect.signature(func).parameters.values())
    blocked = injected_names(func) | RESERVED_NAMES
    name = task.task_name

    bad = sorted(set(kwargs) & blocked)
    if bad:
        raise ReservedArgumentError(f"Task {name!r}: arguments {bad} are set by the server")

    limit = _max_positional(params, blocked)
    if limit is not None and len(args) > limit:
        raise ReservedArgumentError(
            f"Task {name!r} takes at most {limit} positional arguments, got {len(args)}"
        )
