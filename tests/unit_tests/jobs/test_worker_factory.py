"""Store / transport factory, own-storage rules, and the API side (enqueue by name)."""

from __future__ import annotations

import json
import sys
import textwrap
import uuid

import pytest

from tenxgraph_api.jobs import enqueue, registry
from tenxgraph_api.jobs.config import WorkerConfigError, parse_worker_config
from tenxgraph_api.jobs.errors import JobsNotConfigured
from tenxgraph_api.jobs.factory import (
    build_runtime,
    build_store,
    build_transport,
    close_runtime,
    import_task_modules,
    load_hooks,
    start_api_jobs,
    stop_api_jobs,
)
from tenxgraph_api.jobs.hooks import TaskLifecycleHook
from tenxgraph_api.jobs.runtime import get_runtime
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


pytestmark = pytest.mark.asyncio


class AuditHook(TaskLifecycleHook):
    pass


AUDIT_INSTANCE = AuditHook()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A tiny project: a task module with a unique name, and a 10xgraph.json writer."""
    name = f"jobs_mod_{uuid.uuid4().hex[:8]}"
    task_name = f"t_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{name}.py").write_text(
        textwrap.dedent(
            f"""
            from tenxgraph_api.jobs import task

            @task(name="{task_name}")
            async def work(x: int) -> int:
                return x * 2
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    def write(worker):
        data = {"agent": "graph:app"}
        if worker is not None:
            data["worker"] = worker
        path = tmp_path / "10xgraph.json"
        path.write_text(json.dumps(data))
        return GraphConfig(str(path))

    yield tmp_path, name, task_name, write
    registry.unregister(task_name)
    sys.modules.pop(name, None)


async def test_sqlite_local_runtime_creates_its_file_next_to_the_config(tmp_path):
    cfg = parse_worker_config({"backend": "local"})
    runtime = await build_runtime(cfg, base_dir=tmp_path, allow_memory=False)
    try:
        assert (tmp_path / ".10xgraph" / "jobs.db").exists()
        assert type(runtime.store).__name__ == "SqliteRunStore"
        assert type(runtime.transport).__name__ == "SqliteTransport"
        assert runtime.lease_ttl == 60
        assert runtime.result_ttl == 604800
    finally:
        await close_runtime(runtime)


async def test_memory_is_refused_for_servers_and_workers():
    cfg = parse_worker_config({"store": {"type": "memory"}, "transport": {"type": "memory"}})
    with pytest.raises(WorkerConfigError, match='"backend": "local"'):
        await build_runtime(cfg, allow_memory=False)
    runtime = await build_runtime(cfg)  # scripts and tests may use it
    await close_runtime(runtime)


def test_postgres_store_uses_its_own_dsn_and_a_small_pool(monkeypatch):
    monkeypatch.setenv("JOBS_DSN", "postgresql://u:p@db/jobs")
    cfg = parse_worker_config(
        {
            "store": {"type": "postgres", "url": "${JOBS_DSN}", "schema": "jobs", "auto_migrate": False},
            "transport": {"type": "redis", "url": "redis://localhost:6379/0"},
        }
    )
    store = build_store(cfg)
    assert store._dsn == "postgresql://u:p@db/jobs"
    assert store.schema == "jobs"
    assert store._pool_config["max_size"] == 5
    assert store._auto_migrate is False


def test_redis_uses_the_jobs_database_and_the_worker_id_as_consumer():
    cfg = parse_worker_config(
        {
            "store": {"type": "redis", "url": "redis://localhost:6379/0"},
            "transport": {"type": "redis", "url": "redis://localhost:6379/0"},
            "redis_db": 3,
        }
    )
    store = build_store(cfg)
    transport = build_transport(cfg, consumer="w-1")
    assert store._db == 3
    assert transport._db == 3
    assert transport.consumer == "w-1"
    default = parse_worker_config(
        {
            "store": {"type": "redis", "url": "redis://h"},
            "transport": {"type": "redis", "url": "redis://h"},
        }
    )
    assert build_store(default)._db == 1


def test_unset_url_variable_is_a_config_error(monkeypatch):
    monkeypatch.delenv("NOPE_URL", raising=False)
    cfg = parse_worker_config(
        {"store": {"type": "redis", "url": "${NOPE_URL}"}, "transport": {"type": "redis", "url": "redis://h"}}
    )
    with pytest.raises(WorkerConfigError, match="NOPE_URL"):
        build_store(cfg)


def test_hooks_and_modules():
    hooks = load_hooks([f"{__name__}:AuditHook", f"{__name__}:AUDIT_INSTANCE"])
    assert isinstance(hooks[0], AuditHook) and hooks[1] is AUDIT_INSTANCE
    with pytest.raises(WorkerConfigError, match="cannot load"):
        load_hooks([f"{__name__}:Missing"])
    with pytest.raises(WorkerConfigError, match="cannot import 'no_such_jobs_module'"):
        import_task_modules(["no_such_jobs_module"])


async def test_no_worker_section_creates_nothing(project, monkeypatch):
    tmp_path, _name, _task, write = project
    gc = write(None)
    assert await start_api_jobs(gc, base_dir=tmp_path) is None
    assert not (tmp_path / ".10xgraph").exists()
    with pytest.raises(JobsNotConfigured):
        get_runtime()
    # The API lifespan helper does the same.
    from tenxgraph_api.src.app import main

    monkeypatch.setattr(main, "graph_config", gc)
    monkeypatch.setattr(main, "graph_path", str(tmp_path / "10xgraph.json"))
    assert await main._start_jobs() is None
    assert not (tmp_path / ".10xgraph").exists()


async def test_api_enqueue_by_name_with_a_worker_section(project, monkeypatch):
    tmp_path, name, task_name, write = project
    gc = write({"modules": name, "backend": "local"})
    from tenxgraph_api.src.app import main

    monkeypatch.setattr(main, "graph_config", gc)
    monkeypatch.setattr(main, "graph_path", str(tmp_path / "10xgraph.json"))
    runtime = await main._start_jobs()
    try:
        assert get_runtime() is runtime
        assert task_name in registry.all_tasks()
        handle = await enqueue(task_name, {"x": 21}, identity={"user_id": "u1"})
        run = await handle.status()
        assert run.state.value == "queued"
        assert run.owner_user_id == "u1"
        assert await runtime.transport.consume([run.queue], max_messages=5)  # published
    finally:
        await main._stop_jobs(runtime)
    with pytest.raises(JobsNotConfigured):
        get_runtime()


async def test_stop_api_jobs_none_is_a_noop():
    await stop_api_jobs(None)
