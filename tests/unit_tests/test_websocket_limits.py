"""WebSocket abuse limits (H6): per-run rate limit, token re-check, per-user connection cap.

The handshake guard only runs once, but one socket can start any number of graph runs and
stay open long after its token expires. These tests cover the limits that apply for the
lifetime of the connection.
"""

from typing import Any

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations import setup_fastapi
from starlette.websockets import WebSocketDisconnect

from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore, SlotResult
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, WebSocketConfig
from tenxgraph_api.src.app.core.leases import LeaseManager
from tenxgraph_api.src.app.core.middleware.rate_limit import keying
from tenxgraph_api.src.app.routers.graph import realtime_guard
from tenxgraph_api.src.app.routers.graph.realtime_guard import (
    realtime_connection_guard,
    ws_identity_still_valid,
    ws_run_allowed,
)


class _Backend(InMemoryCacheStore):
    """A cache store whose rate-limit window grants the first ``budget`` requests."""

    def __init__(self, budget: int):
        super().__init__()
        self.budget = budget
        self.calls = 0

    async def acquire(self, key, limit, ttl):
        if not key.startswith("ratelimit:"):
            return await super().acquire(key, limit, ttl)
        self.calls += 1
        if self.calls <= self.budget:
            return SlotResult(granted=True, count=self.calls, oldest_expiry=None, slot_id="s")
        return SlotResult(
            granted=False, count=limit, oldest_expiry=await self.now() + 7, slot_id=None
        )


class _RL:
    requests = 100
    window = 60
    by = "ip"
    trusted_proxy_headers = False
    trusted_proxy_hops = 1
    trusted_proxies = ()
    fail_open = True


class _Config:
    def __init__(self, websocket: WebSocketConfig, rate_limit=None):
        self._ws = websocket
        self._rl = rate_limit

    def auth_config(self):
        return None

    @property
    def rate_limit(self):
        return self._rl

    @property
    def websocket(self):
        return self._ws


def _app(config, backend=None) -> TestClient:
    container = InjectQ()
    container.bind_instance(GraphConfig, config, allow_none=True)
    if backend is not None:
        container.bind_instance(BaseCacheStore, backend)
    app = FastAPI()
    setup_fastapi(container, app)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, _guard: None = Depends(realtime_connection_guard)):
        await websocket.accept()
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                return
            retry_after = await ws_run_allowed(websocket)
            await websocket.send_json({"retry_after": retry_after})

    return TestClient(app)


@pytest.fixture(autouse=True)
def leases(monkeypatch):
    """A fresh in-process lease manager behind the connection caps."""
    manager = LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(realtime_guard, "lease_manager", lambda: manager)
    return manager


@pytest.fixture
def caller(monkeypatch):
    """Set which verified user the guard sees (``None`` = anonymous / invalid token)."""

    def _set(user_id: str | None):
        monkeypatch.setattr(keying, "verified_user_id", lambda connection: user_id)

    _set(None)
    return _set


# ---------------------------------------------------------------------------
# config defaults
# ---------------------------------------------------------------------------


def test_absent_limits_get_finite_defaults():
    cfg = WebSocketConfig.from_dict({})
    assert cfg.max_connections == 1000
    assert cfg.max_connections_per_user == 10


@pytest.mark.parametrize("value", [0, None])
def test_explicit_zero_or_null_is_unlimited(value):
    cfg = WebSocketConfig.from_dict({"max_connections": value, "max_connections_per_user": value})
    assert cfg.max_connections is None
    assert cfg.max_connections_per_user is None


def test_negative_per_user_limit_rejected():
    with pytest.raises(ValueError, match="max_connections_per_user"):
        WebSocketConfig.from_dict({"max_connections_per_user": -1})


def test_missing_websocket_section_uses_defaults(tmp_path):
    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "graph.react:app"}')
    ws = GraphConfig(str(path)).websocket
    assert (ws.max_connections, ws.max_connections_per_user) == (1000, 10)


# ---------------------------------------------------------------------------
# per-user connection cap
# ---------------------------------------------------------------------------


def test_one_user_cannot_hold_every_slot(caller):
    caller("alice")
    client = _app(_Config(WebSocketConfig(max_connections=100, max_connections_per_user=1)))
    with client.websocket_connect("/ws"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws") as second:
                second.send_text("x")
                second.receive_json()
        assert exc.value.code == realtime_guard.WS_TRY_AGAIN_LATER
    assert realtime_guard.lease_manager().held("ws:all") == 0


def test_other_users_are_not_affected(caller):
    client = _app(_Config(WebSocketConfig(max_connections=100, max_connections_per_user=1)))
    caller("alice")
    with client.websocket_connect("/ws"):
        caller("bob")
        with client.websocket_connect("/ws") as bob:
            bob.send_text("x")
            assert bob.receive_json() == {"retry_after": None}


def test_user_slot_is_released_on_disconnect(caller):
    caller("alice")
    client = _app(_Config(WebSocketConfig(max_connections=100, max_connections_per_user=1)))
    for _ in range(2):
        with client.websocket_connect("/ws") as conn:
            conn.send_text("x")
            conn.receive_json()
    assert realtime_guard.lease_manager()._held == {}


# ---------------------------------------------------------------------------
# every run counts against the rate limit
# ---------------------------------------------------------------------------


def test_each_run_is_rate_limited(caller):
    backend = _Backend(budget=3)  # handshake + 2 runs
    client = _app(_Config(WebSocketConfig(max_connections=None), rate_limit=_RL()), backend)
    with client.websocket_connect("/ws") as conn:
        results = []
        for _ in range(3):
            conn.send_text("run")
            results.append(conn.receive_json()["retry_after"])
    assert results == [None, None, 7]


def test_runs_unlimited_when_rate_limit_is_off(caller):
    client = _app(_Config(WebSocketConfig(max_connections=None)))
    with client.websocket_connect("/ws") as conn:
        for _ in range(5):
            conn.send_text("run")
            assert conn.receive_json() == {"retry_after": None}


# ---------------------------------------------------------------------------
# the token is re-verified before each run
# ---------------------------------------------------------------------------


class _Ws:
    pass


def test_identity_check_passes_while_token_is_valid(caller):
    caller("alice")
    assert ws_identity_still_valid(_Ws(), {"user_id": "alice"}) is True


def test_identity_check_fails_once_token_stops_verifying(caller):
    caller(None)  # expired or revoked
    assert ws_identity_still_valid(_Ws(), {"user_id": "alice"}) is False


def test_identity_check_fails_if_token_now_belongs_to_someone_else(caller):
    caller("mallory")
    assert ws_identity_still_valid(_Ws(), {"user_id": "alice"}) is False


def test_identity_check_skipped_without_auth(caller):
    user: dict[str, Any] = {}
    assert ws_identity_still_valid(_Ws(), user) is True


# ---------------------------------------------------------------------------
# the handshake and the runs count in the bound cache store (no TestClient socket)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_and_runs_share_the_bound_cache_store(monkeypatch):
    from types import SimpleNamespace

    from fastapi import WebSocketException

    class _Global(_RL):
        by = "global"
        requests = 2

    store = InMemoryCacheStore()
    monkeypatch.setattr(realtime_guard, "bound_cache_store", lambda: store)
    monkeypatch.setattr(realtime_guard, "lease_manager", lambda: LeaseManager(store))
    monkeypatch.setattr(keying, "verified_user_id", lambda connection: None)
    websocket = SimpleNamespace(state=SimpleNamespace())
    ws_config = WebSocketConfig(max_connections=None, max_connections_per_user=None)
    config = _Config(ws_config, rate_limit=_Global())

    guard = realtime_connection_guard(websocket, config)  # type: ignore[arg-type]
    await guard.__anext__()  # handshake: one request
    assert await ws_run_allowed(websocket) is None  # run 1: the second request
    retry_after = await ws_run_allowed(websocket)  # run 2: over the limit
    assert retry_after is not None and 1 <= retry_after <= 60
    assert (await store.acquire("ratelimit:__global__", 10, 60)).count == 3
    await guard.aclose()

    with pytest.raises(WebSocketException):  # the next handshake is over the limit too
        await realtime_connection_guard(websocket, config).__anext__()  # type: ignore[arg-type]
