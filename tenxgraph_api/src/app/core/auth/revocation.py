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
  without ``iat`` cannot be dated, so it counts as revoked while the user entry exists; give the
  backend your ``token_lifetime`` and such a token is dated from its ``exp`` instead, so one
  minted after the revoke is accepted. Tokens should carry ``iat``.

To revoke from your own code use :func:`revoke_token` and :func:`revoke_user`; from a shell
use ``10xgraph revoke``. A newer ``revoke_user`` cutoff is never replaced by an older one.

Enable it in ``10xgraph.json`` with ``"token_revocation": "redis"`` (shared by every worker;
uses the ``redis`` key or ``REDIS_URL``), ``"token_revocation": "memory"`` (one process; tests
and single-worker setups), or ``"token_revocation": "module:attr"`` for your own backend. A
``TokenRevocationBackend`` bound in your InjectQ container works too.

A backend error fails closed: the request is refused with 503 rather than served with a token
that might have been revoked.
"""

from __future__ import annotations

import asyncio
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


# A revoke_user entry is kept this long unless told otherwise. It has to outlive the longest
# token you issue, or an old token comes back to life when the entry goes.
DEFAULT_USER_REVOCATION_TTL = 30 * 24 * 3600


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


async def _redis_now(client: Any) -> float:
    """The Redis server's clock, so every pod agrees; this process's clock without ``TIME``."""
    server_time = getattr(client, "time", None)
    if not callable(server_time):
        return time.time()
    seconds, microseconds = await server_time()
    return float(seconds) + float(microseconds) / 1_000_000


# Keeps the newest cutoff: an older ``revoke_user(before=...)`` must not un-revoke tokens a
# newer call already revoked. An empty cutoff means "now" on the Redis clock.
_REVOKE_USER_LUA = """
local cutoff = tonumber(ARGV[1])
if cutoff == nil then
  local t = redis.call('TIME')
  cutoff = tonumber(t[1]) + tonumber(t[2]) / 1000000
end
local current = tonumber(redis.call('GET', KEYS[1]) or '')
if current ~= nil and current >= cutoff then
  return 0
end
redis.call('SET', KEYS[1], string.format('%.6f', cutoff))
local ttl = tonumber(ARGV[2])
if ttl ~= nil and ttl > 0 then
  redis.call('EXPIRE', KEYS[1], ttl)
end
return 1
"""


class InMemoryTokenRevocation(TokenRevocationBackend):
    """Revocation list held in this process. Fine for tests and single-worker servers."""

    def __init__(self, token_lifetime: float | None = None) -> None:
        self._token_lifetime = token_lifetime
        self._tokens: dict[str, float] = {}  # jti -> expiry (epoch seconds)
        self._users: dict[str, float] = {}  # user_id -> revoke tokens issued at or before

    def revoke_token(self, jti: str, expires_at: float | None = None) -> None:
        """Revoke one token. ``expires_at`` (its ``exp``) lets the entry be dropped later."""
        self._tokens[str(jti)] = float(expires_at) if expires_at is not None else float("inf")

    def revoke_user(self, user_id: str, before: float | None = None) -> None:
        """Revoke every token of ``user_id`` issued at or before ``before`` (default: now)."""
        cutoff = time.time() if before is None else float(before)
        key = str(user_id)
        self._users[key] = max(cutoff, self._users.get(key, cutoff))

    async def is_revoked(self, identity: dict[str, Any]) -> bool:
        now = time.time()
        self._tokens = {jti: exp for jti, exp in self._tokens.items() if exp > now}
        jti = identity.get("jti")
        if jti is not None and str(jti) in self._tokens:
            return True
        user_id = _user_key_of(identity)
        cutoff = self._users.get(user_id) if user_id is not None else None
        return _revoked_by_user_cutoff(identity, cutoff, self._token_lifetime)


class RedisTokenRevocation(TokenRevocationBackend):
    """Revocation list in Redis, shared by every worker and instance.

    Keys: ``<prefix>:jti:<jti>`` (expires with the token) and ``<prefix>:user:<user_id>``
    (holds the cutoff timestamp). A check reads both keys with two parallel ``GET`` calls (a
    single ``MGET`` over two keys fails with CROSSSLOT on Redis Cluster). Times come from the
    Redis clock, so pods with skewed clocks agree.

    ``token_lifetime`` is the longest lifetime of the tokens you issue, in seconds. It dates
    a token that has no ``iat`` from its ``exp``. ``user_ttl`` is how long a ``revoke_user``
    entry is kept when the call gives no ``ttl`` (``None`` keeps it forever).
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any | None = None,
        prefix: str = "10xgraph:revoked",
        token_lifetime: float | None = None,
        user_ttl: int | None = DEFAULT_USER_REVOCATION_TTL,
    ) -> None:
        self._token_lifetime = token_lifetime
        self._user_ttl = user_ttl
        if client is None:
            if not url:
                raise ValueError("RedisTokenRevocation needs a Redis url or client")
            from tenxgraph_api.src.app.core.redis_client import create_redis

            try:
                client = create_redis(url)
            except ImportError as exc:  # pragma: no cover - depends on installed extras
                raise ImportError(
                    'token_revocation "redis" needs the redis package: '
                    'pip install "10xgraph-api[redis]"'
                ) from exc
        self._client = client
        self._prefix = prefix

    def _jti_key(self, jti: Any) -> str:
        return f"{self._prefix}:jti:{jti}"

    def _user_key(self, user_id: Any) -> str:
        return f"{self._prefix}:user:{user_id}"

    async def revoke_token(self, jti: str, expires_at: float | None = None) -> None:
        ttl = None
        if expires_at is not None:
            ttl = max(1, int(expires_at - await _redis_now(self._client)))
        await self._client.set(self._jti_key(jti), b"1", ex=ttl)

    async def revoke_user(
        self, user_id: str, before: float | None = None, ttl: int | None = None
    ) -> None:
        """Revoke tokens of ``user_id`` issued at or before ``before`` (default: now).

        ``ttl`` (seconds) expires the entry once every older token has expired; it defaults to
        ``user_ttl``. An older ``before`` than the one already stored changes nothing.
        """
        cutoff = "" if before is None else repr(float(before))
        keep_for = self._user_ttl if ttl is None else ttl
        await self._client.eval(
            _REVOKE_USER_LUA, 1, self._user_key(user_id), cutoff, str(keep_for or 0)
        )

    async def is_revoked(self, identity: dict[str, Any]) -> bool:
        jti, user_id = identity.get("jti"), _user_key_of(identity)
        if jti is None and user_id is None:
            return False
        # Two plain GETs, not one MGET: the keys hash to different slots on Redis Cluster.
        jti_value, raw = await asyncio.gather(
            self._client.get(self._jti_key(jti)) if jti is not None else _none(),
            self._client.get(self._user_key(user_id)) if user_id is not None else _none(),
        )
        if jti_value is not None:
            return True
        if raw is None:
            return False
        cutoff = float(raw.decode() if isinstance(raw, bytes | bytearray) else raw)
        return _revoked_by_user_cutoff(identity, cutoff, self._token_lifetime)

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            await close()


async def _none() -> None:
    return None


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


async def _call(method: Any, *args: Any, **kwargs: Any) -> None:
    """Call a backend method that may be sync (in-memory) or async (Redis)."""
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        await result


def _backend_or_raise(backend: TokenRevocationBackend | None) -> Any:
    backend = backend or _bound_backend()
    if backend is None:
        raise RuntimeError(
            'No token revocation backend is configured. Set "token_revocation": "redis" in '
            "10xgraph.json (with Redis configured), or bind a TokenRevocationBackend."
        )
    return backend


async def revoke_token(
    jti: str, expires_at: float | None = None, *, backend: TokenRevocationBackend | None = None
) -> None:
    """Revoke one token by its ``jti`` claim. ``expires_at`` is its ``exp`` (epoch seconds)."""
    await _call(_backend_or_raise(backend).revoke_token, jti, expires_at)


async def revoke_user(
    user_id: str, before: float | None = None, *, backend: TokenRevocationBackend | None = None
) -> None:
    """Revoke every token of ``user_id`` issued at or before ``before`` (default: now)."""
    await _call(_backend_or_raise(backend).revoke_user, user_id, before)
