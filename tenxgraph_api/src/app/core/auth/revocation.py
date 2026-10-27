"""Token revocation: reject a credential before it expires.

JWTs are self-contained, so a stolen or logged-out token stays valid until its ``exp``. A
:class:`TokenRevocationBackend` closes that gap: ``RequirePermission`` asks it about every
authenticated request, and WebSocket runs ask again before each run, so a socket opened with
a token that is later revoked stops working too.

Two kinds of revocation are supported by the built-in backends:

- **one token**, by its ``jti`` claim (``revoke_token``), kept until the token would have
  expired anyway;
- **every token of a user issued before a moment** (``revoke_user``), compared against the
  token's ``iat`` claim. Use it for "log out everywhere" or after a password change. A token
  without ``iat`` cannot be dated, so it counts as revoked once its user has been revoked.

Enable it in ``10xgraph.json`` with ``"token_revocation": "redis"`` (shared by every worker;
uses the ``redis`` key or ``REDIS_URL``), ``"token_revocation": "memory"`` (one process; tests
and single-worker setups), or ``"token_revocation": "module:attr"`` for your own backend. A
``TokenRevocationBackend`` bound in your InjectQ container works too.

A backend error fails closed: the request is refused with 503 rather than served with a token
that might have been revoked.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import time
from abc import ABC, abstractmethod
from typing import Any


logger = logging.getLogger("tenxgraph_api.revocation")


class TokenRevocationBackend(ABC):
    """Decides whether an authenticated identity's credential has been revoked.

    ``identity`` is what the auth backend returned: for ``"auth": "jwt"`` the decoded claims
    (``user_id``, ``jti``, ``iat``, ``exp``, ...). Custom auth backends should include the
    claims their revocation backend needs.
    """

    @abstractmethod
    async def is_revoked(self, identity: dict[str, Any]) -> bool:
        """Return True when this credential must no longer be accepted."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release resources (called on app shutdown)."""


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


def _revoked_by_user_cutoff(identity: dict[str, Any], cutoff: float | None) -> bool:
    if cutoff is None:
        return False
    issued_at = _number(identity.get("iat"))
    return issued_at is None or issued_at <= cutoff


class InMemoryTokenRevocation(TokenRevocationBackend):
    """Revocation list held in this process. Fine for tests and single-worker servers."""

    def __init__(self) -> None:
        self._tokens: dict[str, float] = {}  # jti -> expiry (epoch seconds)
        self._users: dict[str, float] = {}  # user_id -> revoke tokens issued at or before

    def revoke_token(self, jti: str, expires_at: float | None = None) -> None:
        """Revoke one token. ``expires_at`` (its ``exp``) lets the entry be dropped later."""
        self._tokens[str(jti)] = float(expires_at) if expires_at is not None else float("inf")

    def revoke_user(self, user_id: str, before: float | None = None) -> None:
        """Revoke every token of ``user_id`` issued at or before ``before`` (default: now)."""
        self._users[str(user_id)] = time.time() if before is None else float(before)

    async def is_revoked(self, identity: dict[str, Any]) -> bool:
        now = time.time()
        self._tokens = {jti: exp for jti, exp in self._tokens.items() if exp > now}
        jti = identity.get("jti")
        if jti is not None and str(jti) in self._tokens:
            return True
        user_id = _user_key_of(identity)
        cutoff = self._users.get(user_id) if user_id is not None else None
        return _revoked_by_user_cutoff(identity, cutoff)


class RedisTokenRevocation(TokenRevocationBackend):
    """Revocation list in Redis, shared by every worker and instance.

    Keys: ``<prefix>:jti:<jti>`` (expires with the token) and ``<prefix>:user:<user_id>``
    (holds the cutoff timestamp). One ``MGET`` per check.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any | None = None,
        prefix: str = "10xgraph:revoked",
    ) -> None:
        if client is None:
            if not url:
                raise ValueError("RedisTokenRevocation needs a Redis url or client")
            try:
                from redis.asyncio import Redis  # type: ignore[import]
            except ImportError as exc:  # pragma: no cover - depends on installed extras
                raise ImportError(
                    'token_revocation "redis" needs the redis package: '
                    'pip install "10xgraph-api[redis]"'
                ) from exc
            client = Redis.from_url(url, decode_responses=False)
        self._client = client
        self._prefix = prefix

    def _jti_key(self, jti: Any) -> str:
        return f"{self._prefix}:jti:{jti}"

    def _user_key(self, user_id: Any) -> str:
        return f"{self._prefix}:user:{user_id}"

    async def revoke_token(self, jti: str, expires_at: float | None = None) -> None:
        ttl = None if expires_at is None else max(1, int(expires_at - time.time()))
        await self._client.set(self._jti_key(jti), b"1", ex=ttl)

    async def revoke_user(
        self, user_id: str, before: float | None = None, ttl: int | None = None
    ) -> None:
        """Revoke tokens of ``user_id`` issued at or before ``before`` (default: now).

        ``ttl`` (seconds) can expire the entry once every older token has expired; pass your
        longest token lifetime.
        """
        cutoff = time.time() if before is None else float(before)
        await self._client.set(self._user_key(user_id), str(cutoff).encode(), ex=ttl)

    async def is_revoked(self, identity: dict[str, Any]) -> bool:
        jti, user_id = identity.get("jti"), _user_key_of(identity)
        keys = []
        if jti is not None:
            keys.append(self._jti_key(jti))
        if user_id is not None:
            keys.append(self._user_key(user_id))
        if not keys:
            return False
        values = await self._client.mget(keys)
        values = dict(zip(keys, values, strict=True))
        if jti is not None and values.get(self._jti_key(jti)) is not None:
            return True
        raw = values.get(self._user_key(user_id)) if user_id is not None else None
        if raw is None:
            return False
        cutoff = float(raw.decode() if isinstance(raw, bytes | bytearray) else raw)
        return _revoked_by_user_cutoff(identity, cutoff)

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            await close()


def build_token_revocation(spec: Any, redis_url: str | None = None) -> TokenRevocationBackend:
    """Build the backend named by the ``token_revocation`` key of 10xgraph.json."""
    if spec == "memory":
        return InMemoryTokenRevocation()
    if spec == "redis":
        if not redis_url:
            raise ValueError(
                'token_revocation "redis" needs a Redis URL: set "redis" in 10xgraph.json '
                "or REDIS_URL"
            )
        return RedisTokenRevocation(redis_url)
    if isinstance(spec, str) and ":" in spec:
        module_name, attr = spec.split(":", 1)
        obj = getattr(importlib.import_module(module_name), attr)
        if inspect.isclass(obj) and issubclass(obj, TokenRevocationBackend):
            obj = obj()
        if not isinstance(obj, TokenRevocationBackend):
            raise TypeError(f"{spec} is not a TokenRevocationBackend")
        return obj
    raise ValueError(
        f"Unsupported token_revocation {spec!r}. Use 'redis', 'memory' or 'module:attr'."
    )


def _bound_backend() -> TokenRevocationBackend | None:
    """The backend bound in the app's container (the request's, inside a request)."""
    from tenxgraph_api.src.app.core.utils.container import current_container

    return current_container().try_get(TokenRevocationBackend)


class TokenRevocationUnavailableError(RuntimeError):
    """The revocation backend could not answer; the request must not be served."""


async def token_revoked(
    identity: dict[str, Any], backend: TokenRevocationBackend | None = None
) -> bool:
    """Whether ``identity``'s credential has been revoked.

    False when no backend is configured or the identity is not a mapping. The ``jti`` is
    checked even when ``user_id`` is missing or odd; per-user revocation applies when
    ``user_id`` is a valid string. Raises :class:`TokenRevocationUnavailableError` when the
    backend fails (fail closed).
    """
    if not isinstance(identity, dict):
        return False
    backend = backend or _bound_backend()
    if backend is None:
        return False
    try:
        return bool(await backend.is_revoked(identity))
    except Exception as exc:
        logger.error("Token revocation check failed: %s", exc)
        raise TokenRevocationUnavailableError("Token revocation check unavailable") from exc
