"""The memory_cleanup config, its boot check and the built-in scheduled task."""

import importlib
import json
import sys
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from tenxgraph.qa.testing.in_memory_store import InMemoryStore
from tenxgraph.storage.store import BaseStore

from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.loader import check_memory_cleanup


def make_config(tmp_path, **extra):
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps({"agent": "m:app", **extra}))
    return GraphConfig(str(path))


def test_default_is_off(tmp_path):
    cfg = make_config(tmp_path).memory_cleanup
    assert cfg.enabled is False
    assert (cfg.interval_seconds, cfg.batch_size) == (3600, 1000)


def test_parses_values(tmp_path):
    raw = {"enabled": True, "interval_seconds": 60, "batch_size": 50}
    cfg = make_config(tmp_path, memory_cleanup=raw).memory_cleanup
    assert (cfg.enabled, cfg.interval_seconds, cfg.batch_size) == (True, 60, 50)


@pytest.mark.parametrize(
    "raw",
    [
        {"enable": True},
        {"enabled": "yes"},
        {"interval_seconds": 0},
        {"batch_size": 0},
        {"interval_seconds": 1.5},
    ],
)
def test_rejects_bad_values(tmp_path, raw):
    with pytest.raises(ValidationError):
        _ = make_config(tmp_path, memory_cleanup=raw).memory_cleanup


def test_off_never_checks(tmp_path):
    check_memory_cleanup(make_config(tmp_path), None)


def test_enabled_with_supporting_store_boots(tmp_path):
    cfg = make_config(tmp_path, memory_cleanup={"enabled": True}, worker={})
    check_memory_cleanup(cfg, InMemoryStore())


def test_enabled_refuses_unsupporting_store(tmp_path):
    class Plain:
        supports_provenance = False

    cfg = make_config(tmp_path, memory_cleanup={"enabled": True}, worker={})
    with pytest.raises(ValueError, match=r"Plain\) cannot delete expired"):
        check_memory_cleanup(cfg, Plain())
    with pytest.raises(ValueError, match="no store"):
        check_memory_cleanup(cfg, None)


def test_enabled_refuses_without_worker(tmp_path):
    cfg = make_config(tmp_path, memory_cleanup={"enabled": True})
    with pytest.raises(ValueError, match='no "worker" key'):
        check_memory_cleanup(cfg, InMemoryStore())


@pytest.fixture
def cleanup_module(tmp_path, monkeypatch):
    """Import the task module against a config that turns cleanup on, then undo it."""
    from tenxgraph_api.jobs import worker

    cfg = make_config(
        tmp_path,
        memory_cleanup={"enabled": True, "interval_seconds": 120, "batch_size": 7},
    )
    monkeypatch.setattr(worker, "graph_config", cfg)
    monkeypatch.delitem(sys.modules, "tenxgraph_api.jobs.memory_cleanup", raising=False)
    module = importlib.import_module("tenxgraph_api.jobs.memory_cleanup")
    yield module
    worker.broker.local_task_registry.pop(module.TASK_NAME, None)
    worker.broker.global_task_registry.pop(module.TASK_NAME, None)
    sys.modules.pop("tenxgraph_api.jobs.memory_cleanup", None)


def test_task_is_scheduled_by_interval(cleanup_module):
    task = cleanup_module.delete_expired_memories
    assert task.labels["schedule"] == [{"interval": 120}]


@pytest.mark.asyncio
async def test_task_calls_adelete_expired_with_batch_size(cleanup_module):
    store = AsyncMock(spec=BaseStore)
    store.adelete_expired.return_value = 12
    deleted = await cleanup_module.delete_expired_memories.original_func(store)
    assert deleted == 12
    store.adelete_expired.assert_awaited_once_with(batch_size=7)
