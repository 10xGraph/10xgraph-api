"""Revoke a token or every token of a user: ``10xgraph revoke``.

Writes to the same Redis the server reads (``token_revocation: "redis"`` in 10xgraph.json),
so the revocation applies to every worker at once. The server needs no restart.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.constants import DEFAULT_CONFIG_FILE
from tenxgraph_api.cli.core.config import resolve_default_config
from tenxgraph_api.cli.exceptions import ValidationError


class RevokeCommand(BaseCommand):
    """Revoke one token (``--jti``) or all of a user's older tokens (``--user``)."""

    def _redis_url(self, redis_url: str | None, config: str) -> str:
        if redis_url:
            return redis_url
        env_url = os.environ.get("REDIS_URL")
        if env_url:
            return env_url
        path = resolve_default_config(config)
        if path.exists():
            from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

            configured = GraphConfig(str(path)).redis_url
            if isinstance(configured, str) and configured and "${" not in configured:
                return configured
        raise ValidationError(
            "No Redis URL. Pass --redis-url, set REDIS_URL, or set `redis` in 10xgraph.json.",
            field="redis_url",
        )

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
            url = self._redis_url(redis_url, config)

            from tenxgraph_api.src.app.core.auth.revocation import RedisTokenRevocation

            async def run() -> None:
                backend = RedisTokenRevocation(url)
                try:
                    if user:
                        await backend.revoke_user(user, before=before)
                    else:
                        await backend.revoke_token(str(jti), expires_at=expires_at)
                finally:
                    await backend.aclose()

            asyncio.run(run())
            if user:
                self.output.success(f"Revoked the tokens of user {user} issued so far.")
            else:
                self.output.success(f"Revoked token {jti}.")
            return 0
        except Exception as e:
            return self.handle_error(e)
