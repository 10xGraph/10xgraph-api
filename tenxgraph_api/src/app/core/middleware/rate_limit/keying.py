"""Shared client-key derivation for rate limiting.

Used by both the HTTP ``RateLimitMiddleware`` and the WebSocket connection guard so that
WebSocket handshakes are counted against the *same* rate-limit bucket as REST requests.
Works on any ``HTTPConnection`` (both ``Request`` and ``WebSocket`` subclass it).
"""

import ipaddress
import logging
from functools import lru_cache

from starlette.requests import HTTPConnection
from starlette.responses import Response

from tenxgraph_api.src.app.core.config.graph_config import (
    DEFAULT_TRUSTED_PROXIES,
    RateLimitConfig,
)


logger = logging.getLogger("tenxgraph_api")


def _client_ip(connection: HTTPConnection, config: RateLimitConfig) -> str:
    """Resolve the client IP, honouring X-Forwarded-For only where it is trustworthy.

    ``X-Forwarded-For`` is a list that each proxy APPENDS to. Whatever the caller
    sent arrives at the left; only the entries your own proxies appended, on the
    right, are trustworthy.

    Reading ``split(",")[0]`` -- the previous behaviour -- therefore took a value
    the caller fully controls. An attacker could send a different
    ``X-Forwarded-For`` on every request, land in a fresh bucket each time, and
    never be rate limited at all.

    We instead count ``trusted_proxy_hops`` back from the RIGHT. With one proxy in
    front (the default), the last entry is the address that proxy actually observed
    -- the real peer.

    All ``X-Forwarded-For`` header lines are combined in order: a proxy that adds its own
    line instead of appending to the caller's must not let the caller's line decide. The
    header is only honoured for connections that come from ``trusted_proxies`` (by default
    loopback and private networks); anyone reaching the app directly is keyed by their peer.
    """
    peer = connection.client.host if connection.client else "unknown"
    if config.trusted_proxy_headers and _from_trusted_proxy(peer, config):
        lines = connection.headers.getlist("X-Forwarded-For")
        if lines:
            parts = [p.strip() for line in lines for p in line.split(",") if p.strip()]
            hops = max(1, getattr(config, "trusted_proxy_hops", 1))
            if len(parts) >= hops:
                return parts[-hops]
            # Fewer entries than our own infrastructure should have appended: the
            # header is not shaped the way we expect, so trust none of it.
            logger.warning(
                "X-Forwarded-For has %d entries but %d trusted proxy hop(s) are "
                "configured; ignoring the header and using the peer address.",
                len(parts),
                hops,
            )

    return peer


def _from_trusted_proxy(peer: str, config: RateLimitConfig) -> bool:
    # An empty list used to mean "trust every peer", which let any direct client spoof its
    # address. It now means the private-network default.
    trusted = tuple(getattr(config, "trusted_proxies", ()) or DEFAULT_TRUSTED_PROXIES)
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in net for net in _networks(trusted))


@lru_cache(maxsize=32)
def _networks(
    trusted: tuple[str, ...],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parsed networks, cached: the list is fixed per config and checked on every request."""
    return tuple(ipaddress.ip_network(net, strict=False) for net in trusted)


def verified_caller_key(connection: HTTPConnection) -> str | None:
    """Identify the caller, exactly as ``RequirePermission`` does: ``user_id`` and tenant values.

    The rate limiter runs before any route dependency, so it cannot read an authenticated
    user from request state. It runs the bound auth class itself instead, with whatever
    credential the request carries (none for a class that reads a cookie), and returns
    ``run_limits.identity_key`` of the resolved identity: the same ``user_id`` in two tenants
    is two callers. Returns ``None`` when auth is off or the request does not authenticate;
    the caller is then limited by address, so forged tokens cannot buy fresh buckets.
    """
    from injectq import InjectQ

    from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
    from tenxgraph_api.src.app.core.auth.permissions import _extract_credential, remember_claims
    from tenxgraph_api.src.app.core.auth.rbac import resolve
    from tenxgraph_api.src.app.core.auth.reserved_identity import identity_is_reserved
    from tenxgraph_api.src.app.routers.graph.run_limits import identity_key

    credential = _extract_credential(connection)
    try:
        auth = InjectQ.get_instance().try_get(BaseAuth)
        if auth is None:
            return None
        # A throwaway Response: anything the auth class sets on it belongs to the real
        # authentication in RequirePermission, which runs again for the route unless the class
        # allows reusing this result (remember_claims).
        claims = auth.authenticate(connection, Response(), credential)
        if not isinstance(claims, dict) or identity_is_reserved(claims):
            return None  # not a valid key; RequirePermission rejects it
        if credential is not None:
            remember_claims(connection, auth, credential.credentials, claims)
        identity = resolve(claims, auth.rbac)
    except Exception as exc:  # invalid token, bad claims, auth error: fall back to the address
        logger.debug("Rate limit could not verify caller identity: %s", exc)
        return None
    return identity_key(identity) if identity.user_id else None


def client_key_for(connection: HTTPConnection, config: RateLimitConfig) -> str:
    """Derive the rate-limit bucket key for a connection.

    Supports ``by``:

    - ``"global"``: a single bucket for the whole service.
    - ``"user"``: one bucket per authenticated user. This is what you want once auth
      is enabled: limiting purely by IP gives one user roaming across addresses an
      unlimited budget, while a NAT'd office sharing one address gets throttled as
      though it were a single caller.
    - ``"ip"``: one bucket per client address (the default).

    ``"user"`` falls back to the peer address when there is no authenticated user,
    so anonymous traffic is still limited per-caller rather than sharing one bucket
    that any single client could exhaust for everybody.
    """
    if config.by == "global":
        return "__global__"

    if config.by == "user":
        caller = verified_caller_key(connection)
        if caller:
            return f"user:{caller}"
        return f"ip:{_client_ip(connection, config)}"

    return _client_ip(connection, config)
