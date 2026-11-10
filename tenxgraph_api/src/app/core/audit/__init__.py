"""Audit events: who accessed or changed what, when, and whether it was allowed.

One emit function, :func:`audit_event`, called from the few places every request passes
through (the auth dependency, the thread lookup, the 404 handler; changes from the routers).
Metadata only: never prompts, answers, message text, file contents, tokens or claims beyond
ids. Request id, transport and client address come from the request context; callers pass only
what they know.

A sink error never fails the request: it is logged at ERROR and the request continues. With
audit off (the default) :func:`audit_event` returns at once.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core.audit.config import AuditConfig
from tenxgraph_api.src.app.core.config.metrics import count_audit_sink_error
from tenxgraph_api.src.app.core.request_context import get_request_context


logger = logging.getLogger("tenxgraph_api")

EventType = Literal["access", "auth", "change"]
Result = Literal["allowed", "denied", "not_found", "failed", "success"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AuditEvent(BaseModel):
    """One audit record; ``model_dump()`` gives the JSON fields (schema version ``v``)."""

    v: Literal[1] = 1
    ts: str = Field(default_factory=_now)
    event: EventType
    request_id: str | None = None
    transport: Literal["http", "ws"] | None = None
    user_id: str | None = None
    tenant: dict[str, str] | None = None
    roles: list[str] | None = None
    action: str | None = None
    resource: str | None = None
    result: Result
    reason: str | None = None
    client_ip: str | None = None
    count: int | None = None


class BaseAuditSink:
    """Where audit events go. Subclass and name it in ``audit.sinks`` as ``"module:Class"``.

    ``emit`` should be quick; a raised exception is logged and the request continues.
    """

    async def emit(self, event: AuditEvent) -> None:
        raise NotImplementedError


class _Active:
    """The sinks in use (set once at boot) and the ``include_reads`` switch."""

    sinks: tuple[BaseAuditSink, ...] = ()
    include_reads = True


def set_sinks(sinks: list[BaseAuditSink], *, include_reads: bool = True) -> None:
    """Replace the active sinks (empty turns audit off)."""
    _Active.sinks = tuple(sinks)
    _Active.include_reads = include_reads


def audit_enabled() -> bool:
    return bool(_Active.sinks)


def configure_audit(config: AuditConfig, *, auth_enabled: bool) -> None:
    """Build the sinks from the ``audit`` block, once at boot."""
    from tenxgraph_api.src.app.core.audit.sinks import build_sinks

    if not config.enabled:
        set_sinks([])
        return
    if not auth_enabled:
        logger.warning("Audit log is on but auth is off: audit events carry user_id null")
    set_sinks(build_sinks(config.sinks), include_reads=config.include_reads)


async def audit_event(
    event: EventType,
    result: Result,
    *,
    action: str | None = None,
    resource: str | None = None,
    reason: str | None = None,
    identity: Identity | None = None,
    user_id: str | None = None,
    count: int | None = None,
) -> None:
    """Send one event to every sink. ``identity`` (when known) gives user, tenant and roles."""
    if not _Active.sinks:
        return
    if not _Active.include_reads and event == "access" and result == "allowed":
        return
    ctx = get_request_context()
    if identity is not None:
        user_id = identity.user_id
    elif user_id is None and ctx is not None:
        user_id = ctx.user_id
    record = AuditEvent(
        event=event,
        request_id=ctx.request_id if ctx else None,
        transport=ctx.transport if ctx else None,
        user_id=user_id,
        tenant=dict(identity.tenant) if identity is not None else None,
        roles=sorted(identity.roles) if identity is not None else None,
        action=action,
        resource=resource,
        result=result,
        reason=reason,
        client_ip=ctx.client_ip if ctx else None,
        count=count,
    )
    for sink in _Active.sinks:
        try:
            await sink.emit(record)
        except Exception as exc:
            _sink_failed(sink, exc)


def _sink_failed(sink: BaseAuditSink, exc: Exception) -> None:
    """A sink raised: log it and count it (``tenxgraph_audit_sink_errors_total``)."""
    count_audit_sink_error(type(sink).__name__)
    logger.error("Audit sink %s failed: %s: %s", type(sink).__name__, type(exc).__name__, exc)


async def audit_not_found(exc: Any, identity: Identity | None = None) -> None:
    """One ``not_found`` access event per ``ThreadNotFoundError``.

    The thread lookup (``core/threads.py``) emits with the identity and marks the exception;
    the HTTP 404 handler then skips it and covers only the ones raised elsewhere (storage).
    The action is the last permission the request checked.
    """
    if getattr(exc, "audited", False):
        return
    exc.audited = True
    ctx = get_request_context()
    context = getattr(exc, "context", None) or {}
    thread_id = context.get("thread_id")
    await audit_event(
        "access",
        "not_found",
        action=ctx.action if ctx else None,
        resource=f"thread/{thread_id}" if thread_id is not None else None,
        identity=identity,
    )
    if ctx is not None:
        ctx.action = None  # recorded: a later 404 response must not emit it again


async def audit_http_not_found() -> None:
    """One ``not_found`` event for a 404 ``HTTPException`` after an allowed permission check.

    Covers files, memories and tasks, where the service answers 404 for a missing resource and
    for one outside the caller's reach. A request with no permission check (an unknown route)
    and a 404 already recorded by ``audit_not_found`` emit nothing.
    """
    ctx = get_request_context()
    if ctx is None or ctx.action is None:
        return
    action, ctx.action = ctx.action, None
    await audit_event("access", "not_found", action=action, resource=ctx.resource)


__all__ = [
    "AuditConfig",
    "AuditEvent",
    "BaseAuditSink",
    "audit_enabled",
    "audit_http_not_found",
    "audit_event",
    "audit_not_found",
    "configure_audit",
    "set_sinks",
]
