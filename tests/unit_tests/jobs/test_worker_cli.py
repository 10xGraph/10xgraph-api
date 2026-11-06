"""``10xgraph worker``: CLI wiring, config errors, and a SIGTERM drain in a real subprocess."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid

import pytest
from typer.testing import CliRunner

from tenxgraph_api.cli.main import app
from tenxgraph_api.jobs import enqueue
from tenxgraph_api.jobs.config.config import parse_worker_config
from tenxgraph_api.jobs.config.factory import build_runtime, close_runtime
from tenxgraph_api.jobs.core import registry
from tenxgraph_api.jobs.core.models import NewRun
from tenxgraph_api.jobs.core.runtime import JobsRuntime, use_runtime
from tenxgraph_api.jobs.store.memory import MemoryRunStore
from tenxgraph_api.jobs.transport.memory import MemoryTransport
from tenxgraph_api.jobs.worker.sweeper import sweep_once


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


@pytest.fixture(autouse=True)
def _keep_env(monkeypatch):
    """The command sets GRAPH_PATH (as `10xgraph dev` does); restore it after each test."""
    monkeypatch.delenv("GRAPH_PATH", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))


def write_project(tmp_path, worker: dict | None, tasks: str = "") -> tuple[str, str]:
    module = f"cli_jobs_{uuid.uuid4().hex[:8]}"
    (tmp_path / "graph.py").write_text(GRAPH)
    (tmp_path / f"{module}.py").write_text(tasks)
    data: dict = {"agent": "graph:app"}
    if worker is not None:
        data["worker"] = {"modules": module, **worker}
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps(data))
    return str(path), module


def test_help_lists_the_flags():
    result = CliRunner().invoke(app, ["worker", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--queue",
        "--concurrency",
        "--threads",
        "--processes",
        "--max-tasks-per-child",
        "--max-memory-per-child",
        "--reserve",
        "--no-scheduler",
        "--shutdown-timeout",
        "--metrics-port",
        "--health-port",
        "--health-host",
        "--config",
    ):
        assert flag in result.output


def test_health_host_defaults_to_loopback_and_passes_through(monkeypatch):
    from tenxgraph_api.cli.commands import worker as worker_cmd
    from tenxgraph_api.jobs.worker.service import WorkerOptions

    assert WorkerOptions().health_host == "127.0.0.1"
    seen: list = []

    def stop(self, config):
        raise RuntimeError("stop")

    real = WorkerOptions.__init__

    def capture(self, **kw):
        seen.append(kw.get("health_host"))
        real(self, **kw)

    monkeypatch.setattr(WorkerOptions, "__init__", capture)
    monkeypatch.setattr(worker_cmd.WorkerCommand, "_load_project", stop)
    CliRunner().invoke(app, ["worker", "--health-host", "0.0.0.0"])
    CliRunner().invoke(app, ["worker"])
    assert seen == ["0.0.0.0", "127.0.0.1"]


def test_no_worker_section_is_a_config_error(tmp_path, monkeypatch):
    path, _ = write_project(tmp_path, None)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["worker", "-c", path])
    assert result.exit_code == 2
    assert "worker" in result.output
    assert not (tmp_path / ".10xgraph").exists()


def test_bad_section_and_bad_reserve(tmp_path, monkeypatch):
    path, _ = write_project(tmp_path, {"backend": "local", "concurency": 1})
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["worker", "-c", path])
    assert result.exit_code == 2
    assert "unknown keys" in result.output
    result = CliRunner().invoke(app, ["worker", "-c", path, "--reserve", "urgent=2"])
    assert result.exit_code != 0
    assert "--reserve" in result.output


@pytest.mark.asyncio
async def test_concurrent_sweepers_recover_a_lost_run_once():
    clock_now = [1_800_000_000.0]
    store = MemoryRunStore(lambda: clock_now[0])
    runtime = JobsRuntime(store=store, transport=MemoryTransport())
    await store.create_run(
        NewRun(run_id="r1", task="t", identity={"user_id": "u"}, owner_user_id="u")
    )
    claimed = await store.claim("r1", worker_id="w", lease_ttl=5)
    assert claimed.run is not None
    clock_now[0] += 10
    stats = await asyncio.gather(*(sweep_once(runtime) for _ in range(4)))
    assert sum(s.lost for s in stats) == 1
    run = await store.get_run("r1")
    assert run.state.value == "queued" and run.lost_count == 1


TASKS = textwrap.dedent(
    """
    import asyncio

    from tenxgraph_api.jobs import task

    @task(name="{quick}")
    async def quick() -> str:
        return "done"

    @task(name="{slow}")
    async def slow() -> str:
        await asyncio.sleep(120)
        return "never"
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_drains_and_requeues(tmp_path):
    quick, slow = f"q_{uuid.uuid4().hex[:6]}", f"s_{uuid.uuid4().hex[:6]}"
    path, module = write_project(
        tmp_path, {"backend": "local", "lease_ttl": 10}, TASKS.format(quick=quick, slow=slow)
    )
    sys.path.insert(0, str(tmp_path))
    cfg = parse_worker_config(json.loads((tmp_path / "10xgraph.json").read_text())["worker"])

    async def enqueue_both():
        __import__(module)
        runtime = await build_runtime(cfg, base_dir=tmp_path)
        with use_runtime(runtime):
            a = await enqueue(quick, identity={"user_id": "u1"})
            b = await enqueue(slow, identity={"user_id": "u1"})
        await close_runtime(runtime)
        return a.id, b.id

    async def read(run_id):
        runtime = await build_runtime(cfg, base_dir=tmp_path)
        try:
            return await runtime.store.get_run(run_id)
        finally:
            await close_runtime(runtime)

    try:
        quick_id, slow_id = asyncio.run(enqueue_both())
    finally:
        for name in (quick, slow):
            registry.unregister(name)
        sys.modules.pop(module, None)
        sys.path.remove(str(tmp_path))

    env = {**os.environ, "MODE": "development", "CI": "1", "PYTHONUNBUFFERED": "1"}
    env.pop("GRAPH_PATH", None)
    proc = subprocess.Popen(  # - our own CLI
        [
            sys.executable,
            "-m",
            "tenxgraph_api.cli.main",
            "--no-animation",
            "worker",
            "-c",
            path,
            "--shutdown-timeout",
            "0.5",
            "--health-port",
            "0",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            assert proc.poll() is None, proc.communicate()[0]
            slow_run = asyncio.run(read(slow_id))
            quick_run = asyncio.run(read(quick_id))
            if slow_run.state.value == "running" and quick_run.state.value == "succeeded":
                break
            assert time.monotonic() < deadline, "worker did not start the runs"
            time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, output
    run = asyncio.run(read(slow_id))
    assert run.state.value == "queued", output
    assert run.error["code"] == "interrupted"
    assert run.failure_count == 0  # no retry used
    assert "task run started" in output and f"run_id={slow_id}" in output
    assert "draining" in output
