"""Tests for `10xgraph init` production setup."""

from __future__ import annotations

import io
from pathlib import Path

from tenxgraph_api.cli.commands.init import InitCommand
from tenxgraph_api.cli.commands.init_context import build_config
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.src.app.core.auth.auth_config import check_auth_config


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


def test_full_mode_defaults_to_memory_storage_and_jwt(tmp_path: Path) -> None:
    """With no answers, Full scaffolds JWT, rate limits and in-memory storage."""
    import json
    import tomllib

    target = tmp_path / "default-full"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    result = command.execute(path=str(target), agent_name="Default Full", mode="full", yes=True)
    assert result == 0

    config = json.loads((target / "10xgraph.json").read_text())
    assert config["auth"] == {"method": "jwt"}
    assert "authorization" not in config
    assert "store" not in config
    assert config["ag_ui"] == {"enabled": False, "allow_client_tools": False}
    assert config["remote_tools"] == []
    assert check_auth_config(config, production=True) == []
    assert config["cache_store"]["type"] == "memory"
    assert config["cache_store"]["rate_limit"]["enabled"] is True
    assert config["cache_store"]["token_revocation"] is True
    assert config["thread_name_generator"] == "first_message"
    assert not (target / "graph" / "thread_name_generator.py").exists()
    assert "InMemoryCheckpointer()" in (target / "graph" / "agent.py").read_text()
    assert (target / "evals").is_dir() and (target / "tests").is_dir()

    dependencies = tomllib.loads((target / "pyproject.toml").read_text())["project"]["dependencies"]
    assert dependencies == ["10xgraph[google-genai]", "10xgraph-api[jwt]"]


def test_full_postgres_scaffolds_pg_checkpointer_and_env(tmp_path: Path) -> None:
    target = tmp_path / "pg"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    result = command.execute(
        path=str(target), agent_name="Pg", mode="full", storage="postgres", non_interactive=True
    )
    assert result == 0
    assert "PgCheckpointer(" in (target / "graph" / "agent.py").read_text()
    env_example = (target / ".env.example").read_text()
    assert "DATABASE_URL=" in env_example
    assert "REDIS_URL=" in env_example


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
        return self._answer(message, default)

    def checkbox(self, message, choices, **_kwargs):
        return self._answer(message, [c.value for c in choices if c.checked])


def _run_prompts(monkeypatch, answers=None):
    output = io.StringIO()
    formatter = OutputFormatter(stream=output)
    scripted = _Scripted(answers)
    monkeypatch.setattr(formatter, "prompts", lambda: scripted)
    return InitCommand(output=formatter)._prompt_user(), scripted, output.getvalue()


_FULL = {"Which setup?": "full"}


def test_interactive_quick_asks_three_questions(monkeypatch) -> None:
    context, scripted, _ = _run_prompts(monkeypatch)
    assert scripted.asked == ["Which setup?", "What is your agent name?", "Which AI provider?"]
    assert context is not None and context["setup_type"] == "quick_start"
    assert context["storage"] is None and context["cache_store"] is None
    assert context["rate_limit"] is False and context["auth"] == "none"
    assert build_config(context, False).keys() >= {"agent"}
    assert "cache_store" not in build_config(context, False)


def test_interactive_full_asks_four_more_questions(monkeypatch) -> None:
    context, scripted, _ = _run_prompts(monkeypatch, _FULL)
    assert scripted.asked == [
        "Which setup?",
        "What is your agent name?",
        "Which AI provider?",
        "Where should state be stored?",
        "How should the API be secured?",
        "Background tasks?",
        "Which extras?",
    ]
    assert context is not None
    assert context["setup_type"] == "production"
    assert context["storage"] == "memory"
    assert context["auth"] == "jwt"
    assert context["cache_store"] == "memory"
    assert context["rate_limit"] is True
    assert context["token_revocation"] is True
    assert context["id_generator"] == "uuid"
    assert (context["evals"], context["tests"]) == (True, True)
    assert (context["websocket"], context["ag_ui"], context["otel"]) == (False, False, False)


def test_interactive_full_writes_the_recommended_config(monkeypatch) -> None:
    import json

    context, _, _ = _run_prompts(monkeypatch, _FULL)
    config = build_config(context, True)
    assert config["auth"] == {"method": "jwt"}
    assert "authorization" not in config
    assert config["ag_ui"] == {"enabled": False, "allow_client_tools": False}
    assert config["remote_tools"] == []
    assert check_auth_config(config, production=True) == []
    assert config["id_generator"] == "uuid"
    assert config["cache_store"] == {
        "type": "memory",
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
    for key in ("auth", "ag_ui", "remote_tools", "id_generator", "thread_name_generator"):
        assert static[key] == config[key], key


def test_summary_shows_the_full_choices(monkeypatch) -> None:
    context, _, _ = _run_prompts(monkeypatch, _FULL)
    stream = io.StringIO()
    InitCommand(output=OutputFormatter(stream=stream))._print_summary(context)
    text = stream.getvalue()
    for label in (
        "Storage",
        "Auth",
        "Cache store",
        "Rate limit",
        "Token revocation",
        "ID generator",
        "Evaluations",
        "Tests",
    ):
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
            path=str(target),
            agent_name="Pg Agent",
            mode="full",
            storage="postgres",
            non_interactive=True,
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


def test_production_template_writes_the_audit_log(tmp_path: Path) -> None:
    import json

    target = tmp_path / "audited"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    assert command.execute(path=str(target), agent_name="Audited", mode="full", yes=True) == 0
    config = json.loads((target / "10xgraph.json").read_text())
    assert config["audit"] == {"enabled": True, "sinks": ["log"], "include_reads": True}


def test_summary_shows_the_audit_log(monkeypatch) -> None:
    context, _, _ = _run_prompts(monkeypatch, _FULL)
    stream = io.StringIO()
    InitCommand(output=OutputFormatter(stream=stream))._print_summary(context)
    assert "Audit log" in stream.getvalue()
