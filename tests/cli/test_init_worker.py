"""`10xgraph init` Full mode background worker: question, flag, config, template files."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tenxgraph_api.cli.commands.init import InitCommand
from tenxgraph_api.cli.commands.init_context import (
    build_config,
    build_context,
    template_flags,
    template_vars,
)
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.cli.exceptions import ValidationError
from tenxgraph_api.jobs.config import WorkerConfig

from .test_init_prod import _FULL, _run_prompts


BROKERS = {
    "redis": ("redis_stream", "${REDIS_URL}", "jobs"),
    "rabbitmq": ("rabbitmq", "${BROKER_URL}", "jobs-rabbitmq"),
    "kafka": ("kafka", "${BROKER_URL}", "jobs-kafka"),
}


def _full(**options) -> dict:
    return build_context(path=".", agent_name="demo", mode="full", **options)


def _init(path: Path, **options) -> int:
    return InitCommand(OutputFormatter(stream=io.StringIO())).execute(
        path=str(path), agent_name="Demo", non_interactive=True, mode="full", **options
    )


# --- prompts -------------------------------------------------------------------------


def test_full_prompt_asks_worker_after_auth_and_defaults_to_none(monkeypatch) -> None:
    context, scripted, _ = _run_prompts(monkeypatch, _FULL)
    assert scripted.asked[-3:] == [
        "How should the API be secured?",
        "Background tasks?",
        "Which extras?",
    ]
    assert context is not None and context["worker"] == "none"


def test_full_prompt_picks_a_worker(monkeypatch) -> None:
    context, _, _ = _run_prompts(monkeypatch, {**_FULL, "Background tasks?": "rabbitmq"})
    assert context is not None
    assert context["worker"] == "rabbitmq" and context["cache_store"] == "redis"


def test_cancel_at_the_worker_question_returns_none(monkeypatch) -> None:
    context, _, _ = _run_prompts(monkeypatch, {**_FULL, "Background tasks?": None})
    assert context is None


def test_quick_prompt_does_not_ask_about_workers(monkeypatch) -> None:
    context, scripted, _ = _run_prompts(monkeypatch)
    assert "Background tasks?" not in scripted.asked
    assert context is not None and context["worker"] == "none"


# --- flag ----------------------------------------------------------------------------


def test_worker_flag_in_quick_mode_is_a_full_only_error() -> None:
    with pytest.raises(ValidationError, match="--worker requires --mode full"):
        build_context(path=".", agent_name="demo", mode="quick", worker="redis")
    assert (
        build_context(path=".", agent_name="demo", mode="quick", worker="none")["worker"] == "none"
    )


def test_invalid_worker_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Invalid worker"):
        _full(worker="sqs")


def test_worker_with_memory_cache_store_is_an_error() -> None:
    with pytest.raises(ValidationError, match="cache-store redis"):
        _full(worker="redis", cache_store="memory")


def test_worker_forces_the_redis_cache_store() -> None:
    context = _full(worker="kafka", storage="memory")
    assert context["cache_store"] == "redis"
    assert template_flags(context)["redis"]


# --- config, flags, extras -----------------------------------------------------------


@pytest.mark.parametrize("worker", list(BROKERS))
def test_build_config_has_the_worker_section(worker: str) -> None:
    broker_type, url, _ = BROKERS[worker]
    config = build_config(_full(worker=worker), True)
    assert config["worker"] == {
        "broker": {"type": broker_type, "url": url},
        "tasks": ["worker.tasks"],
    }
    WorkerConfig.model_validate(config["worker"])


def test_no_worker_section_for_none() -> None:
    assert "worker" not in build_config(_full(), True)
    assert "worker" not in build_config(_full(worker="none"), True)


@pytest.mark.parametrize("worker", list(BROKERS))
def test_pyproject_extra_and_flags(worker: str) -> None:
    context = _full(worker=worker)
    flags = template_flags(context)
    assert flags["worker"] and flags["broker_url"] is (worker != "redis")
    assert f"{BROKERS[worker][2]}" in template_vars(context)["api_requirement"].split("[")[1]


def test_no_worker_flags_by_default() -> None:
    flags = template_flags(_full())
    assert not flags["worker"] and not flags["broker_url"]
    assert "jobs" not in template_vars(_full())["api_requirement"]


# --- scaffold ------------------------------------------------------------------------


def test_worker_files_only_when_picked(tmp_path: Path) -> None:
    assert _init(tmp_path / "off") == 0
    assert not (tmp_path / "off" / "worker").exists()
    assert _init(tmp_path / "on", worker="redis") == 0
    assert (tmp_path / "on" / "worker" / "__init__.py").is_file()
    assert (tmp_path / "on" / "worker" / "tasks.py").is_file()


def test_dry_run_lists_worker_files(tmp_path: Path) -> None:
    stream = io.StringIO()
    code = InitCommand(OutputFormatter(stream=stream)).execute(
        path=str(tmp_path / "p"),
        agent_name="Demo",
        non_interactive=True,
        mode="full",
        worker="redis",
        dry_run=True,
    )
    assert code == 0 and "tasks.py" in stream.getvalue()
    assert not (tmp_path / "p").exists()


@pytest.mark.parametrize(
    ("worker", "expected"),
    [
        ("redis", None),
        ("rabbitmq", 'BROKER_URL="amqp://guest:guest@localhost:5672/"'),
        ("kafka", 'BROKER_URL="localhost:9092"'),
    ],
)
def test_env_example_per_broker(tmp_path: Path, worker: str, expected: str | None) -> None:
    assert _init(tmp_path, worker=worker) == 0
    env = (tmp_path / ".env.example").read_text()
    assert "REDIS_URL=" in env
    if expected is None:
        assert "BROKER_URL" not in env
    else:
        assert expected in env


def test_generated_project_files_are_valid(tmp_path: Path) -> None:
    assert _init(tmp_path, worker="kafka") == 0
    tasks = (tmp_path / "worker" / "tasks.py").read_text()
    compile(tasks, "tasks.py", "exec")
    assert "$" not in tasks
    config = json.loads((tmp_path / "10xgraph.json").read_text())
    worker = WorkerConfig.model_validate(config["worker"])
    assert worker.broker.type == "kafka" and worker.tasks == ["worker.tasks"]
    assert "jobs-kafka" in (tmp_path / "pyproject.toml").read_text()
