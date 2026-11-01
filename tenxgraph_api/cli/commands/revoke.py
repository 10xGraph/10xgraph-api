"""Revoke a token or every token of a user: ``10xgraph revoke``.

Writes to the cache store the server reads (``cache_store.token_revocation`` in
10xgraph.json), picked the same way the server picks it, from ``cache_store``. With a shared
store (type ``redis`` or ``custom``) the revocation applies to every worker at once; the server
needs no restart. ``--redis-url`` overrides the selection.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.constants import DEFAULT_CONFIG_FILE
from tenxgraph_api.cli.core.config import resolve_default_config
from tenxgraph_api.cli.exceptions import ValidationError


class RevokeCommand(BaseCommand):
    """Revoke one token (``--jti``) or all of a user's older tokens (``--user``)."""

    def _cache_store(self, redis_url: str | None, config: str) -> tuple[Any, float | None]:
        """The cache store to write to and the default ``user_ttl`` from the project config."""
        from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore, RedisCacheStore
        from tenxgraph_api.src.app.core.cache_store.loader import build_cache_store
        from tenxgraph_api.src.app.core.config.graph_config import (
            DEFAULT_USER_REVOCATION_TTL,
            GraphConfig,
        )

        path = resolve_default_config(config)
        graph_config = GraphConfig(str(path)) if path.exists() else None
        user_ttl: float | None = DEFAULT_USER_REVOCATION_TTL
        if graph_config is not None:
            settings = graph_config.token_revocation  # raises for an invalid value
            if settings is not None:
                user_ttl = settings.user_ttl
        if redis_url:
            return RedisCacheStore.from_url(redis_url), user_ttl

        if graph_config is not None:
            # A custom cache_store.path imports from the project, like the server does.
            root = str(path.resolve().parent)
            if root not in sys.path:
                sys.path.insert(0, root)
        # GraphConfig has loaded the project's env file, so cache_store.url expands after it.
        store, source = build_cache_store(graph_config)
        if isinstance(store, InMemoryCacheStore):
            raise ValidationError(
                f"The cache store is in-memory ({source}): a CLI process cannot reach the "
                'server\'s memory. Set "cache_store": {"type": "redis", "url": ...} (or type '
                "custom) in 10xgraph.json, the same store the server uses, or pass --redis-url.",
                field="redis_url",
            )
        return store, user_ttl

    def execute(
        self,
        user: str | None = None,
        jti: str | None = None,
        before: float | None = None,
        expires_at: float | None = None,
        redis_url: str | None = None,
        config: str = DEFAULT_CONFIG_FILE,
        **kwargs: Any,
    ) -> int:
        try:
            if bool(user) == bool(jti):
                raise ValidationError("Pass exactly one of --user or --jti.", field="user")
            store, user_ttl = self._cache_store(redis_url, config)

            from tenxgraph_api.src.app.core.auth import revocation

            async def run() -> None:
                try:
                    if user:
                        await revocation.revoke_user(store, user, before=before, ttl=user_ttl)
                    else:
                        await revocation.revoke_token(store, str(jti), expires_at=expires_at)
                finally:
                    await store.aclose()

            asyncio.run(run())
            if user:
                self.output.success(f"Revoked the tokens of user {user} issued so far.")
            else:
                self.output.success(f"Revoked token {jti}.")
            return 0
        except Exception as e:
            return self.handle_error(e)
