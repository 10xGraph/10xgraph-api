"""``cache_store`` section of 10xgraph.json, with token revocation."""

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass
from tenxgraph_api.src.app.core.config.sections.common import _expand_env, _parse_bool
from tenxgraph_api.src.app.core.config.sections.rate_limit import RateLimitConfig


logger = logging.getLogger("tenxgraph_api")


# A revoke_user entry is kept this long unless told otherwise. It has to outlive the longest
# token you issue, or an old token comes back to life when the entry goes.
DEFAULT_USER_REVOCATION_TTL = 30 * 24 * 3600


def _positive_seconds(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        raise ValueError(f"{field} must be a positive number of seconds, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class TokenRevocationConfig:
    """``cache_store.token_revocation`` in 10xgraph.json: ``true``, or an object.

    Example::

        "cache_store": {
            "type": "redis",
            "url": "${REDIS_URL}",
            "token_revocation": {"enabled": true, "token_lifetime": 3600, "user_ttl": 2592000}
        }

    Revocations live in the API's cache store: shared between workers with a Redis or custom
    store, per process with the in-memory one. ``token_lifetime`` is the longest lifetime of the
    tokens you issue, in seconds; it dates a token that has no ``iat`` from its ``exp``.
    ``user_ttl`` is how long a ``revoke_user`` entry is kept by default (``null`` keeps it
    until deleted).
    """

    token_lifetime: float | None = None
    user_ttl: float | None = DEFAULT_USER_REVOCATION_TTL

    @classmethod
    def parse(cls, value: object) -> "TokenRevocationConfig | None":
        """The settings for ``value``; None when token revocation is off."""
        if value is None or value is False:
            return None
        if value is True:
            return cls()
        if not isinstance(value, dict):
            raise ValueError(
                "token_revocation must be true, false, or an object with enabled, "
                f"token_lifetime, user_ttl; got {value!r}"
            )
        unknown = set(value) - {"enabled", "token_lifetime", "user_ttl"}
        if unknown:
            raise ValueError(
                f"token_revocation: unknown keys {sorted(unknown)}; use enabled, "
                "token_lifetime and user_ttl"
            )
        if not _parse_bool(value.get("enabled", True), field="token_revocation.enabled"):
            return None
        lifetime = value.get("token_lifetime")
        user_ttl = value.get("user_ttl", DEFAULT_USER_REVOCATION_TTL)
        return cls(
            token_lifetime=(
                None
                if lifetime is None
                else _positive_seconds(lifetime, field="token_revocation.token_lifetime")
            ),
            user_ttl=(
                None
                if user_ttl is None
                else _positive_seconds(user_ttl, field="token_revocation.user_ttl")
            ),
        )


CACHE_STORE_TYPES: tuple[str, ...] = ("memory", "redis", "custom")
CACHE_STORE_KEYS: tuple[str, ...] = ("type", "url", "path", "rate_limit", "token_revocation")

# Top-level keys whose setting belongs inside ``cache_store``, with where it goes. A leftover
# top-level key is a config error, never ignored: a forgotten ``rate_limit`` would otherwise
# turn rate limiting off without a word.
_CACHE_STORE_SETTINGS = {
    "redis": (
        '`redis` goes inside `cache_store` as `url`: {"cache_store": {"type": "redis", '
        '"url": "${REDIS_URL}"}}'
    ),
    "rate_limit": (
        '`rate_limit` goes inside `cache_store`: {"cache_store": {"type": ..., '
        '"rate_limit": {...}}}'
    ),
    "token_revocation": (
        '`token_revocation` goes inside `cache_store`: {"cache_store": {"type": ..., '
        '"token_revocation": true}}'
    ),
}


def check_cache_store_settings(data: dict) -> None:
    """Raise when a setting that belongs inside ``cache_store`` sits at the top level."""
    for key, message in _CACHE_STORE_SETTINGS.items():
        if key in data:
            raise ValueError(message)


@dataclass(frozen=True)
class CacheStoreConfig:
    """``cache_store`` in 10xgraph.json: the API's cache store and the features it backs.

    Example::

        "cache_store": {
            "type": "redis",
            "url": "${REDIS_URL}",
            "rate_limit": {"requests": 100, "window": 60, "by": "user"},
            "token_revocation": true
        }

    ``type`` picks the store: ``memory`` (the default; one process, not shared between
    workers), ``redis`` (a ``RedisCacheStore`` on ``url``; also Valkey, Dragonfly, KeyDB) or
    ``custom`` (``path`` is ``"module:attr"``, a ``BaseCacheStore`` instance or zero-argument
    factory). ``url`` expands ``$VAR`` / ``${VAR}`` when the store is built, and an unset
    variable is a config error. The store holds the run locks and caps, idempotency records
    and the ownership cache, plus the counters of ``rate_limit`` (:class:`RateLimitConfig`) and
    the revocations of ``token_revocation`` (:class:`TokenRevocationConfig`); both are off
    when absent. ``rate_limit`` is kept here even when its ``enabled`` is false.
    """

    type: str = "memory"
    url: str | None = None  # as written; see expanded_url
    path: str | None = None
    rate_limit: RateLimitConfig | None = None
    token_revocation: TokenRevocationConfig | None = None
    type_set: bool = False  # whether 10xgraph.json names the type

    @classmethod
    def parse(cls, value: object) -> "CacheStoreConfig":
        """The settings for ``value`` (the raw ``cache_store``); memory defaults for None."""
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError(
                'cache_store must be an object: {"type": "memory"}, {"type": "redis", "url": '
                '"${REDIS_URL}"} or {"type": "custom", "path": "module:attr"}'
            )
        unknown = sorted(set(value) - set(CACHE_STORE_KEYS))
        if unknown:
            raise ValueError(
                f"cache_store: unknown keys {unknown}; valid keys: {', '.join(CACHE_STORE_KEYS)}"
            )
        kind = value.get("type")
        type_set = kind is not None
        if kind is None:
            kind = "memory"
        if kind not in CACHE_STORE_TYPES:
            raise ValueError(
                f"cache_store.type must be one of {', '.join(CACHE_STORE_TYPES)}; got {kind!r}"
            )
        url = value.get("url")
        path = value.get("path")
        if url is not None and kind != "redis":
            raise ValueError('cache_store.url goes with "type": "redis"')
        if path is not None and kind != "custom":
            raise ValueError('cache_store.path goes with "type": "custom"')
        if url is not None and (not isinstance(url, str) or not url.strip()):
            raise ValueError(
                'cache_store.url must be a Redis URL string, such as "redis://host:6379/0" '
                'or "${CACHE_REDIS_URL}". Leave it out to use the REDIS_URL environment variable.'
            )
        if kind == "custom" and (not isinstance(path, str) or ":" not in path):
            raise ValueError(
                'cache_store.path is required with "type": "custom": "module:attr" naming a '
                "BaseCacheStore instance or a zero-argument factory"
            )
        rate_limit = value.get("rate_limit")
        return cls(
            type=kind,
            url=url,
            path=path,
            rate_limit=None if rate_limit is None else RateLimitConfig.from_dict(rate_limit),
            token_revocation=TokenRevocationConfig.parse(value.get("token_revocation")),
            type_set=type_set,
        )

    @property
    def url_source(self) -> str:
        """Where the Redis URL comes from: ``cache_store.url``, else the ``REDIS_URL`` variable."""
        return "cache_store.url" if self.url is not None else "REDIS_URL"

    def expanded_url(self) -> str | None:
        """The Redis URL: ``url`` with ``$VAR`` / ``${VAR}`` expanded, else ``REDIS_URL``.

        Only for ``type`` redis. An unset variable, or neither ``url`` nor ``REDIS_URL``,
        raises ValueError.
        """
        if self.type != "redis":
            return None
        return redis_url_or_env(self.url, "cache_store.url")


def redis_url_or_env(raw: str | None, key: str) -> str:
    """``raw`` with ``$VAR`` / ``${VAR}`` expanded, else the ``REDIS_URL`` variable.

    ``key`` names the config value in the errors. Raises ValueError for an unset variable,
    or when neither ``raw`` nor ``REDIS_URL`` is set.
    """
    if raw is None:
        env_url = os.environ.get("REDIS_URL", "").strip()
        if not env_url:
            raise ValueError(
                f"{key} is required: set it in 10xgraph.json, or the REDIS_URL environment variable"
            )
        return env_url
    try:
        return _expand_env(raw) or raw
    except ValueError:
        raise ValueError(f"{key}: {raw} uses an environment variable that is not set") from None
