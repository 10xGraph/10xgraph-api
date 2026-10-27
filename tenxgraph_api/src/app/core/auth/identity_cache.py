"""Verify a credential once per request, even when two layers need the identity.

With ``rate_limit.by == "user"`` the rate limiter verifies the bearer token to pick a bucket,
and ``RequirePermission`` then verifies the same token again for the route. The first result
is kept on the request scope, keyed by the backend and the exact token, so the second check
can reuse it.

Only auth backends that set ``BaseAuth.reusable_within_request = True`` take part: reusing a
result skips the second ``authenticate`` call, so a backend whose ``authenticate`` has side
effects on the response (cookies, refreshed tokens) must keep the default ``False``.
"""

from __future__ import annotations

import copy
from typing import Any

from starlette.requests import HTTPConnection

from tenxgraph_api.src.app.core.auth.reserved_identity import identity_is_reserved


_STATE_KEY = "tenxgraph_verified_identity"


def _state(connection: HTTPConnection) -> dict[str, Any] | None:
    scope = getattr(connection, "scope", None)
    if not isinstance(scope, dict):
        return None
    return scope.setdefault("state", {})


def _reusable(backend: Any) -> bool:
    """Whether ``backend`` declared its own ``authenticate`` reusable.

    The flag counts only when set by the class that defines the ``authenticate`` in use. A
    subclass of ``JwtAuth`` that overrides ``authenticate`` (cookies, a DB lookup) inherits
    ``JwtAuth``'s flag but not its purity, so it is not reused unless it sets the flag itself.
    """
    for cls in type(backend).__mro__:
        if "authenticate" in vars(cls):
            return vars(cls).get("reusable_within_request") is True
    return False


def remember_identity(
    connection: HTTPConnection, backend: Any, token: str, identity: dict[str, Any]
) -> None:
    """Keep a successfully verified identity for the rest of this request."""
    state = _state(connection)
    if (
        state is None
        or not _reusable(backend)
        or not isinstance(identity, dict)
        or identity_is_reserved(identity)
    ):
        return
    state[_STATE_KEY] = (id(backend), token, copy.deepcopy(identity))


def recall_identity(connection: HTTPConnection, backend: Any, token: str) -> dict[str, Any] | None:
    """The identity this request already verified for ``token`` with ``backend``, or None.

    Returns a copy: callers add server-owned keys (``authz``) to the identity they get.
    """
    state = _state(connection)
    if state is None or not _reusable(backend):
        return None
    cached = state.get(_STATE_KEY)
    if not cached:
        return None
    backend_id, cached_token, identity = cached
    if backend_id != id(backend) or cached_token != token:
        return None
    if identity_is_reserved(identity):
        return None
    return copy.deepcopy(identity)
