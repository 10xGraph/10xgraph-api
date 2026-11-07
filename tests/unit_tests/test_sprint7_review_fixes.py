"""Regression test for the Sprint 7 store review finding A2.

A2: the D6 tenant column check runs for the store bound from ``10xgraph.json``.
"""

# ruff: noqa: S101, PLR2004

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from injectq import InjectQ
from tenxgraph.core.exceptions import GraphError
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.storage.columns import parse_columns


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
    config.data = {}
    config.thread_name_generator = "none"
    config.id_generator = None
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
