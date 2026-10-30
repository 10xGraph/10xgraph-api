"""Boot-time invariant: every route must go through the authorization layer.

Authorization is applied per-handler via ``Depends(RequirePermission(...))``, which is
precise (each endpoint declares its own resource/action) but easy to forget on a new
endpoint -- and a forgotten guard ships an open endpoint. This module makes that failure
happen at startup instead: :func:`assert_all_routes_protected` refuses to boot if any
non-public route lacks a ``RequirePermission`` dependency.

It adds zero per-request cost and turns "someone forgot the guard" into a loud deploy-time
error rather than a silent production hole.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute

from tenxgraph_api.src.app.core.auth.permissions import RequirePermission


logger = logging.getLogger("tenxgraph_api.route_guard")

# Paths that are intentionally public (no authorization). Keep this list tiny and explicit.
DEFAULT_PUBLIC_PATHS = frozenset(
    {
        "/ping",
        "/health/ready",
    }
)


def _has_permission_guard(dependant) -> bool:
    """True if ``RequirePermission`` appears anywhere in the dependant subtree."""
    for dep in getattr(dependant, "dependencies", ()):
        if isinstance(getattr(dep, "call", None), RequirePermission):
            return True
        if _has_permission_guard(dep):
            return True
    return False


def _is_guard(dependency: object) -> bool:
    """True for a ``Depends(RequirePermission(...))`` given at ``include_router`` time."""
    return isinstance(getattr(dependency, "dependency", None), RequirePermission)


def _iter_routes(routes, prefix: str = "", guarded: bool = False):
    """Yield ``(full_path, route, guarded_by_include)`` for every route, however nested.

    FastAPI 0.139 keeps ``include_router`` lazy: ``app.routes`` holds one wrapper per
    included router instead of copies of its routes, so a plain walk of ``app.routes`` sees
    no ``APIRoute`` at all. The wrapper carries the router and its include prefix and
    dependencies; older releases copy the routes, which the plain branch handles.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        if included is not None and context is not None:
            yield from _iter_routes(
                included.routes,
                prefix + (getattr(context, "prefix", "") or ""),
                guarded or any(_is_guard(dep) for dep in getattr(context, "dependencies", [])),
            )
        elif isinstance(route, APIRoute | APIWebSocketRoute):
            yield prefix + route.path, route, guarded


def find_unprotected_routes(
    app: FastAPI, public_paths: frozenset[str] = DEFAULT_PUBLIC_PATHS
) -> list[str]:
    """Return ``"METHODS path"`` for every route missing a RequirePermission guard.

    Starlette infra routes (docs, openapi.json, redoc) are not APIRoutes and are skipped.
    """
    unprotected: list[str] = []
    for path, route, guarded in _iter_routes(app.routes):
        if path in public_paths or guarded:
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None or not _has_permission_guard(dependant):
            methods = ",".join(sorted(getattr(route, "methods", None) or ["WS"]))
            unprotected.append(f"{methods} {path}")
    return unprotected


def assert_all_routes_protected(
    app: FastAPI, public_paths: frozenset[str] = DEFAULT_PUBLIC_PATHS
) -> None:
    """Refuse to start if any non-public route lacks a RequirePermission guard.

    Raises:
        RuntimeError: listing every unprotected route.
    """
    unprotected = find_unprotected_routes(app, public_paths)
    if unprotected:
        raise RuntimeError(
            "Refusing to start: the following routes are not protected by "
            "RequirePermission. Add the dependency, or add the path to the public "
            "allowlist if it is intentionally open:\n  - " + "\n  - ".join(unprotected)
        )
    logger.debug("Route protection invariant OK (%d routes checked).", len(app.routes))
