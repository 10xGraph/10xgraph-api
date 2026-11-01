"""Tests for `10xgraph init` production setup."""

from __future__ import annotations

import io
from pathlib import Path

from tenxgraph_api.cli.commands.init import InitCommand
from tenxgraph_api.cli.core.output import OutputFormatter


def SilentOutput() -> OutputFormatter:
    """A real formatter with output suppressed, so it tracks the live surface."""
    return OutputFormatter(stream=io.StringIO(), quiet=True)


def _skip_binary(original):
    """Wrap _should_skip to also exclude non-text template artifacts."""

    def patched(self, src, template_dir, context, is_prod):
        if any(part in {".ruff_cache", "__pycache__"} for part in src.parts):
            return True
        return original(self, src, template_dir, context, is_prod)

    return patched


def test_init_prod_creates_extra_files(monkeypatch, tmp_path: Path) -> None:
    """Ensure prod init creates 10xgraph.json, graph files, and prod configs."""
    ctx = {
        "agent_name": "MyAgent",
        "agent_name_slug": "my-agent",
        "setup_type": "production",
        "auth": "none",
        "cache_store": "memory",
        "rate_limit": False,
        "token_revocation": False,
    }
    monkeypatch.setattr(InitCommand, "_prompt_user", lambda self: ctx)
    monkeypatch.setattr(InitCommand, "_should_skip", _skip_binary(InitCommand._should_skip))

    cmd = InitCommand(output=SilentOutput())
    code = cmd.execute(path=str(tmp_path), force=False)

    assert code == 0, "InitCommand.execute() returned non-zero"

    # Core files
    assert (tmp_path / "10xgraph.json").exists()
    assert (tmp_path / "graph" / "agent.py").exists()
    assert (tmp_path / "graph" / "__init__.py").exists()

    # Production files — accept either spelling of the pre-commit config filename
    assert (tmp_path / "pyproject.toml").exists()
    assert any(
        (tmp_path / f).exists() for f in (".pre-commit-config.yaml", ".pre-commot-config.yaml")
    )

    # Basic sanity check on pyproject content
    content = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project]" in content
    assert "10xgraph-api" in content


def test_init_prod_skips_binary_template_artifacts() -> None:
    cmd = InitCommand(output=SilentOutput())
    assert cmd._should_skip(
        Path("tenxgraph_api/cli/templates/prod/.ruff_cache/0.5.2/17065574497421059950"),
        Path("tenxgraph_api/cli/templates/prod"),
        {},
        True,
    )
    assert cmd._should_skip(
        Path(
            "tenxgraph_api/cli/templates/prod/tests/__pycache__/test_graph_nodes.cpython-313-pytest-9.0.3.pyc"
        ),
        Path("tenxgraph_api/cli/templates/prod"),
        {},
        True,
    )


def test_production_template_defaults_to_recommended_setup(tmp_path: Path) -> None:
    """With no answers, production scaffolds JWT, ownership, Postgres + Redis and rate limits."""
    import json
    import tomllib

    target = tmp_path / "default-prod"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    result = command.execute(
        path=str(target), agent_name="Default Prod", template="production", non_interactive=True
    )
    assert result == 0

    config = json.loads((target / "10xgraph.json").read_text())
    assert config["auth"] == "jwt"
    assert config["authorization"] == "ownership"
    assert config["cache_store"]["type"] == "redis"
    assert "url" not in config["cache_store"]
    assert config["cache_store"]["rate_limit"]["enabled"] is True
    assert config["cache_store"]["token_revocation"] is True

    agent_py = (target / "graph" / "agent.py").read_text()
    assert "PgCheckpointer(" in agent_py
    memory_lines = [line for line in agent_py.splitlines() if "InMemoryCheckpointer" in line]
    assert memory_lines
    assert all(line.lstrip().startswith("#") for line in memory_lines)

    env_example = (target / ".env.example").read_text()
    assert "DATABASE_URL=" in env_example
    assert "REDIS_URL=" in env_example

    dependencies = tomllib.loads((target / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any("pg_checkpoint" in dep for dep in dependencies)
    assert any(dep.startswith("10xgraph-api[") and "redis" in dep for dep in dependencies)


class _Scripted:
    """Prompt double: answers by message prefix, else accepts the default; records asks."""

    def __init__(self, answers: dict[str, object] | None = None) -> None:
        self.answers = answers or {}
        self.asked: list[str] = []

    def _answer(self, message, default):
        self.asked.append(message)
        for prefix, value in self.answers.items():
            if message.startswith(prefix):
                return value
        return default

    def require_interactive(self, **_kwargs) -> None:
        return None

    def text(self, message, default="", **_kwargs):
        return self._answer(message, default)

    def confirm(self, message, default=False, **_kwargs):
        return self._answer(message, default)

    def select(self, message, _choices, default=None, **_kwargs):
        if message == "Which project template?":
            return "production"
        return self._answer(message, default)


def _run_prompts(monkeypatch, answers=None):
    output = io.StringIO()
    formatter = OutputFormatter(stream=output)
    scripted = _Scripted(answers)
    monkeypatch.setattr(formatter, "prompts", lambda: scripted)
    return InitCommand(output=formatter)._prompt_user(), scripted, output.getvalue()


def test_interactive_production_asks_only_the_basics(monkeypatch) -> None:
    """Name, template and provider are the only questions; Production gets the full setup."""
    context, scripted, text = _run_prompts(monkeypatch)

    assert scripted.asked == [
        "What is your agent name?",
        "Which AI provider?",
    ]
    assert context is not None
    assert context["setup_type"] == "production"
    assert context["auth"] == "jwt"
    assert context["cache_store"] == "redis"
    assert context["rate_limit"] is True
    assert context["token_revocation"] is True
    assert context["id_generator"] == "uuid"
    assert context["websocket"] is False
    assert "cannot reach" not in text


def test_interactive_quick_start_has_no_cache_store(monkeypatch) -> None:
    class _Quick(_Scripted):
        def select(self, message, _choices, default=None, **_kwargs):
            return "quick_start" if message == "Which project template?" else default

    output = io.StringIO()
    formatter = OutputFormatter(stream=output)
    monkeypatch.setattr(formatter, "prompts", lambda: _Quick())
    context = InitCommand(output=formatter)._prompt_user()
    assert context is not None and context["setup_type"] == "quick_start"
    assert context["cache_store"] is None and context["rate_limit"] is False
    assert InitCommand(output=formatter)._build_config(context, False).keys() >= {"agent"}
    assert "cache_store" not in InitCommand(output=formatter)._build_config(context, False)


def test_interactive_production_writes_the_recommended_config(monkeypatch, tmp_path) -> None:
    import json

    context, _, _ = _run_prompts(monkeypatch)
    config = InitCommand(output=SilentOutput())._build_config(context, True)
    assert config["auth"] == "jwt"
    assert config["authorization"] == "ownership"
    assert config["id_generator"] == "uuid"
    assert config["cache_store"] == {
        "type": "redis",
        "rate_limit": {
            "enabled": True,
            "requests": 100,
            "window": 60,
            "by": "user",
            "trusted_proxy_headers": False,
            "fail_open": False,
            "exclude_paths": ["/ping", "/docs", "/redocs", "/openapi.json"],
        },
        "token_revocation": True,
    }
    template = Path(__file__).parents[2] / "tenxgraph_api/cli/templates/prod/10xgraph.json"
    static = json.loads(template.read_text(encoding="utf-8"))
    for key in ("auth", "authorization", "id_generator", "cache_store", "thread_name_generator"):
        assert static[key] == config[key], key


def test_summary_shows_the_production_choices(monkeypatch) -> None:
    context, _, _ = _run_prompts(monkeypatch)
    stream = io.StringIO()
    InitCommand(output=OutputFormatter(stream=stream))._print_summary(context)
    text = stream.getvalue()
    for label in ("Auth", "Cache store", "Rate limit", "Token revocation", "ID generator"):
        assert label in text


def test_production_graph_builds_a_pg_checkpointer_without_services(tmp_path: Path) -> None:
    """The scaffolded agent imports with Postgres and Redis down (connections are lazy)."""
    import subprocess
    import sys

    import pytest

    pytest.importorskip("asyncpg")
    pytest.importorskip("redis")
    pytest.importorskip("google.genai")
    target = tmp_path / "pg-agent"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    assert (
        command.execute(
            path=str(target), agent_name="Pg Agent", template="production", non_interactive=True
        )
        == 0
    )
    script = (
        "import tests.conftest\n"
        "from graph.agent import checkpointer\n"
        "print(type(checkpointer).__name__)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=target,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("PgCheckpointer")
