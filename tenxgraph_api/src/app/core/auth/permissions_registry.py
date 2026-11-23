"""The one list of permissions (``resource:action``) the API knows about.

``REGISTRY`` holds the route permissions: a closed list. ``tools`` is the one open resource:
its actions are tool names (``tools:refund``, ``tools:*``), checked by core's ``ToolGuard``
with ``Identity.can``, never by a route.
"""

from __future__ import annotations

import re
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

# No tool permissions: with no roles, a tool behind a ToolGuard rule is denied (fail closed).
DEFAULT_PERMISSIONS: frozenset[str] = frozenset(
    p for p in ALL_PERMISSIONS if not p.startswith("admin:")
)

TOOLS = "tools"
TOOLS_ALL = "tools:*"
# Tool names as providers and MCP accept them: letters, digits, ``_``, ``-`` and ``.``.
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


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
            result |= ALL_PERMISSIONS | {TOOLS_ALL}
            continue
        resource, sep, action = pattern.partition(":")
        if not sep or not resource or not action:
            raise ValueError(f"permission {pattern!r} is malformed; expected 'resource:action'")
        if resource == TOOLS:
            if action != "*" and not TOOL_NAME.fullmatch(action):
                raise ValueError(
                    f"permission {pattern!r} has an invalid tool name; use 1-128 of "
                    "A-Z a-z 0-9 _ - . or '*'"
                )
            result.add(pattern)
            continue
        if resource not in REGISTRY:
            raise ValueError(f"permission {pattern!r} has unknown resource {resource!r}")
        if action == "*":
            result |= {f"{resource}:{a}" for a in REGISTRY[resource]}
        elif action in REGISTRY[resource]:
            result.add(pattern)
        else:
            raise ValueError(f"permission {pattern!r} has unknown action {action!r}")
    return frozenset(result)


def intersect(a: frozenset[str], b: frozenset[str]) -> frozenset[str]:
    """Permissions both sets allow. ``tools:*`` in one set keeps the other set's tool names."""
    return frozenset(p for p in a | b if _allows(a, p) and _allows(b, p))


def _allows(permissions: frozenset[str], permission: str) -> bool:
    if permission in permissions:
        return True
    return permission.startswith(f"{TOOLS}:") and TOOLS_ALL in permissions
