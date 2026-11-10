"""The one auth dependency for every route, on HTTP and WebSocket alike.

``RequirePermission(resource, action)`` and ``RequireIdentity()`` build the caller's
:class:`Identity` once per request: credential, ``BaseAuth.authenticate``, ``resolve`` (roles,
permissions, reach, token scope narrowing), reserved-id and revocation checks. Then
``BaseAuth.authorize`` decides. With no ``BaseAuth`` bound (auth off) they return ``None`` and
check nothing.

A result verified earlier in the same request (the rate limiter, ``rate_limit.by == "user"``)
is reused when the auth class sets ``reusable_within_request`` and defines its own
``authenticate``: a class whose ``authenticate`` has side effects on the response (cookies,
refreshed tokens) must keep the default ``False``.
"""

import copy
import email.message
from typing import Any, Literal, NoReturn

from fastapi import HTTPException, Response, WebSocket, WebSocketException
from fastapi.security import HTTPAuthorizationCredentials
from injectq.integrations import InjectAPI
from starlette.requests import HTTPConnection, Request
from tenxgraph.core.authz import Identity, UserCredential

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.audit import audit_enabled, audit_event
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.permissions_registry import ALL_PERMISSIONS
from tenxgraph_api.src.app.core.auth.rbac import InvalidClaimsError, resolve
from tenxgraph_api.src.app.core.auth.reserved_identity import (
    RESERVED_USER_ID_MESSAGE,
    identity_is_reserved,
    log_reserved_rejection,
)
from tenxgraph_api.src.app.core.auth.revocation import (
    TokenRevocationUnavailableError,
    token_revoked,
)
from tenxgraph_api.src.app.core.exceptions.general_exception import GeneralException
from tenxgraph_api.src.app.core.request_context import get_request_context, set_request_user
from tenxgraph_api.src.app.core.utils.container import current_container
from tenxgraph_api.src.app.core.validation.request_config import (
    ClientInputError,
    normalize_thread_id,
    resource_id_error,
)


# Sec-WebSocket-Protocol sentinel carrying the bearer token for browser WebSocket clients.
# The client offers two subprotocols: this sentinel followed by the raw JWT, e.g.
#   new WebSocket(url, ["10xgraph-bearer", "<jwt>"])
# The token rides in a request header, so -- unlike ``?token=`` -- it never lands in URLs,
# access logs, or browser history. The server must echo the sentinel on accept() (see
# ``ws_bearer_subprotocol``) or browsers fail the handshake.
WS_BEARER_SUBPROTOCOL = "10xgraph-bearer"
# Pre-rename sentinel, still sent by released clients; accepted until 2.0.
LEGACY_WS_BEARER_SUBPROTOCOL = "agentflow-bearer"
_BEARER_SUBPROTOCOLS = (WS_BEARER_SUBPROTOCOL, LEGACY_WS_BEARER_SUBPROTOCOL)

# A bearer-carrying Sec-WebSocket-Protocol offer is exactly [sentinel, token].
_BEARER_SUBPROTOCOL_PARTS = 2


def _subprotocols(connection: HTTPConnection) -> list[str]:
    header = connection.headers.get("sec-websocket-protocol")
    if not header:
        return []
    return [p.strip() for p in header.split(",") if p.strip()]


def ws_bearer_subprotocol(connection: HTTPConnection) -> str | None:
    """Return the subprotocol to echo on ``accept()`` when the client used it for the token.

    Browsers require the server to confirm one of the offered subprotocols; when the bearer
    sentinel was offered, accept with it. Returns ``None`` otherwise (plain ``accept()``).
    """
    parts = _subprotocols(connection)
    if parts and parts[0] in _BEARER_SUBPROTOCOLS:
        return parts[0]
    return None


def _extract_credential(
    connection: HTTPConnection,
) -> HTTPAuthorizationCredentials | None:
    """Extract bearer credentials from a request or WebSocket connection.

    Mirrors ``HTTPBearer(auto_error=False)`` but works for both HTTP and WebSocket routes
    (FastAPI cannot inject ``HTTPBearer`` on a WebSocket route). Token sources:

    1. ``Authorization: Bearer <token>`` header -- for non-browser clients.
    2. ``Sec-WebSocket-Protocol: 10xgraph-bearer, <token>`` (legacy: ``agentflow-bearer``) --
       WebSocket only, because browsers cannot set headers on a WebSocket.

    A ``?token=`` query parameter is never read: a token in the URL ends up in access logs,
    proxy logs and HTTP trace spans.
    """
    authorization = connection.headers.get("Authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return HTTPAuthorizationCredentials(scheme=scheme, credentials=token)

    if not _is_websocket(connection):
        return None

    # [sentinel, token] -- the two subprotocols a browser client offers to carry the bearer.
    parts = _subprotocols(connection)
    if len(parts) >= _BEARER_SUBPROTOCOL_PARTS and parts[0] in _BEARER_SUBPROTOCOLS and parts[1]:
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=parts[1])

    return None


def _is_websocket(connection: HTTPConnection) -> bool:
    scope = getattr(connection, "scope", None)
    if isinstance(scope, dict) and scope.get("type") == "websocket":
        return True
    return isinstance(connection, WebSocket)


# RFC 6455 close code 1008 "Policy Violation" -- the right signal for an auth/authz
# rejection at the WebSocket handshake.
WS_POLICY_VIOLATION = 1008


def _reject(connection: HTTPConnection, status_code: int, detail: str) -> NoReturn:
    """Reject a connection with the error type appropriate to its protocol.

    FastAPI translates an ``HTTPException`` into a response only on HTTP routes; on a
    WebSocket route it propagates unhandled and tears the socket down abruptly (close
    1006) with a server-side error log. Raise ``WebSocketException`` (close 1008) there
    instead so the client sees a clean policy-violation rejection.
    """
    if isinstance(connection, WebSocket):
        raise WebSocketException(code=WS_POLICY_VIOLATION, reason=detail)
    raise HTTPException(status_code=status_code, detail=detail)


_STATE_KEY = "tenxgraph_verified_claims"
# The run credential from ``BaseAuth.downstream_credential``; read with ``run_credential``.
_CREDENTIAL_STATE_KEY = "tenxgraph_run_credential"


def _reusable(auth: BaseAuth) -> bool:
    """Whether ``auth`` declared its own ``authenticate`` reusable.

    The flag counts only when set by the class that defines the ``authenticate`` in use. A
    subclass of ``JwtAuth`` that overrides ``authenticate`` (cookies, a DB lookup) inherits
    ``JwtAuth``'s flag but not its purity, so it is not reused unless it sets the flag itself.
    """
    for cls in type(auth).__mro__:
        if "authenticate" in vars(cls):
            return vars(cls).get("reusable_within_request") is True
    return False


def remember_claims(
    connection: HTTPConnection, auth: BaseAuth, token: str, claims: dict[str, Any]
) -> None:
    """Keep verified claims for the rest of this request (the rate limiter calls this)."""
    scope = getattr(connection, "scope", None)
    if not isinstance(scope, dict) or not _reusable(auth) or identity_is_reserved(claims):
        return
    scope.setdefault("state", {})[_STATE_KEY] = (id(auth), token, copy.deepcopy(claims))


def _recall_claims(connection: HTTPConnection, auth: BaseAuth, token: str) -> dict[str, Any] | None:
    scope = getattr(connection, "scope", None)
    cached = scope.get("state", {}).get(_STATE_KEY) if isinstance(scope, dict) else None
    if not cached or not _reusable(auth):
        return None
    auth_id, cached_token, claims = cached
    if auth_id != id(auth) or cached_token != token or identity_is_reserved(claims):
        return None
    return copy.deepcopy(claims)


async def _auth_failed(reason: str, user_id: str | None = None) -> None:
    """The ``auth`` audit event for a refused credential (no token or claim content)."""
    await audit_event("auth", "failed", reason=reason, user_id=user_id)


def _failure_reason(
    credential: HTTPAuthorizationCredentials | None, exc: Exception | None = None
) -> str:
    """Short audit code for a failed ``authenticate``: missing, expired or invalid token."""
    if getattr(exc, "status_code", 401) >= 500:  # noqa: PLR2004
        return "auth_error"  # the auth backend broke (JWT settings missing), not the caller
    if credential is None:
        return "missing_token"
    if getattr(exc, "error_code", None) == "EXPIRED_TOKEN":
        return "expired"
    return "invalid_token"


async def _authenticate(
    auth: BaseAuth, connection: HTTPConnection, response: Response | None
) -> tuple[dict[str, Any], HTTPAuthorizationCredentials | None]:
    """The verified claims of this request and its bearer credential, or a 401 (1008 on a WS).

    Claims the rate limiter already verified are reused (``_recall_claims``): the limiter
    emits nothing, so each request gets at most one ``auth`` event, from here or ``_identity``.
    """
    credential = _extract_credential(connection)
    try:
        claims = _recall_claims(connection, auth, credential.credentials) if credential else None
        if claims is None:
            claims = auth.authenticate(connection, response, credential)
    except HTTPException as exc:
        await _auth_failed(_failure_reason(credential, exc))
        _reject(connection, exc.status_code, str(exc.detail))
    except GeneralException as exc:
        await _auth_failed(_failure_reason(credential, exc))
        # JwtAuth raises UserAccountError (401). HTTP routes render it through its exception
        # handler; a WebSocket has no handler, so close it cleanly.
        if isinstance(connection, WebSocket):
            _reject(connection, exc.status_code, exc.message)
        raise
    if not claims:
        await _auth_failed(_failure_reason(credential))
        _reject(connection, 401, "Authentication required")
    if not isinstance(claims, dict):
        logger.error("%s.authenticate returned %s, not a dict", type(auth).__name__, type(claims))
        await _auth_failed("claims_rejected")
        _reject(connection, 401, "Authentication failed")
    return claims, credential


async def _identity(
    connection: HTTPConnection, response: Response | None, auth: BaseAuth
) -> Identity:
    """Authenticate, resolve roles and permissions, refuse reserved ids and revoked tokens.

    Each refusal emits one ``auth`` audit event; the WARNING lines are written only while audit
    is off, so the record is not doubled.
    """
    claims, credential = await _authenticate(auth, connection, response)
    try:
        identity = resolve(claims, auth.rbac)
    except InvalidClaimsError as exc:
        if not audit_enabled():
            logger.warning("Rejected token claims: %s", exc)
        user_id = claims.get("user_id")
        await _auth_failed("claims_rejected", user_id if isinstance(user_id, str) else None)
        _reject(connection, 401, "Invalid token, please login again")
    if identity_is_reserved(identity):
        # Any auth class, any transport, cached or fresh: nobody acts as the shared owner.
        # The event leaves the reserved id out, like the log line.
        if not audit_enabled():
            log_reserved_rejection()
        await _auth_failed("reserved_id")
        _reject(connection, 401, RESERVED_USER_ID_MESSAGE)
    try:
        revoked = await token_revoked(identity)
    except TokenRevocationUnavailableError:
        await _auth_failed("revocation_unavailable", identity.user_id)
        _reject(connection, 503, "Token revocation check unavailable")
    if revoked:
        if not audit_enabled():
            logger.warning("Rejected revoked credential for user %s", identity.user_id)
        await _auth_failed("revoked", identity.user_id)
        _reject(connection, 401, "Token has been revoked, please login again")
    _keep_run_credential(
        connection, await auth.downstream_credential(connection, credential, claims)
    )
    set_request_user(identity.user_id)
    return identity


def _keep_run_credential(connection: HTTPConnection, credential: Any) -> None:
    """Keep the run credential on the connection, apart from the identity and claims."""
    if credential is not None and not isinstance(credential, UserCredential):
        # A plain string would be logged and published like any other config value.
        raise TypeError(
            f"downstream_credential must return a UserCredential or None, "
            f"got {type(credential).__name__}"
        )
    scope = getattr(connection, "scope", None)
    if isinstance(scope, dict):
        scope.setdefault("state", {})[_CREDENTIAL_STATE_KEY] = credential


def run_credential(connection: HTTPConnection | None) -> UserCredential | None:
    """The caller's credential for the run config (``config["credential"]``), or None.

    Set by ``RequireIdentity`` / ``RequirePermission``; None with auth off or before them.
    """
    scope = getattr(connection, "scope", None)
    credential = (
        scope.get("state", {}).get(_CREDENTIAL_STATE_KEY) if isinstance(scope, dict) else None
    )
    return credential if isinstance(credential, UserCredential) else None


class RequireIdentity:
    """Authenticate the caller and return its ``Identity``; no permission check.

    ``None`` when auth is off. Raises 401 on HTTP routes and closes with 1008 on WebSocket
    routes (FastAPI does not translate an ``HTTPException`` on a WebSocket handshake).
    """

    permission: str | None = None

    async def __call__(
        self,
        connection: HTTPConnection,
        response: Response,
        auth: BaseAuth | None = InjectAPI(BaseAuth),
    ) -> Identity | None:
        if auth is None:
            if self.permission is not None:
                # Auth off: everyone is allowed; the event records the call (user_id null).
                await _audit_access(None, self.permission, _path_resource(connection), "allowed")
            return None
        identity = await _identity(connection, response, auth)
        if self.permission is not None:
            resource_id = await _resource_id(connection)
            resource = _resource_label(connection, resource_id)
            try:
                await _authorize(auth, identity, self.permission, resource_id, resource)
            except HTTPException as exc:
                _reject(connection, exc.status_code, str(exc.detail))
        return identity


class RequirePermission(RequireIdentity):
    """``RequireIdentity`` plus ``BaseAuth.authorize(identity, "resource:action", id)`` (403).

    Usage: ``identity = Depends(RequirePermission("graph", "invoke"))``. A permission the
    registry does not know raises ``ValueError`` here, at import, not on the first request.
    """

    def __init__(self, resource: str, action: str):
        permission = f"{resource}:{action}"
        if permission not in ALL_PERMISSIONS:
            raise ValueError(f"Unknown permission {permission!r}; see permissions_registry.py")
        self.resource = resource
        self.action = action
        self.permission = permission


async def _audit_access(
    identity: Identity | None,
    permission: str,
    resource: str | None,
    result: Literal["allowed", "denied"],
) -> None:
    """The ``access`` audit event; also remembers the permission for a later ``not_found``."""
    ctx = get_request_context()
    if ctx is not None:
        ctx.action = permission
        ctx.resource = resource
    await audit_event("access", result, action=permission, resource=resource, identity=identity)


async def _authorize(
    auth: BaseAuth,
    identity: Identity,
    permission: str,
    resource_id: str | None,
    resource: str | None,
) -> None:
    """``auth.authorize`` or an ``HTTPException`` (403; one ``authorize`` raised passes through).

    ``resource_id`` goes to ``authorize``; ``resource`` (``"thread/<id>"``) to the audit event.
    """
    try:
        allowed = await auth.authorize(identity, permission, resource_id)
    except HTTPException:
        await _audit_access(identity, permission, resource, "denied")
        raise
    await _audit_access(identity, permission, resource, "allowed" if allowed else "denied")
    if not allowed:
        if not audit_enabled():
            logger.warning("User %s lacks permission %s", identity.user_id, permission)
        raise HTTPException(status_code=403, detail=f"Missing required permission: {permission}")


async def authorize_resource(
    identity: Identity | None, permission: str, resource: str | None
) -> None:
    """``BaseAuth.authorize`` on a thread the route reads from its own input; 403 when refused.

    ``RequirePermission`` passes the id from the path or a JSON ``thread_id``. AG-UI
    (``threadId``), every ``/v1/graph/ws`` run frame and the ``/v1/graph/live`` init frame name
    their thread where it cannot look, so those routes call this once they know it, and
    ``authorize`` sees the same ``(permission, resource)`` on every transport. Raises
    ``HTTPException``; a socket route turns it into an error frame. Nothing to check with auth
    off (``identity`` is None).
    """
    if identity is None:
        return
    auth = current_container().try_get(BaseAuth)
    if auth is not None:
        label = f"thread/{resource}" if resource is not None else None
        await _authorize(auth, identity, permission, resource, label)


# Path parameters that name the target, in priority order, with the audit resource kind.
_RESOURCE_KINDS = {
    "thread_id": "thread",
    "memory_id": "memory",
    "namespace": "namespace",
    "file_id": "file",
}


def _resource_label(connection: HTTPConnection, resource_id: str | None) -> str | None:
    """``"<kind>/<id>"`` for the audit event; a body id is always a thread."""
    if resource_id is None:
        return None
    params = connection.path_params
    kind = next((k for name, k in _RESOURCE_KINDS.items() if name in params), "thread")
    return f"{kind}/{resource_id}"


def _path_resource(connection: HTTPConnection) -> str | None:
    """The audit resource from the path alone (auth off: the body is not read)."""
    params = connection.path_params
    resource_id = next((str(params[n]) for n in _RESOURCE_KINDS if n in params), None)
    return _resource_label(connection, resource_id)


async def _resource_id(connection: HTTPConnection) -> str | None:
    """The thread (or memory, namespace, file) id the request targets, validated (422).

    Path parameters win; otherwise the thread id from the JSON body. Ids that break the shared
    resource-id rule are refused here, once, for every route.
    """
    params = connection.path_params
    problem = None
    for name in ("thread_id", "message_id", "memory_id"):
        if name in params:
            problem = resource_id_error(params[name], name) or problem
    resource_id = next((str(params[n]) for n in _RESOURCE_KINDS if n in params), None)
    if resource_id is None:
        resource_id = await _body_thread_id(connection)
        problem = problem or resource_id_error(resource_id, "thread_id")
    if problem is not None:
        if isinstance(connection, WebSocket):
            _reject(connection, 422, problem)
        raise ClientInputError(problem)
    return resource_id


def _is_json_content_type(content_type: str | None) -> bool:
    """Whether FastAPI parses a body with this content type as JSON (so we read it too)."""
    if not content_type:
        return True
    message = email.message.Message()
    message["content-type"] = content_type
    if message.get_content_maintype() != "application":
        return False
    subtype = message.get_content_subtype()
    return subtype == "json" or subtype.endswith("+json")


async def _body_thread_id(connection: HTTPConnection) -> str | None:
    """The thread id a JSON body targets (root ``thread_id`` or ``config.thread_id``).

    A body naming two different threads is rejected (400) so the id checked here is the one the
    service runs. No second parse: FastAPI reads the body first and Starlette caches it on this
    Request (tests/integration_tests/test_body_parsed_once.py guards it).
    """
    if not isinstance(connection, Request):
        return None
    if not _is_json_content_type(connection.headers.get("content-type")):
        return None
    try:
        body = await connection.json()
    except Exception as exc:  # FastAPI rejects a malformed body itself before the route runs.
        logger.debug("Failed to parse request body for thread_id: %s", exc)
        return None
    if not isinstance(body, dict):
        return None
    root_id = normalize_thread_id(body.get("thread_id"))
    cfg = body.get("config")
    config_id = normalize_thread_id(cfg.get("thread_id")) if isinstance(cfg, dict) else None
    if root_id and config_id and root_id != config_id:
        _reject(connection, 400, "thread_id and config.thread_id must match")
    return root_id or config_id
