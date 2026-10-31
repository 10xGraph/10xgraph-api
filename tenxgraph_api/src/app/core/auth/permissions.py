"""
Unified authentication and authorization dependency for FastAPI endpoints.

This module provides a reusable dependency that combines authentication and
authorization checks, reducing code duplication across routers.
"""

import email.message
from collections.abc import Callable
from typing import Any, NoReturn

from fastapi import HTTPException, Response, WebSocket, WebSocketException
from fastapi.security import HTTPAuthorizationCredentials
from injectq.integrations import InjectAPI
from starlette.requests import HTTPConnection

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.auth_backend import BaseAuth
from tenxgraph_api.src.app.core.auth.authorization import (
    AuthorizationBackend,
    ScopeClaimError,
    scopes_from_claims,
)
from tenxgraph_api.src.app.core.auth.identity_cache import recall_identity
from tenxgraph_api.src.app.core.auth.request_config import (
    ClientInputError,
    normalize_thread_id,
    resource_id_error,
)
from tenxgraph_api.src.app.core.auth.reserved_identity import (
    RESERVED_USER_ID_MESSAGE,
    identity_is_reserved,
    log_reserved_rejection,
)
from tenxgraph_api.src.app.core.auth.revocation import (
    TokenRevocationUnavailableError,
    token_revoked,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.exceptions.general_exception import GeneralException
from tenxgraph_api.src.app.core.utils.log_sanitizer import sanitize_for_logging


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
    from fastapi import WebSocket

    return isinstance(connection, WebSocket)


# RFC 6455 close code 1008 "Policy Violation" -- the right signal for an auth/authz
# rejection at the WebSocket handshake.
WS_POLICY_VIOLATION = 1008


def _is_json_content_type(content_type: str | None) -> bool:
    """Whether FastAPI parses a body with this content type as JSON.

    Mirrors ``fastapi.routing``: a missing content type, ``application/json`` and any
    ``application/*+json`` are all parsed as JSON. Reading fewer types here would let a
    request skip the thread check while the route still receives its body.
    """
    if not content_type:
        return True
    message = email.message.Message()
    message["content-type"] = content_type
    if message.get_content_maintype() != "application":
        return False
    subtype = message.get_content_subtype()
    return subtype == "json" or subtype.endswith("+json")


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


def _authenticate(
    auth_backend: BaseAuth,
    connection: HTTPConnection,
    response: Response,
    credential: HTTPAuthorizationCredentials | None,
) -> dict[str, Any] | None:
    """Verify the credential, reusing a result this request already verified when allowed.

    The rate limiter (``rate_limit.by == "user"``) verifies the same token first; for a backend
    with ``reusable_within_request`` its result is reused instead of decoding the token twice.
    """
    if credential is not None:
        cached = recall_identity(connection, auth_backend, credential.credentials)
        if cached is not None:
            return cached
    return auth_backend.authenticate(connection, response, credential)  # type: ignore[arg-type]


async def _reject_if_revoked(connection: HTTPConnection, user: dict[str, Any]) -> None:
    """Refuse a credential that was revoked (401), when ``token_revocation`` is on.

    When the cache store cannot answer, refuse with 503 instead of serving a token that might
    have been revoked.
    """
    try:
        revoked = await token_revoked(user)
    except TokenRevocationUnavailableError:
        _reject(connection, 503, "Token revocation check unavailable")
    if revoked:
        logger.warning("Rejected revoked credential for user %s", user.get("user_id"))
        _reject(connection, 401, "Token has been revoked, please login again")


def _granted_scopes(
    connection: HTTPConnection, authz: AuthorizationBackend, user: dict[str, Any]
) -> list[str] | None:
    """Scopes the backend grants this identity, or None for unrestricted.

    A scope claim of the wrong type is a malformed token: 401.
    """
    scopes_fn = getattr(authz, "scopes_for", None)
    try:
        granted = scopes_fn(user) if callable(scopes_fn) else None
        if granted is None:
            granted = scopes_from_claims(user)  # backends without scopes_for
    except ScopeClaimError as exc:
        logger.warning("Rejected scope claim for user %s: %s", user.get("user_id"), exc)
        _reject(connection, 401, "Invalid token, malformed scope claim")
    return granted


class RequirePermission:
    """
    FastAPI dependency that combines authentication and authorization.

    This class-based dependency verifies user authentication and checks
    authorization in a single step, reducing boilerplate code in endpoints.

    Usage:
        @router.post("/v1/resource")
        async def endpoint(
            user: dict = Depends(RequirePermission("resource", "action"))
        ):
            # User is authenticated and authorized
            pass

    Args:
        resource: Resource type being accessed (e.g., "graph", "checkpointer", "store")
        action: Action being performed (e.g., "invoke", "read", "write", "delete")
        extract_resource_id: Optional function to extract resource_id from request
    """

    def __init__(
        self,
        resource: str,
        action: str,
        extract_resource_id: Callable[[HTTPConnection], str | None] | None = None,
        *,
        introspection: bool = False,
        explicit_scope: bool = False,
    ):
        """
        Initialize the permission requirement.

        Args:
            resource: Resource type (graph, checkpointer, store)
            action: Action type (invoke, stream, read, write, delete, etc.)
            extract_resource_id: Optional callable that extracts resource_id from request
            introspection: The route exposes the graph's internals (structure, tool schemas,
                state schema). Under ``graph_introspection: "scoped"`` the caller must hold
                the scope explicitly; an identity that declares no scopes is refused.
            explicit_scope: Admin-only route. The caller must hold the scope explicitly, always:
                an identity that declares no scopes (normally unrestricted) is refused.
        """
        self.resource = resource
        self.action = action
        self.extract_resource_id_fn = extract_resource_id
        self.introspection = introspection
        self.explicit_scope = explicit_scope

    async def __call__(  # noqa: PLR0912
        self,
        connection: HTTPConnection,
        response: Response,
        config: GraphConfig = InjectAPI(GraphConfig),
        auth_backend: BaseAuth = InjectAPI(BaseAuth),
        authz: AuthorizationBackend = InjectAPI(AuthorizationBackend),
    ) -> dict[str, Any]:
        """
        Verify authentication and authorization.

        ``connection`` is typed as ``HTTPConnection`` (the common base of ``Request``
        and ``WebSocket``) so this dependency resolves on both HTTP and WebSocket
        routes; FastAPI cannot inject a ``Request`` on a WebSocket route.

        Returns:
            dict: User information if authenticated and authorized

        Raises:
            HTTPException: 401/403 on HTTP routes when auth or authz fails.
            WebSocketException: close 1008 on WebSocket routes for the same failures
                (FastAPI does not translate an HTTPException on a WS handshake).
        """
        # Extract bearer credentials from the Authorization header, with a
        # ``?token=`` query fallback for browser WebSocket clients (see
        # _extract_credential). Works for both HTTP and WebSocket connections.
        credential = _extract_credential(connection)

        # Step 1: Check if auth is configured
        backend = config.auth_config()

        # If auth is not configured, skip authentication and authorization entirely
        if not backend:
            logger.debug(
                f"Auth not configured, skipping auth/authz for {self.resource}:{self.action}"
            )
            return {}

        # Step 2: Authentication (reusing verify_current_user logic)
        user = {}
        if not auth_backend:
            logger.error("Auth backend is not configured")
            user = {}
        else:
            try:
                user_result = _authenticate(auth_backend, connection, response, credential)
            except HTTPException as exc:
                # JWT/custom backends signal auth failure with HTTPException; convert it
                # to a clean WebSocket close on WS routes (see _reject).
                _reject(connection, exc.status_code, str(exc.detail))
            except GeneralException as exc:
                # JwtAuth raises UserAccountError (401). HTTP routes render it through its
                # exception handler; a WebSocket has no handler, so close it cleanly.
                if isinstance(connection, WebSocket):
                    _reject(connection, exc.status_code, exc.message)
                raise
            if user_result and "user_id" not in user_result:
                logger.error("Authentication failed: 'user_id' not found in user info")
            user = user_result or {}
            if identity_is_reserved(user):
                # Any backend, any transport, cached or fresh: nobody acts as the shared owner.
                log_reserved_rejection()
                _reject(connection, 401, RESERVED_USER_ID_MESSAGE)

        # Step 2b: a verified credential may still have been revoked before it expires.
        await _reject_if_revoked(connection, user)

        # Step 3: Extract resource_id if available
        resource_id = None
        if self.extract_resource_id_fn:
            resource_id = self.extract_resource_id_fn(connection)
        else:
            resource_id = self._extract_resource_id_from_path(connection)
            if resource_id is None:
                resource_id = await self._extract_resource_id_from_body(connection)
            self._check_resource_ids(connection, resource_id)

        # Step 4: Scope check -- does this identity carry the permission for this
        # endpoint at all? The required scope is "<resource>:<action>". Scopes come from
        # the authenticated identity (the ``scope`` / ``scopes`` claims, merged into exact
        # names). If the identity declares no scopes, we stay permissive (backward
        # compatible) -- once scopes are issued, they are enforced.
        required_scope = f"{self.resource}:{self.action}"
        granted_scopes = _granted_scopes(connection, authz, user)
        if granted_scopes is not None and required_scope not in set(granted_scopes):
            logger.warning(f"Missing scope '{required_scope}' for user {user.get('user_id')}")
            _reject(connection, 403, f"Missing required scope: {required_scope}")
        if granted_scopes is None and (
            self.explicit_scope
            or (self.introspection and getattr(config, "introspection_scoped", False) is True)
        ):
            # No scopes declared would normally mean "permissive"; graph internals are not.
            _reject(connection, 403, f"Missing required scope: {required_scope}")

        # Step 5: Object-level authorization (ownership etc.)
        if not await authz.authorize(
            user,
            self.resource,
            self.action,
            resource_id=resource_id,
        ):
            logger.warning(
                f"Authorization failed for user {user.get('user_id')} "
                f"on {self.resource}:{self.action}"
            )
            _reject(
                connection,
                403,
                f"Not authorized to {self.action} {self.resource}",
            )

        # Step 6: Stamp the trusted authz block into the user object. Every service copies
        # this user into config["user"], so the isolation policy + scopes reach every
        # downstream call (checkpointer, store, graph execution). Overwrites anything the
        # client sent -- not hijackable.
        if isinstance(user, dict) and user.get("user_id"):
            from tenxgraph.core.authz import build_authz

            scope_fn = getattr(authz, "isolation_scope", None)
            iso_scope = scope_fn() if callable(scope_fn) else "none"
            user["authz"] = build_authz(
                user["user_id"],
                scope=iso_scope,
                scopes=granted_scopes if granted_scopes is not None else [],
            )

        # Log successful auth/authz (with sanitized user info)
        logger.debug(
            f"Auth/Authz success for {self.resource}:{self.action}, "
            f"user: {sanitize_for_logging(user)}"
        )

        return user

    @staticmethod
    def _check_resource_ids(connection: HTTPConnection, resource_id: str | None) -> None:
        """Refuse an id that breaks the shared resource-id rule (422 ``VALIDATION_ERROR``).

        Covers the thread id from the path or body and the message and memory ids in the path.
        """
        problem = None
        params = connection.path_params
        for name in ("thread_id", "message_id", "memory_id"):
            if name in params:
                problem = resource_id_error(params[name], name) or problem
        if problem is None:
            problem = resource_id_error(resource_id, "thread_id")
        if problem is None:
            return
        if isinstance(connection, WebSocket):
            _reject(connection, 422, problem)
        raise ClientInputError(problem)

    def _extract_resource_id_from_path(self, connection: HTTPConnection) -> str | None:
        """
        Extract resource ID from request path parameters.

        Looks for common patterns like thread_id, memory_id in path params.

        Args:
            connection: FastAPI request or WebSocket connection

        Returns:
            Resource ID as string, or None if not found
        """
        # Check path parameters
        path_params = connection.path_params

        # Common resource ID patterns
        for param_name in ["thread_id", "memory_id", "namespace"]:
            if param_name in path_params:
                return str(path_params[param_name])

        return None

    async def _extract_resource_id_from_body(self, connection: HTTPConnection) -> str | None:
        """Extract the thread ID a request body targets.

        The body is read exactly when FastAPI would parse it as JSON, and the ID is normalised
        the same way the services normalise it, so the thread checked here is the thread that
        runs. Routes read either the root ``thread_id`` (stop, fix) or ``config.thread_id``
        (invoke, stream); a body that names two different threads is rejected rather than
        letting the check and the service pick different ones.
        """
        from starlette.requests import Request

        if not isinstance(connection, Request):
            return None
        if not _is_json_content_type(connection.headers.get("content-type")):
            return None

        # No second parse: FastAPI reads a JSON body with ``request.json()`` before it resolves
        # dependencies, and Starlette caches the result on this same Request object, so this
        # returns the cached value. tests/integration_tests/test_body_parsed_once.py guards it.
        try:
            body = await connection.json()
        except Exception as exc:
            # FastAPI rejects a malformed body itself before the route runs.
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
