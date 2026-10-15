"""Revoked tokens are refused on the real request path."""

# ruff: noqa: S101

from __future__ import annotations

from typing import Any

from tenxgraph_api.src.app.core.auth.revocation import (
    InMemoryTokenRevocation,
    TokenRevocationBackend,
)
from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router

from .conftest import build_app, make_client, user_headers


HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_UNAVAILABLE = 503


def _client(backend: TokenRevocationBackend):
    app = build_app(routers=[checkpointer_router], extra_bindings={TokenRevocationBackend: backend})
    return make_client(app)


def test_revoked_user_is_refused_and_others_are_not():
    backend = InMemoryTokenRevocation()
    backend.revoke_user("mallory")  # HeaderAuth identities carry no iat -> revoked
    client = _client(backend)

    assert client.get("/v1/threads", headers=user_headers("alice")).status_code == HTTP_OK
    resp = client.get("/v1/threads", headers=user_headers("mallory"))
    assert resp.status_code == HTTP_UNAUTHORIZED
    assert "revoked" in resp.text


def test_revocation_outage_fails_closed():
    class _Down(TokenRevocationBackend):
        async def is_revoked(self, identity: dict[str, Any]) -> bool:
            raise ConnectionError("redis down")

    client = _client(_Down())
    assert client.get("/v1/threads", headers=user_headers("alice")).status_code == (
        HTTP_UNAVAILABLE
    )
