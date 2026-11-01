"""Startup gate: refuse to serve while a backend the server needs cannot be reached.

A server whose checkpointer or store is down still answers ``/ping``, and before this gate it
served every other request with an error. The lifespan now calls ``aping()`` on what is bound
(the checkpointer, the store and the API's cache store, as readiness does in
``readiness.py``) before the app serves, retrying with a short backoff for
``STARTUP_CHECK_TIMEOUT_SECONDS`` so services started together can come up in any order. When
one still fails, :class:`StartupCheckError` stops the boot and names each component, its class,
where its setting came from and its endpoint when the component describes one (never
credentials). ``STARTUP_CHECKS=false`` turns the gate off.

The cache store is fatal only when a configured feature fails closed without it
(``cache_store.token_revocation``, or ``cache_store.rate_limit`` with ``fail_open: false``; see
``readiness.cache_required_by``). Otherwise it is checked once and an unreachable one logs one
WARNING: the run locks and caps fall back to this process, and the server starts.

Unlike readiness, a required backend's outage is not tolerated here: a backend that is wrong at
boot is a deployment mistake, not a transient outage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from tenxgraph_api.src.app.core.cache_store.loader import CacheStoreSource
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.utils.log_sanitizer import redact_text

from .readiness import (
    CHECK_TIMEOUT_SECONDS,
    FAIL,
    OK,
    cache_required_by,
    check_outcome,
    ping_component,
    readiness_targets,
)


logger = logging.getLogger("tenxgraph_api.startup")

FIRST_RETRY_DELAY_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 5.0
HINT = "Start the backend, fix its URL, or set STARTUP_CHECKS=false to skip this check."


class StartupCheckError(RuntimeError):
    """A required backend stayed unreachable for the whole startup window."""


@dataclass
class Target:
    """One bound backend to check: a name, its probe and what to call it in messages."""

    name: str
    check: Callable[[], Awaitable[None]]
    details: list[str] = field(default_factory=list)
    required: bool = True


def _component_details(component: Any) -> list[str]:
    """The class name and, when the component has a ``describe()``, its endpoint."""
    details = [type(component).__name__]
    describe = getattr(component, "describe", None)
    if callable(describe):
        try:
            where = describe()
        except Exception:  # a custom component's describe must not break boot
            where = None
        if isinstance(where, str) and where:
            details.append(where)
    return details


def _cache_source(container: Any) -> str | None:
    found = container.try_get(CacheStoreSource) if CacheStoreSource in container else None
    return getattr(found, "text", None)


def _graph_config(container: Any) -> Any:
    return container.try_get(GraphConfig) if GraphConfig in container else None


def startup_targets(container: Any) -> list[Target]:
    """The checkpointer, store and cache store bound in ``container``; unbound ones left out."""
    checkpointer, store, cache = readiness_targets(container)
    targets = []
    if checkpointer is not None:
        targets.append(
            Target(
                "checkpointer",
                lambda: ping_component(checkpointer),
                _component_details(checkpointer),
            )
        )
    if store is not None:
        targets.append(Target("store", lambda: ping_component(store), _component_details(store)))
    if cache is not None:
        details = _component_details(cache)
        if source := _cache_source(container):
            details.insert(1, source)
        required_by = cache_required_by(_graph_config(container))
        if required_by:
            details.append(f"required by {' and '.join(required_by)}")
        targets.append(
            Target("cache", lambda: ping_component(cache), details, required=bool(required_by))
        )
    return targets


def _describe(target: Target) -> str:
    return f"{target.name} ({', '.join(target.details)})" if target.details else target.name


def _error_text(error: BaseException | None, timeout: float) -> str:
    if isinstance(error, TimeoutError) or error is None:
        return f"timed out after {timeout}s"
    text = str(error).strip()
    return redact_text(f"{type(error).__name__}: {text}" if text else type(error).__name__)


async def wait_for_backends(
    targets: list[Target],
    *,
    timeout: float,
    attempt_timeout: float = CHECK_TIMEOUT_SECONDS,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Check ``targets`` until all pass or ``timeout`` seconds run out; raise if any still fail.

    Each round checks the components still failing, concurrently, each bounded by
    ``attempt_timeout``. There is always at least one round. A component with nothing to check
    (no ``aping``) is skipped with an INFO line.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    delay = FIRST_RETRY_DELAY_SECONDS
    pending = list(targets)
    errors: dict[str, str] = {}
    attempt = 0
    while True:
        attempt += 1
        outcomes = await asyncio.gather(
            *(check_outcome(target.check, attempt_timeout) for target in pending)
        )
        still_failing = []
        for target, (result, error) in zip(pending, outcomes, strict=True):
            if result == OK:
                errors.pop(target.name, None)
                if attempt > 1:
                    logger.info("Startup check: %s is reachable", _describe(target))
            elif result == FAIL:
                errors[target.name] = _error_text(error, attempt_timeout)
                still_failing.append(target)
            else:
                logger.info("Startup check: %s has nothing to check; skipped", target.name)
        pending = still_failing
        if not pending:
            return
        remaining = deadline - loop.time()
        for target in pending:
            logger.warning(
                "Startup check: %s is unreachable (attempt %d): %s%s",
                _describe(target),
                attempt,
                errors[target.name],
                f"; retrying for up to {remaining:.0f}s more" if remaining > 0 else "",
            )
        if remaining <= 0:
            break
        await sleep(min(delay, remaining))
        delay = min(delay * 2, MAX_RETRY_DELAY_SECONDS)

    lines = [f"  - {_describe(target)}: {errors[target.name]}" for target in pending]
    raise StartupCheckError(
        "\n".join(
            [
                f"Refusing to start: {len(pending)} backend(s) unreachable after {timeout:g}s:",
                *lines,
                HINT,
            ]
        )
    )


def _label(target: Target) -> str:
    """``RedisCacheStore (cache_store.url, localhost:6379)``: the class, then the rest."""
    if not target.details:
        return target.name
    first, *rest = target.details
    return f"{first} ({', '.join(rest)})" if rest else first


async def _check_optional(target: Target, attempt_timeout: float) -> bool:
    """One attempt at a backend the server can run without; one WARNING when it fails."""
    result, error = await check_outcome(target.check, attempt_timeout)
    if result != FAIL:
        return True
    logger.warning(
        "Startup check: cache store %s unreachable: locks and caps fall back to per process "
        "until it answers (%s). No configured feature needs it, so the server starts.",
        _label(target),
        _error_text(error, attempt_timeout),
    )
    return False


async def check_backends_at_startup(
    container: Any, *, timeout: float, attempt_timeout: float = CHECK_TIMEOUT_SECONDS
) -> None:
    """Run the startup gate for the app's container; log and raise when it fails."""
    targets = startup_targets(container)
    if not targets:
        logger.debug("Startup check: no backends configured")
        return
    required = [target for target in targets if target.required]
    reachable = list(required)
    for target in targets:
        if not target.required and await _check_optional(target, attempt_timeout):
            reachable.append(target)
    try:
        await wait_for_backends(required, timeout=timeout, attempt_timeout=attempt_timeout)
    except StartupCheckError as exc:
        # The text as the format string: an argument would have its line breaks escaped.
        logger.critical(str(exc))
        raise
    logger.info(
        "Startup check passed: %s", ", ".join(target.name for target in reachable) or "nothing"
    )
