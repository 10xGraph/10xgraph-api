"""Revoked tokens are refused on the real request path."""

# ruff: noqa: S101

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from tenxgraph_api.src.app.core.auth.revocation import TokenRevocationConfig, revoke_user
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore
from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router

from .conftest import build_app, make_client, user_headers


HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_UNAVAILABLE = 503


def _client(cache: BaseCacheStore):
    app = build_app(
        routers=[checkpointer_router],
        extra_bindings={BaseCacheStore: cache, TokenRevocationConfig: TokenRevocationConfig()},
    )
    return make_client(app)


def test_revoked_user_is_refused_and_others_are_not():
    cache = InMemoryCacheStore()
    asyncio.run(revoke_user(cache, "mallory"))  # HeaderAuth identities carry no iat -> revoked
    client = _client(cache)

    assert client.get("/v1/threads", headers=user_headers("alice")).status_code == HTTP_OK
    resp = client.get("/v1/threads", headers=user_headers("mallory"))
    assert resp.status_code == HTTP_UNAUTHORIZED
    assert "revoked" in resp.text


def test_revocation_outage_fails_closed():
    cache = MagicMock(spec=BaseCacheStore)
    cache.get = AsyncMock(side_effect=ConnectionError("redis down"))
    client = _client(cache)
    assert client.get("/v1/threads", headers=user_headers("alice")).status_code == (
        HTTP_UNAVAILABLE
    )


def test_revocation_off_ignores_the_cache_store():
    cache = MagicMock(spec=BaseCacheStore)
    cache.get = AsyncMock(side_effect=ConnectionError("redis down"))
    app = build_app(routers=[checkpointer_router], extra_bindings={BaseCacheStore: cache})
    client = make_client(app)
    assert client.get("/v1/threads", headers=user_headers("alice")).status_code == HTTP_OK
