"""Token revocation: reject a credential before it expires.

JWTs are self-contained, so a stolen or logged-out token stays valid until its ``exp``.
Revocation closes that gap: ``RequirePermission`` checks every authenticated request, and
WebSocket runs check again before each run, so a socket opened with a token that is later
revoked stops working too.

Two kinds of revocation are supported:

- **one token**, by its ``jti`` claim (:func:`revoke_token`), kept until the token would have
  expired anyway;
- **every token of a user issued before a moment** (:func:`revoke_user`), compared against
  the token's ``iat`` claim. Use it for "log out everywhere" or after a password change. A
  token without ``iat`` cannot be dated, so it counts as revoked while the user entry exists;
  set ``token_lifetime`` and such a token is dated from its ``exp`` instead, so one minted
  after the revoke is accepted. Tokens should carry ``iat``.

Revocations are kept in the API's cache store (``BaseCacheStore``): keys
``revoked:jti:<jti>`` (expires with the token) and ``revoked:user:<user_id>`` (holds the
cutoff timestamp). A newer ``revoke_user`` cutoff is never replaced by an older one
(``set(mode="if_greater")``). Times come from the store's clock, so pods with skewed clocks
agree. From your own code call :func:`revoke_token` / :func:`revoke_user` with the app's
cache store; from a shell use ``10xgraph revoke``.

Enable it in ``10xgraph.json`` with ``"cache_store": {"type": ..., "token_revocation": true}``
(or an object with ``token_lifetime`` / ``user_ttl``, see ``TokenRevocationConfig``). With a
shared cache store (type ``redis`` or ``custom``) a revocation reaches every worker at once;
with the in-memory store only this process.

A cache store error fails closed: the request is refused with 503 rather than served with a
token that might have been revoked.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING, Any

from tenxgraph_api.src.app.core.config.graph_config import (
    DEFAULT_USER_REVOCATION_TTL,
    TokenRevocationConfig,
)
from tenxgraph_api.src.app.core.redis_client import key_part


if TYPE_CHECKING:
    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore


logger = logging.getLogger("tenxgraph_api.revocation")

_MICROSECOND = Decimal("0.000001")

__all__ = [
    "DEFAULT_USER_REVOCATION_TTL",
    "TokenRevocationConfig",
    "TokenRevocationUnavailableError",
    "is_revoked",
    "revoke_token",
    "revoke_user",
    "token_revoked",
]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _user_key_of(identity: dict[str, Any]) -> str | None:
    """The identity's ``user_id`` as a string, or None when it is missing or not usable."""
    user_id = identity.get("user_id")
    if isinstance(user_id, bool):
        return None
    if isinstance(user_id, int):
        return str(user_id)
    if isinstance(user_id, str) and user_id.strip():
        return user_id
    return None


def _revoked_by_user_cutoff(
    identity: dict[str, Any], cutoff: float | None, token_lifetime: float | None = None
) -> bool:
    if cutoff is None:
        return False
    issued_at = _number(identity.get("iat"))
    if issued_at is None and token_lifetime is not None:
        expires_at = _number(identity.get("exp"))
        if expires_at is not None:
            issued_at = expires_at - token_lifetime
    return issued_at is None or issued_at <= cutoff


def jti_key(jti: Any) -> str:
    """Cache store key of a revoked token."""
    return f"revoked:jti:{key_part(jti)}"


def user_key(user_id: Any) -> str:
    """Cache store key of a user's revocation cutoff."""
    return f"revoked:user:{key_part(user_id)}"


async def revoke_token(cache: BaseCacheStore, jti: str, expires_at: float | None = None) -> None:
    """Revoke one token by its ``jti`` claim.

    ``expires_at`` is its ``exp`` (epoch seconds); the entry is dropped then. Without it the
    entry is kept until deleted.
    """
    ttl = None
    if expires_at is not None:
        ttl = max(1, int(float(expires_at) - await cache.now()))
    await cache.set(jti_key(jti), b"1", ttl)


async def revoke_user(
    cache: BaseCacheStore,
    user_id: str,
    before: float | None = None,
    ttl: float | None = DEFAULT_USER_REVOCATION_TTL,
) -> None:
    """Revoke every token of ``user_id`` issued at or before ``before`` (default: now).

    ``now`` is the cache store's clock. ``ttl`` (seconds) expires the entry once every older
    token has expired; None keeps it until deleted. An older ``before`` than the one already
    stored changes nothing (nor its TTL).
    """
    cutoff = await cache.now() if before is None else float(before)
    # Stored to the microsecond, rounded up: rounding down would let a token issued at the
    # cutoff itself through.
    text = Decimal(cutoff).quantize(_MICROSECOND, rounding=ROUND_CEILING)
    await cache.set(user_key(user_id), f"{text:f}".encode(), ttl, mode="if_greater")


async def _none() -> None:
    return None


async def is_revoked(
    cache: BaseCacheStore, identity: dict[str, Any], token_lifetime: float | None = None
) -> bool:
    """Whether ``identity``'s token was revoked (by ``jti`` or by a user cutoff).

    Raises whatever the cache store raises; :func:`token_revoked` turns that into
    :class:`TokenRevocationUnavailableError`.
    """
    jti, user_id = identity.get("jti"), _user_key_of(identity)
    if jti is None and user_id is None:
        return False
    # Two plain GETs, not one MGET: the keys hash to different slots on Redis Cluster.
    jti_value, raw = await asyncio.gather(
        cache.get(jti_key(jti)) if jti is not None else _none(),
        cache.get(user_key(user_id)) if user_id is not None else _none(),
    )
    if jti_value is not None:
        return True
    if raw is None:
        return False
    cutoff = float(raw.decode() if isinstance(raw, bytes | bytearray) else raw)
    return _revoked_by_user_cutoff(identity, cutoff, token_lifetime)


class TokenRevocationUnavailableError(RuntimeError):
    """The cache store could not answer a revocation check; the request must not be served."""


def _bound() -> tuple[TokenRevocationConfig | None, BaseCacheStore | None]:
    """The revocation settings and cache store bound in the app's container."""
    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore
    from tenxgraph_api.src.app.core.utils.container import current_container

    container = current_container()
    # ``in`` first: try_get builds an unbound class whose arguments all have defaults, which
    # would switch revocation on for every app.
    config = (
        container.try_get(TokenRevocationConfig) if TokenRevocationConfig in container else None
    )
    return config, container.try_get(BaseCacheStore)


async def token_revoked(
    identity: dict[str, Any],
    cache: BaseCacheStore | None = None,
    config: TokenRevocationConfig | None = None,
) -> bool:
    """Whether ``identity``'s credential has been revoked.

    ``cache`` and ``config`` default to the ones bound in the app's container. False when
    token revocation is off or the identity is not a mapping. The ``jti`` is checked even when
    ``user_id`` is missing or odd; per-user revocation applies when ``user_id`` is a valid
    string. Raises :class:`TokenRevocationUnavailableError` when the cache store fails or none
    is bound (fail closed).
    """
    if not isinstance(identity, dict):
        return False
    if cache is None or config is None:
        bound_config, bound_cache = _bound()
        config = config or bound_config
        cache = cache or bound_cache
    if config is None:
        return False
    if cache is None:
        logger.error("Token revocation is on but no cache store is bound")
        raise TokenRevocationUnavailableError("Token revocation check unavailable")
    try:
        return await is_revoked(cache, identity, config.token_lifetime)
    except Exception as exc:
        logger.error("Token revocation check failed: %s", exc)
        raise TokenRevocationUnavailableError("Token revocation check unavailable") from exc
