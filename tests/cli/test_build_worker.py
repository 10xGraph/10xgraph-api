"""`10xgraph build`: worker and scheduler services, and the questions."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from tenxgraph_api.cli.commands.build import BuildCommand
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.cli.templates.defaults import (
    GRACEFUL_TIMEOUT_SECONDS,
    SHUTDOWN_KILL_MARGIN_SECONDS,
    generate_docker_compose_content,
    generate_k8s_manifest_content,
)


def _config(broker: str | None = "redis_stream", tasks=("app.tasks",)) -> dict:
    cfg: dict = {"agent": "graph.react:app"}
    if broker:
        cfg["worker"] = {"broker": {"type": broker}, "tasks": list(tasks)}
    return cfg


class FakePrompts:
    """Answers questions in order; ``None`` is a cancel."""

    interactive = True

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[str] = []

    def _next(self, message: str):
        self.asked.append(message)
        return self.answers.pop(0)

    def select(self, message, *args, **kw):
        return self._next(message)

    confirm = select


def _run(tmp_path: Path, monkeypatch, config=None, prompts=None, **options):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    if config is not None:
        (tmp_path / "10xgraph.json").write_text(json.dumps(config), encoding="utf-8")
    stream = io.StringIO()
    output = OutputFormatter(stream=stream)
    if prompts is not None:
        output.prompts = lambda **_: prompts
    options.setdefault("non_interactive", prompts is None)
    code = BuildCommand(output).execute(**options)
    return code, stream.getvalue()


def _compose(tmp_path: Path) -> dict:
    return yaml.safe_load((tmp_path / "docker-compose.yml").read_text(encoding="utf-8"))


def _k8s(tmp_path: Path) -> list[dict]:
    return list(yaml.safe_load_all((tmp_path / "k8s.yaml").read_text(encoding="utf-8")))


def test_compose_with_worker_and_scheduler(tmp_path, monkeypatch):
    code, _ = _run(
        tmp_path, monkeypatch, _config(), docker_compose=True, worker=True, scheduler=True
    )
    assert code == 0
    services = _compose(tmp_path)["services"]
    assert list(services) == ["10xgraph-api", "worker", "scheduler"]
    api, worker, scheduler = services.values()
    for key in ("build", "env_file", "environment", "read_only", "tmpfs", "cap_drop", "restart"):
        assert worker[key] == api[key] == scheduler[key]
    assert worker["stop_grace_period"] == api["stop_grace_period"]
    assert "ports" not in worker
    assert "volumes" not in worker
    assert worker["healthcheck"] == {"disable": True}
    assert worker["command"] == [
        "taskiq",
        "worker",
        "--wait-tasks-timeout",
        str(GRACEFUL_TIMEOUT_SECONDS),
        "--shutdown-timeout",
        "60",
        "tenxgraph_api.jobs.worker:broker",
        "app.tasks",
    ]
    assert scheduler["command"][:3] == [
        "taskiq",
        "scheduler",
        "tenxgraph_api.jobs.scheduler:scheduler",
    ]
    assert scheduler["command"][-2:] == ["--update-interval", "10"]
    assert "exactly one scheduler" in (tmp_path / "docker-compose.yml").read_text()


def test_compose_worker_only_and_globs(tmp_path, monkeypatch):
    cfg = _config(tasks=("app.tasks", "jobs/*_tasks.py"))
    _run(tmp_path, monkeypatch, cfg, docker_compose=True)
    services = _compose(tmp_path)["services"]
    assert list(services) == ["10xgraph-api", "worker"]
    assert services["worker"]["command"][-4:] == [
        "app.tasks",
        "--fs-discover",
        "--tasks-pattern",
        "jobs/*_tasks.py",
    ]


def test_no_worker_key_leaves_the_api_service_unchanged(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _config(None), docker_compose=True, k8s=None)
    text = (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    assert text == generate_docker_compose_content("10xgraph-api", 8000)
    assert list(_compose(tmp_path)["services"]) == ["10xgraph-api"]
    _run(tmp_path, monkeypatch, docker_compose=False, k8s=True, force=True)
    assert (tmp_path / "k8s.yaml").read_text(encoding="utf-8") == generate_k8s_manifest_content(
        "10xgraph-api", 8000
    )


def test_k8s_with_worker_and_scheduler(tmp_path, monkeypatch):
    code, _ = _run(tmp_path, monkeypatch, _config(), k8s=True, worker=True, scheduler=True)
    assert code == 0
    api, service, worker, scheduler = _k8s(tmp_path)
    assert service["kind"] == "Service"
    assert worker["metadata"]["name"] == "10xgraph-api-worker"
    assert scheduler["metadata"]["name"] == "10xgraph-api-scheduler"
    api_pod = api["spec"]["template"]["spec"]
    for dep, recreate in ((worker, False), (scheduler, True)):
        pod = dep["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert dep["spec"]["replicas"] == 1
        assert (dep["spec"].get("strategy") == {"type": "Recreate"}) is recreate
        assert pod["securityContext"] == api_pod["securityContext"]
        assert container["securityContext"] == api_pod["containers"][0]["securityContext"]
        assert container["envFrom"] == api_pod["containers"][0]["envFrom"]
        assert container["env"] == api_pod["containers"][0]["env"]
        assert container["resources"] == api_pod["containers"][0]["resources"]
        assert "ports" not in container
        assert "readinessProbe" not in container
        assert "lifecycle" not in container
        assert pod["terminationGracePeriodSeconds"] == (
            GRACEFUL_TIMEOUT_SECONDS + SHUTDOWN_KILL_MARGIN_SECONDS
        )
    assert worker["spec"]["template"]["spec"]["containers"][0]["command"][0] == "taskiq"


def test_memory_broker_has_no_worker_and_prints_a_note(tmp_path, monkeypatch):
    code, text = _run(tmp_path, monkeypatch, _config("memory"), docker_compose=True)
    assert code == 0
    assert list(_compose(tmp_path)["services"]) == ["10xgraph-api"]
    assert "tasks run inside the API process" in text


@pytest.mark.parametrize(
    "options",
    [{"worker": True}, {"scheduler": True}],
)
@pytest.mark.parametrize("config", [_config(None), _config("memory")])
def test_worker_flags_need_a_real_broker(tmp_path, monkeypatch, config, options):
    code, _ = _run(tmp_path, monkeypatch, config, docker_compose=True, **options)
    assert code != 0
    assert not (tmp_path / "docker-compose.yml").exists()


def test_scheduler_without_worker_is_an_error(tmp_path, monkeypatch):
    code, _ = _run(
        tmp_path, monkeypatch, _config(), docker_compose=True, scheduler=True, worker=False
    )
    assert code != 0
    assert not (tmp_path / "docker-compose.yml").exists()


def test_worker_flag_needs_compose_or_k8s(tmp_path, monkeypatch):
    code, _ = _run(tmp_path, monkeypatch, _config(), worker=True)
    assert code != 0


def test_invalid_worker_key_is_a_validation_error(tmp_path, monkeypatch):
    cfg = {"agent": "a:b", "worker": {"broker": {"type": "sqs"}}}
    code, text = _run(tmp_path, monkeypatch, cfg, docker_compose=True)
    assert code != 0
    assert not (tmp_path / "docker-compose.yml").exists()


def test_non_interactive_defaults(tmp_path, monkeypatch):
    # worker is included without a flag, the scheduler only with --scheduler
    _run(tmp_path, monkeypatch, _config(), docker_compose=True)
    assert list(_compose(tmp_path)["services"]) == ["10xgraph-api", "worker"]
    # Dockerfile only without any deploy flag: no compose file at all
    (tmp_path / "docker-compose.yml").unlink()
    _run(tmp_path, monkeypatch, force=True)
    assert not (tmp_path / "docker-compose.yml").exists()
    assert not (tmp_path / "k8s.yaml").exists()


def test_no_worker_flag_skips_the_worker(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _config(), docker_compose=True, worker=False)
    assert list(_compose(tmp_path)["services"]) == ["10xgraph-api"]


# questions


def test_questions_compose_worker_and_scheduler(tmp_path, monkeypatch):
    prompts = FakePrompts("compose", True, True)
    code, _ = _run(tmp_path, monkeypatch, _config(), prompts)
    assert code == 0
    assert prompts.asked == [
        "Where will you deploy?",
        "Run a background worker?",
        "Run the scheduler?",
    ]
    assert list(_compose(tmp_path)["services"]) == ["10xgraph-api", "worker", "scheduler"]


def test_questions_k8s_no_worker_skips_the_scheduler_question(tmp_path, monkeypatch):
    prompts = FakePrompts("k8s", False)
    code, _ = _run(tmp_path, monkeypatch, _config(), prompts)
    assert code == 0
    assert len(prompts.asked) == 2
    assert [d["kind"] for d in _k8s(tmp_path)] == ["Deployment", "Service"]


def test_questions_dockerfile_only_asks_nothing_more(tmp_path, monkeypatch):
    prompts = FakePrompts("dockerfile")
    code, _ = _run(tmp_path, monkeypatch, _config(), prompts)
    assert code == 0
    assert prompts.asked == ["Where will you deploy?"]
    assert (tmp_path / "Dockerfile").exists()
    assert not (tmp_path / "docker-compose.yml").exists()


def test_questions_skip_what_flags_answered(tmp_path, monkeypatch):
    prompts = FakePrompts()
    code, _ = _run(
        tmp_path,
        monkeypatch,
        _config(),
        prompts,
        docker_compose=True,
        worker=True,
        scheduler=False,
    )
    assert code == 0
    assert prompts.asked == []


def test_questions_without_a_worker_key_ask_only_the_target(tmp_path, monkeypatch):
    prompts = FakePrompts("compose")
    _run(tmp_path, monkeypatch, _config(None), prompts)
    assert prompts.asked == ["Where will you deploy?"]


def test_cancel_writes_nothing(tmp_path, monkeypatch):
    code, _ = _run(tmp_path, monkeypatch, _config(), FakePrompts("compose", None))
    assert code == 0
    assert not (tmp_path / "Dockerfile").exists()
    assert not (tmp_path / "docker-compose.yml").exists()


def test_declining_the_overwrite_writes_nothing(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("old", encoding="utf-8")
    prompts = FakePrompts("compose", True, False, False)
    code, _ = _run(tmp_path, monkeypatch, _config(), prompts)
    assert code == 0
    assert prompts.asked[-1] == "Overwrite existing docker-compose.yml?"
    assert (tmp_path / "docker-compose.yml").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "Dockerfile").exists()


def test_accepting_the_overwrite_replaces_the_file(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("old", encoding="utf-8")
    code, _ = _run(tmp_path, monkeypatch, _config(), FakePrompts("compose", True, False, True))
    assert code == 0
    assert "worker" in _compose(tmp_path)["services"]


def test_existing_file_without_force_still_fails_without_questions(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("old", encoding="utf-8")
    code, _ = _run(tmp_path, monkeypatch, _config(), docker_compose=True)
    assert code == 1
    assert not (tmp_path / "Dockerfile").exists()


# dependency warning


def test_warns_when_the_jobs_extra_is_missing(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("10xgraph-api[jwt]\n", encoding="utf-8")
    _, text = _run(tmp_path, monkeypatch, _config(), docker_compose=True)
    assert "10xgraph-api[jobs]" in text


def test_no_warning_with_the_jobs_extra(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("10xgraph-api[jwt, jobs]>=1\n", encoding="utf-8")
    _, text = _run(tmp_path, monkeypatch, _config(), docker_compose=True)
    assert "cannot start" not in text


def test_warns_with_the_rabbitmq_extra_for_that_broker(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["10xgraph-api[jobs]"]\n', encoding="utf-8"
    )
    _, text = _run(tmp_path, monkeypatch, _config("rabbitmq"), docker_compose=True)
    assert "10xgraph-api[jobs-rabbitmq]" in text


def test_unreadable_dependency_file_does_not_warn(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    _, text = _run(tmp_path, monkeypatch, _config(), docker_compose=True)
    assert "cannot start" not in text
