"""``10xgraph worker`` / ``scheduler``: the argv built for TaskIQ and the refusals."""

import importlib
import json
import os
import sys

import pytest

from tenxgraph_api.cli.commands import worker as cmd


# Same waits as the build image (`taskiq worker` in the Dockerfile, compose and k8s).
WORKER_DEFAULTS = ["--wait-tasks-timeout", "600", "--shutdown-timeout", "60"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project dir with a worker config; restores cwd, sys.path and GRAPH_PATH afterwards."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv("GRAPH_PATH", raising=False)
    data = {"agent": "a:b", "worker": {"broker": {"type": "memory"}, "tasks": ["app.tasks"]}}
    (tmp_path / "10xgraph.json").write_text(json.dumps(data))
    return tmp_path


@pytest.fixture
def redis_project(project):
    data = {"agent": "a:b", "worker": {"broker": {"type": "redis_stream"}, "tasks": ["app.tasks"]}}
    (project / "10xgraph.json").write_text(json.dumps(data))
    return project


def run(command, monkeypatch, **kwargs):
    """Execute ``command`` with TaskIQ's entry point patched; returns (status, argv or None)."""
    seen = []
    monkeypatch.setattr(command, "run_taskiq", lambda argv: seen.append(argv))
    status = command.execute(**kwargs)
    return status, (seen[0] if seen else None)


def test_worker_argv_and_environment(project, monkeypatch):
    status, argv = run(cmd.WorkerCommand(), monkeypatch, extra=["--workers", "4"])
    assert status == 0
    assert argv == [
        "tenxgraph_api.jobs.worker:broker",
        "app.tasks",
        "--workers",
        "4",
        *WORKER_DEFAULTS,
    ]
    assert os.environ["GRAPH_PATH"] == str(project / "10xgraph.json")
    assert sys.path[0] == str(project)
    assert os.getcwd() == str(project)


def test_scheduler_adds_defaults(redis_project, monkeypatch):
    _, argv = run(cmd.SchedulerCommand(), monkeypatch)
    assert argv == [
        "tenxgraph_api.jobs.scheduler:scheduler",
        "app.tasks",
        "--update-interval",
        "10",
        "--skip-first-run",
        "--send-timeout",
        "30",
    ]


@pytest.mark.parametrize(
    "given",
    [
        ["--update-interval", "3"],
        ["--update-interval=3"],
        ["--send-timeout", "5"],
        ["--skip-first-run"],
    ],
)
def test_scheduler_keeps_user_flags(redis_project, monkeypatch, given):
    _, argv = run(cmd.SchedulerCommand(), monkeypatch, extra=given)
    assert argv[2 : 2 + len(given)] == given
    flag = given[0].split("=")[0]
    assert sum(arg.startswith(flag) for arg in argv) == 1


def test_scheduler_refuses_memory_broker(project, monkeypatch):
    status, argv = run(cmd.SchedulerCommand(), monkeypatch)
    assert status != 0
    assert argv is None


def test_globs_become_fs_discover_patterns(project, monkeypatch):
    tasks = ["app.tasks", "jobs/*.py", "tasks.py", "other.mod"]
    command = cmd.WorkerCommand()
    assert command.build_argv(tasks, []) == [
        "tenxgraph_api.jobs.worker:broker",
        "app.tasks",
        "other.mod",
        "--fs-discover",
        "--tasks-pattern",
        "jobs/*.py",
        "--tasks-pattern",
        "tasks.py",
        *WORKER_DEFAULTS,
    ]


@pytest.mark.parametrize(
    "given",
    [["--wait-tasks-timeout", "30"], ["--wait-tasks-timeout=30"], ["--shutdown-timeout", "5"]],
)
def test_worker_keeps_user_shutdown_flags(project, monkeypatch, given):
    _, argv = run(cmd.WorkerCommand(), monkeypatch, extra=given)
    assert argv[2 : 2 + len(given)] == given
    flag = given[0].split("=")[0]
    assert sum(arg.startswith(flag) for arg in argv) == 1


@pytest.mark.parametrize(
    "flag", ["--use-process-pool", "--max-process-pool-processes=2", "--use-proc"]
)
def test_worker_refuses_process_pool(project, monkeypatch, flag):
    status, argv = run(cmd.WorkerCommand(), monkeypatch, extra=[flag])
    assert status != 0
    assert argv is None


def test_worker_allows_similar_flags(project, monkeypatch):
    status, argv = run(cmd.WorkerCommand(), monkeypatch, extra=["--max-threadpool-threads", "2"])
    assert status == 0
    assert argv[2:4] == ["--max-threadpool-threads", "2"]


def test_missing_config_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    status, argv = run(cmd.WorkerCommand(), monkeypatch)
    assert status != 0
    assert argv is None


def test_missing_worker_key_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "10xgraph.json").write_text(json.dumps({"agent": "a:b"}))
    status, argv = run(cmd.SchedulerCommand(), monkeypatch)
    assert status != 0
    assert argv is None


def test_scheduler_sources_for_memory_broker(project, monkeypatch):
    monkeypatch.setenv("GRAPH_PATH", str(project / "10xgraph.json"))
    for name in ("tenxgraph_api.jobs.worker", "tenxgraph_api.jobs.scheduler"):
        sys.modules.pop(name, None)
    module = importlib.import_module("tenxgraph_api.jobs.scheduler")
    assert [type(s).__name__ for s in module.scheduler.sources] == ["LabelScheduleSource"]
    assert module.scheduler.broker is module.broker
