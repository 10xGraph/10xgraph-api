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
        "rate_limit": "none",
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
    assert config["rate_limit"]["enabled"] is True
    assert config["rate_limit"]["backend"] == "redis"

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


def test_interactive_production_prompts_default_to_jwt_and_redis(
    monkeypatch, tmp_path: Path
) -> None:
    """Accepting every default in the production flow gives JWT and the Redis limiter."""

    class _AcceptDefaults:
        def require_interactive(self, **_kwargs) -> None:
            return None

        def text(self, _message, default="", **_kwargs):
            return default

        def confirm(self, _message, default=False, **_kwargs):
            return default

        def select(self, message, _choices, default=None, **_kwargs):
            return "production" if message == "Which project template?" else default

    output = SilentOutput()
    monkeypatch.setattr(output, "prompts", lambda: _AcceptDefaults())

    context = InitCommand(output=output)._prompt_user()

    assert context is not None
    assert context["setup_type"] == "production"
    assert context["auth"] == "jwt"
    assert context["rate_limit"] == "redis"


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
