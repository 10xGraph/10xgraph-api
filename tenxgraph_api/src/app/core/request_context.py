"""Per-request context (request id, transport, client address, user) carried in a contextvar.

Set by ``RequestIDMiddleware`` for HTTP requests and WebSocket connections. Log lines and audit
events read it, so call sites pass only what they alone know.
"""

from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel


class RequestContext(BaseModel):
    request_id: str
    transport: Literal["http", "ws"]
    client_ip: str | None = None
    user_id: str | None = None  # set later by auth once known
    # Last permission checked and its resource, for a later audit ``not_found`` event.
    action: str | None = None
    resource: str | None = None


request_context_var: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def get_request_context() -> RequestContext | None:
    return request_context_var.get()


def set_request_user(user_id: str | None) -> None:
    """Record the authenticated user on the current context, if there is one."""
    ctx = request_context_var.get()
    if ctx is not None:
        ctx.user_id = user_id
