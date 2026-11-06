"""``10xgraph tasks``: enqueue, runs, run, cancel, migrate, workers, fire (SQLite backend)."""

from __future__ import annotations

import json
import sys
import textwrap
import uuid

import pytest
from typer.testing import CliRunner

from tenxgraph_api.cli.main import app
from tenxgraph_api.jobs.core import registry


GRAPH = textwrap.dedent(
    """
    from tenxgraph.core.graph import StateGraph
    from tenxgraph.core.state import Message
    from tenxgraph.storage.checkpointer import InMemoryCheckpointer
    from tenxgraph.utils.constants import END


    def node(state, config):
        return Message.text_message("ok", role="assistant")


    g = StateGraph()
    g.add_node("A", node)
    g.add_edge("A", END)
    g.set_entry_point("A")
    app = g.compile(checkpointer=InMemoryCheckpointer())
    """
)

TASKS = textwrap.dedent(
    """
    from tenxgraph_api.jobs import task


    @task(name="{name}")
    async def add(a: int, b: int = 1) -> int:
        return a + b
    """
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("GRAPH_PATH", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    with registry.isolated_registry():
        yield


class Project:
    def __init__(self, tmp_path, worker: dict | None = ...):
        self.tmp_path = tmp_path
        self.task = f"add_{uuid.uuid4().hex[:8]}"
        module = f"tasks_cli_{uuid.uuid4().hex[:8]}"
        (tmp_path / "graph.py").write_text(GRAPH)
        (tmp_path / f"{module}.py").write_text(TASKS.format(name=self.task))
        data: dict = {"agent": "graph:app"}
        if worker is not None:
            data["worker"] = (
                {
                    "modules": module,
                    "backend": "local",
                    "system_identities": {"cron": {"user_id": "__system__:cron"}},
                }
                if worker is ...
                else {"modules": module, **worker}
            )
        self.config = tmp_path / "10xgraph.json"
        self.config.write_text(json.dumps(data))

    def run(self, *args: str, root: tuple[str, ...] = ()):
        return CliRunner().invoke(
            app, [*root, "tasks", *args, "-c", str(self.config)], env={"COLUMNS": "250"}
        )

    def enqueue(self, *extra: str):
        return self.run(
            "enqueue", self.task, "--kwargs", '{"a": 2, "b": 3}', "--identity", "cron", *extra
        )

    def json_events(self, result) -> list[dict]:
        return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


@pytest.fixture
def project(tmp_path):
    return Project(tmp_path)


def run_id_of(project: Project) -> str:
    result = project.enqueue()
    assert result.exit_code == 0, result.output
    listing = project.run("runs", root=("--json",))
    (event,) = project.json_events(listing)
    return event["data"]["items"][0]["run_id"]


def test_enqueue_then_runs_shows_it(project):
    result = project.enqueue()
    assert result.exit_code == 0, result.output
    assert "queued" in result.output
    listing = project.run("runs")
    assert listing.exit_code == 0, listing.output
    assert project.task in listing.output
    assert "queued" in listing.output


def test_runs_filters_and_json(project):
    project.enqueue()
    result = project.run("runs", "--state", "queued", "--task", project.task, root=("--json",))
    assert result.exit_code == 0, result.output
    (event,) = project.json_events(result)
    items = event["data"]["items"]
    assert [i["task"] for i in items] == [project.task]
    assert items[0]["owner_user_id"] == "__system__:cron"
    assert items[0]["kwargs"] == {"a": 2, "b": 3}
    empty = project.run("runs", "--state", "failed", root=("--json",))
    assert project.json_events(empty)[0]["data"]["items"] == []


def test_runs_invalid_state(project):
    project.enqueue()
    result = project.run("runs", "--state", "bogus")
    assert result.exit_code == 3
    assert "--state" in result.output


def test_enqueue_as_user(project):
    result = project.run("enqueue", project.task, "--kwargs", '{"a": 1}', "--user", "u1")
    assert result.exit_code == 0, result.output
    listing = project.run("runs", root=("--json",))
    items = project.json_events(listing)[0]["data"]["items"]
    assert items[0]["owner_user_id"] == "u1"


def test_enqueue_priority_and_key(project):
    result = project.enqueue("--priority", "high", "--key", "k1")
    assert result.exit_code == 0, result.output
    items = project.json_events(project.run("runs", root=("--json",)))[0]["data"]["items"]
    assert items[0]["priority"] == "high"
    assert items[0]["idempotency_key"] == "k1"


def test_enqueue_delay_keeps_it_queued(project):
    assert project.enqueue("--delay", "60").exit_code == 0
    assert "queued" in project.run("runs").output


def test_enqueue_requires_one_identity(project):
    neither = project.run("enqueue", project.task)
    assert neither.exit_code == 3
    both = project.run("enqueue", project.task, "--identity", "cron", "--user", "u1")
    assert both.exit_code == 3


def test_enqueue_unknown_identity(project):
    result = project.run("enqueue", project.task, "--identity", "nope")
    assert result.exit_code == 3
    assert "cron" in result.output


def test_enqueue_unknown_task(project):
    result = project.run("enqueue", "missing_task", "--identity", "cron")
    assert result.exit_code == 3
    assert "no task named" in result.output
    assert project.task in result.output


def test_enqueue_bad_kwargs(project):
    bad_json = project.run("enqueue", project.task, "--kwargs", "{oops", "--identity", "cron")
    assert bad_json.exit_code == 3
    not_object = project.run("enqueue", project.task, "--kwargs", "[1]", "--identity", "cron")
    assert not_object.exit_code == 3
    wrong_type = project.run(
        "enqueue", project.task, "--kwargs", '{"a": "x"}', "--identity", "cron"
    )
    assert wrong_type.exit_code == 3


def test_enqueue_bad_priority(project):
    assert project.enqueue("--priority", "urgent").exit_code == 3


def test_run_detail(project):
    run_id = run_id_of(project)
    result = project.run("run", run_id)
    assert result.exit_code == 0, result.output
    assert run_id in result.output
    assert project.task in result.output
    as_json = project.run("run", run_id, root=("--json",))
    (event,) = project.json_events(as_json)
    assert event["data"]["run"]["run_id"] == run_id
    assert isinstance(event["data"]["events"], list)


def test_run_not_found(project):
    project.enqueue()
    result = project.run("run", "does-not-exist")
    assert result.exit_code == 1
    assert "not found" in result.output


def test_cancel(project):
    run_id = run_id_of(project)
    result = project.run("cancel", run_id)
    assert result.exit_code == 0, result.output
    assert "cancelled" in result.output
    detail = project.json_events(project.run("run", run_id, root=("--json",)))[0]
    assert detail["data"]["run"]["state"] == "cancelled"
    again = project.run("cancel", run_id)
    assert again.exit_code == 0
    assert "cancelled" in again.output


def test_cancel_not_found(project):
    project.enqueue()
    assert project.run("cancel", "nope").exit_code == 1


def test_reads_do_not_create_the_store(project, tmp_path):
    for args in (("runs",), ("run", "x"), ("cancel", "x")):
        result = project.run(*args)
        assert result.exit_code == 1, result.output
        assert "store not initialised, run 10xgraph tasks migrate" in result.output
    assert not (tmp_path / ".10xgraph").exists()


def test_migrate_check_and_migrate(project, tmp_path):
    check = project.run("migrate", "--check")
    assert check.exit_code == 1
    assert not (tmp_path / ".10xgraph").exists()
    done = project.run("migrate")
    assert done.exit_code == 0, done.output
    assert (tmp_path / ".10xgraph" / "jobs.db").exists()
    again = project.run("migrate", "--check", root=("--json",))
    assert again.exit_code == 0, again.output
    (event,) = project.json_events(again)
    assert event["data"]["up_to_date"] is True
    assert project.run("runs").exit_code == 0


def test_no_worker_section(tmp_path):
    project = Project(tmp_path, worker=None)
    for args in (("runs",), ("migrate",), ("enqueue", "x", "--identity", "a"), ("workers",)):
        result = project.run(*args)
        assert result.exit_code == 2, result.output
        assert 'No "worker" section' in result.output


def test_workers_not_available(project):
    result = project.run("workers")
    assert result.exit_code == 0
    assert "Sprint 4" in result.output


def test_fire_not_available(project):
    result = project.run("fire", "nightly")
    assert result.exit_code == 2
    assert "Sprint 4" in result.output
