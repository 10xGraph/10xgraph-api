"""Token revocation on the cache store, and its wiring."""

# ruff: noqa: S101

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from injectq import InjectQ

from tenxgraph_api.src.app.core.auth import revocation
from tenxgraph_api.src.app.core.auth.revocation import (
    TokenRevocationConfig,
    TokenRevocationUnavailableError,
    is_revoked,
    revoke_token,
    revoke_user,
    token_revoked,
)
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore


@pytest.fixture(autouse=True)
def _propagate_logs(monkeypatch):
    """The app's log setup (run by any test that builds the app) stops ``tenxgraph_api``
    from propagating; caplog listens on the root logger."""
    monkeypatch.setattr(logging.getLogger("tenxgraph_api"), "propagate", True)


# Reference time for issued-at values only; token expiry is computed in each test.
NOW = time.time()
ON = TokenRevocationConfig()

# ---------------------------------------------------------------------------
# revoke / check on the cache store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revokes_one_token_by_jti():
    store = InMemoryCacheStore()
    await revoke_token(store, "t-1", expires_at=time.time() + 60)
    assert await is_revoked(store, {"user_id": "a", "jti": "t-1"}) is True
    assert await is_revoked(store, {"user_id": "a", "jti": "t-2"}) is False
    assert await is_revoked(store, {"user_id": "a"}) is False


@pytest.mark.asyncio
async def test_token_entry_is_dropped_once_the_token_expired():
    store = InMemoryCacheStore()
    await revoke_token(store, "old", expires_at=time.time() - 1)  # kept for the 1 s minimum
    assert await store.get("revoked:jti:old") == b"1"
    await store.delete("revoked:jti:old")
    assert await is_revoked(store, {"user_id": "a", "jti": "old"}) is False


@pytest.mark.asyncio
async def test_revokes_a_users_older_tokens():
    store = InMemoryCacheStore()
    await revoke_user(store, "a", before=NOW)
    assert await is_revoked(store, {"user_id": "a", "iat": NOW - 10}) is True
    assert await is_revoked(store, {"user_id": "a", "iat": NOW}) is True
    assert await is_revoked(store, {"user_id": "a", "iat": NOW + 10}) is False  # new login
    assert await is_revoked(store, {"user_id": "a"}) is True  # undated token
    assert await is_revoked(store, {"user_id": "b", "iat": NOW - 10}) is False
    assert await is_revoked(store, {"jti": "nothing"}) is False
    assert await is_revoked(store, {}) is False


@pytest.mark.asyncio
async def test_revoke_user_defaults_to_the_store_clock():
    store = InMemoryCacheStore()
    store.now = AsyncMock(return_value=5000.0)  # type: ignore[method-assign]
    await revoke_user(store, "a")
    assert await store.get("revoked:user:a") == b"5000.000000"


# ---------------------------------------------------------------------------
# token_revoked: configuration and fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_revoked_needs_an_identity_and_the_feature_on(monkeypatch):
    store = InMemoryCacheStore()
    await revoke_user(store, "a")
    assert await token_revoked({}, store, ON) is False  # auth off: nothing to revoke
    assert await token_revoked({"user_id": "a"}, store, ON) is True
    monkeypatch.setattr(revocation, "_bound", lambda: (None, store))
    assert await token_revoked({"user_id": "a"}) is False  # not configured


@pytest.mark.asyncio
async def test_token_revoked_fails_closed_when_the_store_fails():
    store = MagicMock(spec=BaseCacheStore)
    store.get = AsyncMock(side_effect=ConnectionError("down"))
    with pytest.raises(TokenRevocationUnavailableError):
        await token_revoked({"user_id": "a"}, store, ON)


@pytest.mark.asyncio
async def test_token_revoked_fails_closed_without_a_cache_store(monkeypatch):
    monkeypatch.setattr(revocation, "_bound", lambda: (ON, None))
    with pytest.raises(TokenRevocationUnavailableError):
        await token_revoked({"user_id": "a"})


@pytest.mark.asyncio
async def test_token_revoked_uses_the_configured_token_lifetime():
    store = InMemoryCacheStore()
    await revoke_user(store, "a", before=NOW)
    fresh = {"user_id": "a", "exp": NOW + 3600 + 100}
    assert await token_revoked(fresh, store, ON) is True
    assert await token_revoked(fresh, store, TokenRevocationConfig(token_lifetime=3600)) is False


def test_unbound_settings_mean_revocation_is_off(monkeypatch):
    container = InjectQ()
    monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: container))
    assert revocation._bound() == (None, None)


def test_bound_settings_outside_a_request_use_the_global_container(monkeypatch):
    container = InjectQ()
    store = InMemoryCacheStore()
    container.bind_instance(TokenRevocationConfig, ON)
    container.bind_instance(BaseCacheStore, store)
    monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: container))
    assert revocation._bound() == (ON, store)


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
    config.token_revocation = TokenRevocationConfig.parse(spec)
    return config


def test_loader_binds_the_settings():
    from tenxgraph_api.src.app.loader import _enable_token_revocation

    container = InjectQ()
    container.bind_instance(BaseCacheStore, InMemoryCacheStore())
    _enable_token_revocation(container, _config({"token_lifetime": 60}), True)
    assert container.try_get(TokenRevocationConfig) == TokenRevocationConfig(token_lifetime=60)


@pytest.mark.parametrize(("spec", "auth"), [(None, True), (False, True), (True, False)])
def test_loader_skips_when_off_or_without_auth(spec, auth):
    from tenxgraph_api.src.app.loader import _enable_token_revocation

    container = InjectQ()
    _enable_token_revocation(container, _config(spec), auth)
    assert TokenRevocationConfig not in container


@pytest.mark.parametrize(
    ("mode", "workers", "warned"),
    [
        ("production", "1", True),
        ("development", "4", True),
        ("development", "1", False),
    ],
)
def test_loader_warns_when_revocations_stay_in_one_process(
    monkeypatch, caplog, mode, workers, warned
):
    from tenxgraph_api.src.app import loader

    monkeypatch.setattr(loader, "_is_production", lambda: mode == "production")
    monkeypatch.setenv("WEB_CONCURRENCY", workers)
    container = InjectQ()
    container.bind_instance(BaseCacheStore, InMemoryCacheStore())
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api"):
        loader._enable_token_revocation(container, _config(True), True)
    found = any("only the process" in r.getMessage() for r in caplog.records)
    assert found is warned


def test_loader_does_not_warn_with_a_shared_cache_store(monkeypatch, caplog):
    from tenxgraph_api.src.app import loader
    from tenxgraph_api.src.app.core.cache_store import RedisCacheStore

    monkeypatch.setattr(loader, "_is_production", lambda: True)
    container = InjectQ()
    container.bind_instance(BaseCacheStore, RedisCacheStore(client=MagicMock()))
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api"):
        loader._enable_token_revocation(container, _config(True), True)
    assert not any("only the process" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_graph_config_token_revocation(tmp_path):
    from tenxgraph_api.src.app.core.config.graph_config import (
        DEFAULT_USER_REVOCATION_TTL,
        GraphConfig,
    )

    path = tmp_path / "10xgraph.json"

    def parsed(value):
        path.write_text(f'{{"agent": "g:app", "cache_store": {{"token_revocation": {value}}}}}')
        return GraphConfig(str(path)).token_revocation

    assert parsed("true") == TokenRevocationConfig()
    assert parsed("true").user_ttl == DEFAULT_USER_REVOCATION_TTL
    assert parsed("false") is None
    assert parsed("null") is None
    assert parsed('{"enabled": false}') is None
    assert parsed('{"token_lifetime": 3600, "user_ttl": 60}') == TokenRevocationConfig(
        token_lifetime=3600, user_ttl=60
    )
    assert parsed('{"enabled": true, "user_ttl": null}').user_ttl is None
    for bad in ("5", '{"token_lifetime": -1}', '{"user_ttl": "1d"}', '{"backend": "redis"}'):
        with pytest.raises(ValueError, match="token_revocation"):
            parsed(bad)


@pytest.mark.parametrize("value", ['"redis"', '"memory"', '"my.mod:backend"'])
def test_string_token_revocation_is_a_config_error(tmp_path, value):
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

    path = tmp_path / "10xgraph.json"
    path.write_text(f'{{"agent": "g:app", "cache_store": {{"token_revocation": {value}}}}}')
    with pytest.raises(
        ValueError,
        match="token_revocation must be true, false, or an object with enabled, "
        "token_lifetime, user_ttl",
    ):
        _ = GraphConfig(str(path)).token_revocation


def test_config_editor_validates_token_revocation(monkeypatch):
    from tenxgraph_api.cli.config_editor import validate_config

    monkeypatch.delenv("REDIS_URL", raising=False)
    store = {"type": "redis", "url": "redis://cache:6379/0"}
    base = {"agent": "graph:app", "auth": "jwt"}
    issues = validate_config({**base, "cache_store": {**store, "token_revocation": True}})
    assert not [i for i in issues if i["level"] == "error"]
    for bad in ("memory", "redis", "bad path"):
        errors = [
            i
            for i in validate_config({**base, "cache_store": {**store, "token_revocation": bad}})
            if i["level"] == "error"
        ]
        assert errors and "token_revocation must be true, false" in errors[0]["message"]
    warn = validate_config({"agent": "graph:app", "cache_store": {"token_revocation": True}})
    assert "warning" in [i["level"] for i in warn if "token_revocation" in i["path"]]
