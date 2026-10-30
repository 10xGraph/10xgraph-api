"""Startup gate: refuse to serve while a configured backend cannot be reached.

A server whose Redis, Postgres or vector store is down still answers ``/ping``, and before
this gate it served every other request with an error (a fail-closed rate limiter answered
429 to everything). The lifespan now runs the readiness probes (``readiness.py``) for each
configured backend before the app serves, retrying with a short backoff for
``STARTUP_CHECK_TIMEOUT_SECONDS`` so services started together can come up in any order. When
one still fails, :class:`StartupCheckError` stops the boot and names each component, its
``host:port`` (never credentials) and the error. ``STARTUP_CHECKS=false`` turns the gate off.

Unlike readiness, a Redis outage is not tolerated here: a configured backend that is wrong at
boot is a deployment mistake, not a transient outage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from tenxgraph_api.src.app.core.utils.log_sanitizer import redact_text

from .readiness import (
    CHECK_TIMEOUT_SECONDS,
    FAIL,
    OK,
    check_checkpointer,
    check_outcome,
    check_redis,
    check_store,
    readiness_targets,
)


logger = logging.getLogger("tenxgraph_api.startup")

FIRST_RETRY_DELAY_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 5.0
HINT = "Start Redis/Postgres/Qdrant, or set STARTUP_CHECKS=false to skip this check."


class StartupCheckError(RuntimeError):
    """A configured backend stayed unreachable for the whole startup window."""


@dataclass
class Target:
    """One configured backend to check: a name, its probe and where it lives."""

    name: str
    check: Callable[[], Awaitable[None]]
    endpoints: list[str] = field(default_factory=list)


def host_port(url: Any) -> str | None:
    """``host:port`` of a URL or DSN, without user, password, path or query; None if unknown."""
    if not isinstance(url, str) or "://" not in url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if port else host


def _redis_client_endpoint(client: Any) -> str | None:
    """``host:port`` a redis-py client connects to, from its pool's connection settings."""
    kwargs = getattr(getattr(client, "connection_pool", None), "connection_kwargs", None)
    if not isinstance(kwargs, dict) or not kwargs.get("host"):
        return None
    return f"{kwargs['host']}:{kwargs.get('port', 6379)}"


def _checkpointer_endpoints(checkpointer: Any) -> list[str]:
    """Best effort: the Postgres and Redis a ``PgCheckpointer`` uses."""
    found = []
    pool_config = getattr(checkpointer, "_pg_pool_config", None)
    dsn = pool_config.get("postgres_dsn") if isinstance(pool_config, dict) else None
    if (where := host_port(dsn)) is not None:
        found.append(f"postgres {where}")
    if (where := _redis_client_endpoint(getattr(checkpointer, "redis", None))) is not None:
        found.append(f"redis {where}")
    return found


def _store_endpoints(store: Any) -> list[str]:
    """Best effort: the server a ``QdrantStore`` talks to (its client's REST address)."""
    remote = getattr(getattr(store, "client", None), "_client", None)
    where = host_port(getattr(remote, "rest_uri", None))
    return [where] if where else []


def startup_targets(container: Any, rate_limit_backend: Any = None) -> list[Target]:
    """Every configured backend, as readiness sees it, plus the rate limiter's own Redis.

    Components that are not configured are left out. The rate limiter's Redis is added only
    when it was built from its own URL and that URL differs from the API's Redis.
    """
    checkpointer, store, redis_url = readiness_targets(container)
    targets = []
    if checkpointer is not None:
        targets.append(
            Target(
                "checkpointer",
                lambda: check_checkpointer(checkpointer),
                _checkpointer_endpoints(checkpointer),
            )
        )
    if store is not None:
        targets.append(Target("store", lambda: check_store(store), _store_endpoints(store)))
    if redis_url:
        targets.append(
            Target("redis", lambda: check_redis(redis_url), [host_port(redis_url) or "?"])
        )
    limiter_url = getattr(rate_limit_backend, "redis_url", None)
    if isinstance(limiter_url, str) and limiter_url and limiter_url != redis_url:
        targets.append(
            Target(
                "rate_limit redis",
                lambda: check_redis(limiter_url),
                [host_port(limiter_url) or "?"],
            )
        )
    elif hasattr(rate_limit_backend, "redis_url") and not limiter_url:
        logger.info(
            "Startup check: the rate limiter uses a Redis client bound in the container; "
            "it is not checked at startup."
        )
    return targets


def _describe(target: Target) -> str:
    return f"{target.name} ({', '.join(target.endpoints)})" if target.endpoints else target.name


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
    (a store without ``aping``/``ping``) is skipped with an INFO line.
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
                f"Refusing to start: {len(pending)} configured backend(s) unreachable after "
                f"{timeout:g}s:",
                *lines,
                HINT,
            ]
        )
    )


async def check_backends_at_startup(
    container: Any, *, timeout: float, rate_limit_backend: Any = None
) -> None:
    """Run the startup gate for the app's container; log and raise when it fails."""
    targets = startup_targets(container, rate_limit_backend)
    if not targets:
        logger.debug("Startup check: no backends configured")
        return
    try:
        await wait_for_backends(targets, timeout=timeout)
    except StartupCheckError as exc:
        # The text as the format string: an argument would have its line breaks escaped.
        logger.critical(str(exc))
        raise
    logger.info("Startup check passed: %s", ", ".join(target.name for target in targets))
