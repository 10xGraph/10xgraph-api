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
from typing import Any, NoReturn

from fastapi import HTTPException, Response, WebSocket, WebSocketException
from fastapi.security import HTTPAuthorizationCredentials
from injectq.integrations import InjectAPI
from starlette.requests import HTTPConnection, Request
from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core import logger
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
    (FastAPI cannot inject ``HTTPBearer`` on a WebSocket route). Token sources, most secure
    first:

    1. ``Authorization: Bearer <token>`` header -- for non-browser clients.
    2. ``Sec-WebSocket-Protocol: 10xgraph-bearer, <token>`` (legacy: ``agentflow-bearer``) --
       browser-settable and kept out of URLs/logs; the preferred browser mechanism.
    3. ``?token=<token>`` query parameter -- last-resort fallback; the token is exposed in
       URLs/access logs/history, so prefer (2) for browser clients.

    Sources (2) and (3) exist only because browsers cannot set headers on a WebSocket, so
    they are accepted on WebSocket connections only; HTTP routes take the header alone.
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

    ws_token = connection.query_params.get("token")
    if ws_token:
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=ws_token)

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


def _authenticate(
    auth: BaseAuth, connection: HTTPConnection, response: Response | None
) -> dict[str, Any]:
    """The verified claims of this request, or a 401 (1008 on a WebSocket)."""
    credential = _extract_credential(connection)
    try:
        claims = _recall_claims(connection, auth, credential.credentials) if credential else None
        if claims is None:
            claims = auth.authenticate(connection, response, credential)
    except HTTPException as exc:
        _reject(connection, exc.status_code, str(exc.detail))
    except GeneralException as exc:
        # JwtAuth raises UserAccountError (401). HTTP routes render it through its exception
        # handler; a WebSocket has no handler, so close it cleanly.
        if isinstance(connection, WebSocket):
            _reject(connection, exc.status_code, exc.message)
        raise
    if not claims:
        _reject(connection, 401, "Authentication required")
    if not isinstance(claims, dict):
        logger.error("%s.authenticate returned %s, not a dict", type(auth).__name__, type(claims))
        _reject(connection, 401, "Authentication failed")
    return claims


async def _identity(
    connection: HTTPConnection, response: Response | None, auth: BaseAuth
) -> Identity:
    """Authenticate, resolve roles and permissions, refuse reserved ids and revoked tokens."""
    claims = _authenticate(auth, connection, response)
    try:
        identity = resolve(claims, auth.rbac)
    except InvalidClaimsError as exc:
        logger.warning("Rejected token claims: %s", exc)
        _reject(connection, 401, "Invalid token, please login again")
    if identity_is_reserved(identity):
        # Any auth class, any transport, cached or fresh: nobody acts as the shared owner.
        log_reserved_rejection()
        _reject(connection, 401, RESERVED_USER_ID_MESSAGE)
    try:
        revoked = await token_revoked(identity)
    except TokenRevocationUnavailableError:
        _reject(connection, 503, "Token revocation check unavailable")
    if revoked:
        logger.warning("Rejected revoked credential for user %s", identity.user_id)
        _reject(connection, 401, "Token has been revoked, please login again")
    return identity


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
            return None
        identity = await _identity(connection, response, auth)
        if self.permission is not None:
            resource_id = await _resource_id(connection)
            try:
                await _authorize(auth, identity, self.permission, resource_id)
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


async def _authorize(
    auth: BaseAuth, identity: Identity, permission: str, resource: str | None
) -> None:
    """``auth.authorize`` or an ``HTTPException`` (403; one ``authorize`` raised passes through)."""
    if not await auth.authorize(identity, permission, resource):
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
        await _authorize(auth, identity, permission, resource)


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
    resource_id = next(
        (str(params[n]) for n in ("thread_id", "memory_id", "namespace", "file_id") if n in params),
        None,
    )
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
