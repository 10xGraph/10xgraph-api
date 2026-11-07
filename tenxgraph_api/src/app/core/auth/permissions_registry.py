"""The one list of permissions (``resource:action``) the API knows about."""

from __future__ import annotations

from collections.abc import Iterable


REGISTRY: dict[str, tuple[str, ...]] = {
    "graph": ("read", "invoke", "stream", "stop", "fix"),
    "threads": ("read", "write", "delete"),
    "store": ("read", "write", "delete"),
    "files": ("read", "upload", "delete"),
    "config": ("read",),
    "tasks": ("trigger", "read", "cancel"),
    "admin": ("read", "write"),
}

ALL_PERMISSIONS: frozenset[str] = frozenset(
    f"{resource}:{action}" for resource, actions in REGISTRY.items() for action in actions
)

DEFAULT_PERMISSIONS: frozenset[str] = frozenset(
    p for p in ALL_PERMISSIONS if not p.startswith("admin:")
)


def expand(patterns: Iterable[str]) -> frozenset[str]:
    """Turn ``*``, ``resource:*`` and concrete names into concrete permissions.

    Raises:
        ValueError: an entry is malformed or names an unknown resource or action.
    """
    result: set[str] = set()
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise ValueError(f"permission {pattern!r} is not a string")
        if pattern == "*":
            result |= ALL_PERMISSIONS
            continue
        resource, sep, action = pattern.partition(":")
        if not sep or not resource or not action:
            raise ValueError(f"permission {pattern!r} is malformed; expected 'resource:action'")
        if resource not in REGISTRY:
            raise ValueError(f"permission {pattern!r} has unknown resource {resource!r}")
        if action == "*":
            result |= {f"{resource}:{a}" for a in REGISTRY[resource]}
        elif action in REGISTRY[resource]:
            result.add(pattern)
        else:
            raise ValueError(f"permission {pattern!r} has unknown action {action!r}")
    return frozenset(result)
