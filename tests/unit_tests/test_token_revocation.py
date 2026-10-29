"""Token revocation backends and wiring."""

# ruff: noqa: S101

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from injectq import InjectQ

from tenxgraph_api.src.app.core.auth import revocation
from tenxgraph_api.src.app.core.auth.revocation import (
    InMemoryTokenRevocation,
    RedisTokenRevocation,
    TokenRevocationBackend,
    TokenRevocationUnavailableError,
    build_token_revocation,
    token_revoked,
)


NOW = time.time()


# ---------------------------------------------------------------------------
# in-memory backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_revokes_one_token_by_jti():
    backend = InMemoryTokenRevocation()
    backend.revoke_token("t-1", expires_at=NOW + 60)
    assert await backend.is_revoked({"user_id": "a", "jti": "t-1"}) is True
    assert await backend.is_revoked({"user_id": "a", "jti": "t-2"}) is False
    assert await backend.is_revoked({"user_id": "a"}) is False


@pytest.mark.asyncio
async def test_memory_drops_entries_once_the_token_expired():
    backend = InMemoryTokenRevocation()
    backend.revoke_token("old", expires_at=NOW - 1)
    assert await backend.is_revoked({"user_id": "a", "jti": "old"}) is False
    assert backend._tokens == {}


@pytest.mark.asyncio
async def test_memory_revokes_a_users_older_tokens():
    backend = InMemoryTokenRevocation()
    backend.revoke_user("a", before=NOW)
    assert await backend.is_revoked({"user_id": "a", "iat": NOW - 10}) is True
    assert await backend.is_revoked({"user_id": "a", "iat": NOW}) is True
    assert await backend.is_revoked({"user_id": "a", "iat": NOW + 10}) is False  # new login
    assert await backend.is_revoked({"user_id": "a"}) is True  # undated token
    assert await backend.is_revoked({"user_id": "b", "iat": NOW - 10}) is False


# ---------------------------------------------------------------------------
# Redis backend (fake client)
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, Any] = {}
        self.ttl: dict[str, Any] = {}
        self.closed = False

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl[key] = ex

    async def mget(self, keys):
        return [self.data.get(k) for k in keys]

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_redis_token_and_user_revocation():
    client = _FakeRedis()
    backend = RedisTokenRevocation(client=client, prefix="p")
    await backend.revoke_token("t-1", expires_at=time.time() + 120)
    assert 1 <= client.ttl["p:jti:t-1"] <= 120
    assert await backend.is_revoked({"user_id": "a", "jti": "t-1"}) is True
    assert await backend.is_revoked({"user_id": "a", "jti": "t-2", "iat": NOW}) is False

    await backend.revoke_user("a", before=NOW, ttl=3600)
    assert client.ttl["p:user:a"] == 3600
    assert await backend.is_revoked({"user_id": "a", "iat": NOW - 1}) is True
    assert await backend.is_revoked({"user_id": "a", "iat": NOW + 1}) is False
    assert await backend.is_revoked({"jti": "nothing"}) is False
    assert await backend.is_revoked({}) is False

    await backend.aclose()
    assert client.closed


def test_redis_needs_a_url_or_client():
    with pytest.raises(ValueError, match="url or client"):
        RedisTokenRevocation()


# ---------------------------------------------------------------------------
# factory and lookup
# ---------------------------------------------------------------------------


class _Custom(TokenRevocationBackend):
    async def is_revoked(self, identity):
        return False


CUSTOM_INSTANCE = _Custom()


def test_build_token_revocation():
    assert isinstance(build_token_revocation("memory"), InMemoryTokenRevocation)
    assert isinstance(
        build_token_revocation("redis", "redis://localhost:6379/0"), RedisTokenRevocation
    )
    with pytest.raises(ValueError, match="needs a Redis URL"):
        build_token_revocation("redis", None)
    assert isinstance(build_token_revocation(f"{__name__}:_Custom"), _Custom)
    assert build_token_revocation(f"{__name__}:CUSTOM_INSTANCE") is CUSTOM_INSTANCE
    with pytest.raises(TypeError):
        build_token_revocation(f"{__name__}:NOW")
    with pytest.raises(ValueError, match="Unsupported"):
        build_token_revocation("sometimes")


@pytest.mark.asyncio
async def test_token_revoked_needs_an_identity_and_a_backend(monkeypatch):
    backend = InMemoryTokenRevocation()
    backend.revoke_user("a")
    assert await token_revoked({}, backend) is False  # auth off: nothing to revoke
    assert await token_revoked({"user_id": "a"}, backend) is True
    monkeypatch.setattr(revocation, "_bound_backend", lambda: None)
    assert await token_revoked({"user_id": "a"}) is False  # not configured


@pytest.mark.asyncio
async def test_token_revoked_fails_closed():
    class _Down(TokenRevocationBackend):
        async def is_revoked(self, identity):
            raise ConnectionError("down")

    with pytest.raises(TokenRevocationUnavailableError):
        await token_revoked({"user_id": "a"}, _Down())


def test_bound_backend_outside_a_request_uses_the_global_container(monkeypatch):
    container = InjectQ()
    backend = InMemoryTokenRevocation()
    container.bind_instance(TokenRevocationBackend, backend)
    monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: container))
    assert revocation._bound_backend() is backend


# ---------------------------------------------------------------------------
# WebSocket: a token revoked after the handshake stops the next run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [True, "outage"])
async def test_ws_run_gate_closes_on_revocation(monkeypatch, revoked):
    import importlib

    # The package re-exports the APIRouter as ``router``; patch the module itself.
    graph_router = importlib.import_module("tenxgraph_api.src.app.routers.graph.router")

    async def fake_revoked(user):
        if revoked == "outage":
            raise TokenRevocationUnavailableError("down")
        return True

    monkeypatch.setattr(graph_router, "token_revoked", fake_revoked)
    monkeypatch.setattr(graph_router, "ws_identity_still_valid", lambda ws, user: True)
    websocket = MagicMock()
    websocket.send_text = AsyncMock()
    websocket.close = AsyncMock()

    assert await graph_router._ws_run_gate(websocket, {"user_id": "a"}) == "close"
    websocket.close.assert_awaited_once_with(code=1008)


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------


def _config(spec):
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

    config = MagicMock(spec=GraphConfig)
    config.token_revocation = spec
    return config


def test_loader_binds_the_configured_backend():
    from tenxgraph_api.src.app.loader import _bind_token_revocation

    container = InjectQ()
    _bind_token_revocation(container, _config("memory"), True, None)
    assert isinstance(container.try_get(TokenRevocationBackend), InMemoryTokenRevocation)


def test_loader_keeps_a_backend_the_developer_bound():
    from tenxgraph_api.src.app.loader import _bind_token_revocation

    container = InjectQ()
    container.bind_instance(TokenRevocationBackend, CUSTOM_INSTANCE)
    _bind_token_revocation(container, _config("memory"), True, None)
    assert container.try_get(TokenRevocationBackend) is CUSTOM_INSTANCE


@pytest.mark.parametrize(("spec", "auth"), [(None, True), ("memory", False)])
def test_loader_skips_when_off_or_without_auth(spec, auth):
    from tenxgraph_api.src.app.loader import _bind_token_revocation

    container = InjectQ()
    with patch.object(container, "bind_instance") as bind:
        _bind_token_revocation(container, _config(spec), auth, None)
    bind.assert_not_called()


def test_graph_config_token_revocation(tmp_path):
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "g:app", "token_revocation": "redis"}')
    assert GraphConfig(str(path)).token_revocation == "redis"
    path.write_text('{"agent": "g:app", "token_revocation": 5}')
    with pytest.raises(ValueError, match="token_revocation"):
        _ = GraphConfig(str(path)).token_revocation


def test_config_editor_validates_token_revocation():
    from tenxgraph_api.cli.config_editor import validate_config

    base = {"agent": "graph:app", "auth": "jwt"}
    issues = validate_config({**base, "token_revocation": "memory"})
    assert not [i for i in issues if i["path"].startswith("token_revocation")]
    assert any(
        i["level"] == "error" for i in validate_config({**base, "token_revocation": "bad path"})
    )
    warn = validate_config({"agent": "graph:app", "token_revocation": "memory"})
    assert [i["level"] for i in warn if i["path"] == "token_revocation"] == ["warning"]
