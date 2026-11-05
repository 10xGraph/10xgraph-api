"""Regression tests for the Sprint 7 store review findings A2, A3, A4 and A6.

A2: the D6 tenant column check runs for the store bound from ``10xgraph.json``.
A3: a delete tombstone exempts only a caller matching the deleter on every tenant column.
A4: a failed L2 eviction is visible, and tenant scope entries use a short L2 TTL.
"""

# ruff: noqa: S101, PLR2004

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from injectq import InjectQ
from tenxgraph.core.exceptions import GraphError
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.storage.columns import parse_columns

from tenxgraph_api.src.app.core.auth.authorization import OwnershipAuthorizationBackend
from tenxgraph_api.src.app.core.auth.ownership_resolver import (
    DEFAULT_L2_TTL_SECONDS,
    TENANT_L2_TTL_SECONDS,
    OwnershipCacheEvictionError,
    ThreadOwnershipResolver,
)
from tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service import (
    CheckpointerService,
)


class _Cache:
    """A shared cache store double (both workers see the same dict)."""

    def __init__(self, *, fail_delete: bool = False, fail_set: bool = False) -> None:
        self.d: dict[str, bytes] = {}
        self.ttls: dict[str, int | None] = {}
        self.fail_delete = fail_delete
        self.fail_set = fail_set

    async def get(self, key):
        return self.d.get(key)

    async def set(self, key, value, ttl=None, mode="always"):
        if self.fail_set and key.startswith("owner:tomb:"):
            raise ConnectionError("cache store down")
        self.d[key] = value
        self.ttls[key] = ttl
        return True

    async def delete(self, key):
        if self.fail_delete:
            raise ConnectionError("cache store down")
        self.d.pop(key, None)


class _TenantCheckpointer:
    """A checkpointer double with an ``org_id`` tenant column; threads live in ``db``."""

    tenant_column_names = ("org_id",)
    columns = parse_columns({"org_id": "text"})

    def __init__(self) -> None:
        self.db: dict[str, dict] = {}

    async def aget_thread_scope(self, thread_id):
        return self.db.get(thread_id)

    async def aget_thread_owner(self, thread_id):
        return (self.db.get(thread_id) or {}).get("user_id")


A = {"user_id": "u1", "org_id": "A"}
B = {"user_id": "u1", "org_id": "B"}


def _workers(cache=None, cp=None):
    cache = cache if cache is not None else _Cache()
    cp = cp if cp is not None else _TenantCheckpointer()
    w1 = OwnershipAuthorizationBackend(cp, cache=cache, server_thread_ids=False)
    w2 = OwnershipAuthorizationBackend(cp, cache=cache, server_thread_ids=False)
    return w1, w2, cp, cache


# ---------------------------------------------------------------------------
# A3: tombstone keyed on the deleter's full scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_user_id_in_another_tenant_cannot_take_a_just_deleted_id():
    """tomb.py: u1@A deletes T on W2; u1@B must not recreate T while W1 still caches u1@A."""
    w1, w2, cp, _ = _workers()
    cp.db["T"] = dict(A)
    assert await w1.authorize(A, "checkpointer", "read", "T") is True
    assert await w2.authorize(A, "checkpointer", "delete", "T") is True
    del cp.db["T"]
    await w2.evict("T", "u1", user=A)

    assert await w2.authorize(B, "graph", "invoke", "T") is False
    assert await w1.authorize(B, "graph", "invoke", "T") is False  # through the L2 tombstone
    # The deleter itself (same user_id and tenant) may recreate it at once, on any worker.
    assert await w2.authorize(A, "graph", "invoke", "T") is True
    assert await w1.authorize(A, "graph", "invoke", "T") is True


@pytest.mark.asyncio
async def test_tombstone_stores_the_deleter_scope_as_canonical_json():
    _, w2, cp, cache = _workers()
    cp.db["T"] = dict(A)
    assert await w2.authorize(A, "checkpointer", "delete", "T") is True
    await w2.evict("T", "u1", user=A)
    assert json.loads(cache.d["owner:tomb:T"]) == {"org_id": "A", "user_id": "u1"}


@pytest.mark.asyncio
async def test_bare_user_id_tombstone_blocks_everyone_with_tenant_columns():
    """A plain-owner tombstone (an older worker during a rolling deploy) exempts nobody."""
    w1, _, _, cache = _workers()
    cache.d["owner:tomb:T"] = b"u1"
    assert await w1.authorize(A, "graph", "invoke", "T") is False
    assert await w1.authorize(B, "graph", "invoke", "T") is False


@pytest.mark.asyncio
async def test_plain_owner_tombstones_unchanged_without_tenant_columns():
    """No tenant columns: the tombstone stays the plain user_id older releases read."""
    cache = _Cache()
    deleting = ThreadOwnershipResolver(AsyncMock(return_value=None), cache=cache)
    other = ThreadOwnershipResolver(AsyncMock(return_value=None), cache=cache)
    await deleting.evict("T", "alice")
    assert cache.d["owner:tomb:T"] == b"alice"
    assert await other.recently_deleted("T", "alice") is False
    assert await other.recently_deleted("T", "bob") is True
    cache.d["owner:tomb:U"] = b"alice"  # written by an older release
    assert await other.recently_deleted("U", "alice") is False
    assert await other.recently_deleted("U", {"user_id": "alice"}) is False


@pytest.mark.asyncio
async def test_resolver_exempts_only_a_full_scope_match():
    r = ThreadOwnershipResolver(AsyncMock(return_value=None), tenant_columns=("org_id",))
    await r.evict("T", {"user_id": "u1", "org_id": "A"})
    assert await r.recently_deleted("T", {"user_id": "u1", "org_id": "A"}) is False
    assert await r.recently_deleted("T", {"user_id": "u1", "org_id": "B"}) is True
    assert await r.recently_deleted("T", "u1") is True  # no tenant values: not a match
    assert await r.recently_deleted("T") is True


@pytest.mark.asyncio
async def test_deleter_from_another_tenant_gets_no_head_start():
    """Same user_id, other tenant (a ["user_id"] scope) deleting: the tombstone blocks all."""
    r = ThreadOwnershipResolver(
        AsyncMock(return_value={"user_id": "u1", "org_id": "A"}), tenant_columns=("org_id",)
    )
    await r.scope_of("T", ("user_id", "org_id"))
    await r.evict("T", {"user_id": "u1", "org_id": "B"})
    assert await r.recently_deleted("T", {"user_id": "u1", "org_id": "B"}) is True
    assert await r.recently_deleted("T", {"user_id": "u1", "org_id": "A"}) is True


# ---------------------------------------------------------------------------
# A4: failed L2 eviction is visible; short L2 TTL with tenant columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_l2_eviction_raises_and_warns(caplog):
    cache = _Cache()
    r = ThreadOwnershipResolver(
        AsyncMock(return_value={"user_id": "u1", "org_id": "A"}),
        cache=cache,
        tenant_columns=("org_id",),
    )
    await r.scope_of("T", ("user_id", "org_id"))
    cache.fail_delete = True
    with (
        caplog.at_level(logging.WARNING, logger="tenxgraph_api.authorization"),
        pytest.raises(OwnershipCacheEvictionError),
    ):
        await r.evict("T", {"user_id": "u1", "org_id": "A"})
    assert "did not reach the cache store" in caplog.text
    # L1 and the local tombstone are still handled before the error.
    assert "T" not in r._l1
    assert await r.recently_deleted("T", {"user_id": "u1", "org_id": "B"}) is True


@pytest.mark.asyncio
async def test_failed_tombstone_write_raises():
    r = ThreadOwnershipResolver(AsyncMock(return_value=None), cache=_Cache(fail_set=True))
    with pytest.raises(OwnershipCacheEvictionError):
        await r.evict("T", "alice")


@pytest.mark.asyncio
async def test_thread_delete_reports_a_failed_cache_eviction(monkeypatch):
    cp = _TenantCheckpointer()
    cp.db["T"] = dict(A)
    cache = _Cache()
    backend = OwnershipAuthorizationBackend(cp, cache=cache)
    assert await backend.authorize(A, "checkpointer", "delete", "T") is True
    cache.fail_delete = True

    container = MagicMock()
    container.try_get.return_value = backend
    monkeypatch.setattr(
        "tenxgraph_api.src.app.core.utils.container.current_container", lambda: container
    )
    telemetry = MagicMock()
    telemetry.try_get.return_value = None
    monkeypatch.setattr("injectq.InjectQ.get_instance", lambda: telemetry)
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: None))

    service = CheckpointerService.__new__(CheckpointerService)
    service.checkpointer = MagicMock(aclean_thread=AsyncMock(return_value=None))
    service.settings = MagicMock()
    res = await service.delete_thread({"thread_id": "T"}, dict(A), "T")
    assert res.success is False
    assert res.data["cleanup_failed"] == ["cache"]


@pytest.mark.asyncio
async def test_thread_delete_passes_the_deleter_scope(monkeypatch):
    """delete_thread hands the whole user to the built-in backend (A3 end to end)."""
    w1, w2, cp, _ = _workers()
    cp.db["T"] = dict(A)
    assert await w1.authorize(A, "checkpointer", "read", "T") is True
    assert await w2.authorize(A, "checkpointer", "delete", "T") is True

    container = MagicMock()
    container.try_get.return_value = w2
    monkeypatch.setattr(
        "tenxgraph_api.src.app.core.utils.container.current_container", lambda: container
    )
    telemetry = MagicMock()
    telemetry.try_get.return_value = None
    monkeypatch.setattr("injectq.InjectQ.get_instance", lambda: telemetry)
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: None))
    service = CheckpointerService.__new__(CheckpointerService)

    async def clean(_cfg):
        cp.db.pop("T", None)

    service.checkpointer = MagicMock(aclean_thread=clean)
    service.settings = MagicMock()
    res = await service.delete_thread({"thread_id": "T"}, dict(A), "T")
    assert res.success is True
    # The deleting worker (L1 evicted) and a fresh worker (shared tombstone only).
    w3 = OwnershipAuthorizationBackend(cp, cache=w1._cache, server_thread_ids=False)
    for worker in (w2, w3):
        assert await worker.authorize(B, "graph", "invoke", "T") is False
        assert await worker.authorize(A, "graph", "invoke", "T") is True


@pytest.mark.asyncio
async def test_custom_evict_without_user_keyword_still_works(monkeypatch):
    calls = []

    class Backend:
        def evict(self, thread_id, owner=None):
            calls.append((thread_id, owner))

    container = MagicMock()
    container.try_get.return_value = Backend()
    monkeypatch.setattr(
        "tenxgraph_api.src.app.core.utils.container.current_container", lambda: container
    )
    telemetry = MagicMock()
    telemetry.try_get.return_value = None
    monkeypatch.setattr("injectq.InjectQ.get_instance", lambda: telemetry)
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: None))
    service = CheckpointerService.__new__(CheckpointerService)
    service.checkpointer = MagicMock(aclean_thread=AsyncMock(return_value=None))
    service.settings = MagicMock()
    res = await service.delete_thread({"thread_id": "T"}, dict(A), "T")
    assert res.success is True
    assert calls == [("T", "u1")]


@pytest.mark.asyncio
async def test_l2_ttl_is_short_with_tenant_columns():
    lookup = AsyncMock(return_value={"user_id": "u1", "org_id": "A"})
    tenant_cache, plain_cache = _Cache(), _Cache()
    await ThreadOwnershipResolver(lookup, cache=tenant_cache, tenant_columns=("org_id",)).scope_of(
        "T", ("user_id", "org_id")
    )
    await ThreadOwnershipResolver(lookup, cache=plain_cache).scope_of("T")
    assert tenant_cache.ttls["owner:scope:T"] == TENANT_L2_TTL_SECONDS == 300
    assert plain_cache.ttls["owner:id:T"] == DEFAULT_L2_TTL_SECONDS
    explicit = _Cache()
    await ThreadOwnershipResolver(
        lookup, cache=explicit, tenant_columns=("org_id",), l2_ttl=42
    ).scope_of("T", ("user_id", "org_id"))
    assert explicit.ttls["owner:scope:T"] == 42


# ---------------------------------------------------------------------------
# A2: D6 tenant column check for the store bound from 10xgraph.json
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, tenant: dict | None = None) -> None:
        self.columns = parse_columns(tenant)
        self.tenant_column_names = tuple(tenant or ())


def _graph(checkpointer, store=None):
    return SimpleNamespace(_checkpointer=checkpointer, _store=store)


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setenv("MODE", "production")


@pytest.fixture
def development(monkeypatch):
    monkeypatch.setenv("MODE", "development")


def test_store_key_mismatch_refuses_in_production(production):
    from tenxgraph_api.src.app.loader import check_store_tenant_columns

    with pytest.raises(GraphError):
        check_store_tenant_columns(_graph(_TenantCheckpointer()), InjectQ(), _Store())


def test_store_key_mismatch_warns_in_development(development, caplog):
    from tenxgraph_api.src.app.loader import check_store_tenant_columns

    with caplog.at_level(logging.WARNING, logger="tenxgraph.graph"):
        check_store_tenant_columns(_graph(_TenantCheckpointer()), InjectQ(), _Store())
    assert "declares tenant columns" in caplog.text


def test_store_key_matching_columns_pass(production):
    from tenxgraph_api.src.app.loader import check_store_tenant_columns

    check_store_tenant_columns(_graph(_TenantCheckpointer()), InjectQ(), _Store({"org_id": "text"}))


def test_store_key_opt_out(production):
    from tenxgraph_api.src.app.loader import check_store_tenant_columns

    check_store_tenant_columns(
        _graph(_TenantCheckpointer()), InjectQ(), _Store(), allow_mismatch=True
    )


def test_store_key_checked_against_the_container_checkpointer(production):
    from tenxgraph_api.src.app.loader import check_store_tenant_columns

    container = InjectQ()
    container.bind_instance(BaseCheckpointer, _TenantCheckpointer(), allow_concrete=True)
    with pytest.raises(GraphError):
        check_store_tenant_columns(_graph(InMemoryCheckpointer()), container, _Store())


def test_store_compiled_into_the_graph_is_not_checked_twice(production):
    """compile() already checked it, with the developer's allow_tenant_mismatch."""
    from tenxgraph_api.src.app.loader import check_store_tenant_columns

    store = _Store()
    check_store_tenant_columns(_graph(_TenantCheckpointer(), store), InjectQ(), store)


def _config(store_path, mismatch=False):
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RunsConfig

    config = MagicMock(spec=GraphConfig)
    config.graph_path = "mod:graph"
    config.auth_config.return_value = None
    config.thread_name_generator = "none"
    config.id_generator = None
    config.authorization_path = None
    config.store_path = store_path
    config.store_allow_tenant_mismatch = mismatch
    config.cache_store = None
    config.checkpointer_path = None
    config.remote_tools = []
    config.runs = RunsConfig()
    return config


async def _attach(config, mode, monkeypatch):
    from tenxgraph_api.src.app.core.config import settings as settings_mod
    from tenxgraph_api.src.app.loader import attach_all_modules

    # Build (or reuse) the cached settings before MODE changes, so no production Settings
    # object is created here and cached for other tests.
    monkeypatch.setattr(settings_mod.get_settings(), "MODE", mode)
    monkeypatch.setenv("MODE", mode)  # core's tenant check reads the environment
    graph = _graph(_TenantCheckpointer())
    with (
        patch("tenxgraph_api.src.app.loader.load_graph", return_value=graph),
        patch("tenxgraph_api.src.app.loader.apply_id_generator"),
        patch("tenxgraph_api.src.app.loader.load_store", return_value=_Store()),
        patch("tenxgraph_api.src.app.core.config.media_settings.get_media_settings"),
    ):
        return await attach_all_modules(config, InjectQ())


@pytest.mark.asyncio
async def test_production_boot_refuses_a_mismatched_store_key(monkeypatch):
    with pytest.raises(GraphError):
        await _attach(_config("graph.deps:store"), "production", monkeypatch)


@pytest.mark.asyncio
async def test_development_boot_warns_on_a_mismatched_store_key(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="tenxgraph.graph"):
        await _attach(_config("graph.deps:store"), "development", monkeypatch)
    assert "declares tenant columns" in caplog.text


@pytest.mark.asyncio
async def test_production_boot_with_the_opt_out(monkeypatch):
    await _attach(_config("graph.deps:store", mismatch=True), "production", monkeypatch)


def test_graph_config_parses_the_opt_out(tmp_path):
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps({"agent": "g:app"}))
    assert GraphConfig(str(path)).store_allow_tenant_mismatch is False
    path.write_text(json.dumps({"agent": "g:app", "store_allow_tenant_mismatch": True}))
    assert GraphConfig(str(path)).store_allow_tenant_mismatch is True
    path.write_text(json.dumps({"agent": "g:app", "store_allow_tenant_mismatch": "maybe"}))
    with pytest.raises(ValueError, match="store_allow_tenant_mismatch"):
        _ = GraphConfig(str(path)).store_allow_tenant_mismatch


def test_config_editor_knows_the_opt_out():
    from tenxgraph_api.cli.config_editor.schema import KNOWN_TOP_LEVEL_KEYS, build_schema
    from tenxgraph_api.cli.config_editor.validation import validate_config

    assert "store_allow_tenant_mismatch" in KNOWN_TOP_LEVEL_KEYS
    paths = [
        field["path"]
        for section in build_schema()["sections"]
        for field in section.get("fields", [])
    ]
    assert ["store_allow_tenant_mismatch"] in paths
    issues = validate_config({"agent": "g:app", "store_allow_tenant_mismatch": "yes"})
    assert any(
        issue["path"] == "store_allow_tenant_mismatch" and issue["level"] == "error"
        for issue in issues
    )
    assert not [
        issue
        for issue in validate_config({"agent": "g:app", "store_allow_tenant_mismatch": True})
        if issue["path"] == "store_allow_tenant_mismatch"
    ]
