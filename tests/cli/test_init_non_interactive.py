"""Non-interactive and dry-run project initialization."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from tenxgraph_api.cli.commands.init import InitCommand
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore, RedisCacheStore
from tenxgraph_api.src.app.core.cache_store.loader import build_cache_store
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


def test_dry_run_does_not_create_target(tmp_path: Path) -> None:
    target = tmp_path / "preview-agent"
    stream = io.StringIO()
    command = InitCommand(OutputFormatter(stream=stream))

    result = command.execute(
        path=str(target),
        agent_name="Preview Agent",
        template="production",
        auth="jwt",
        rate_limit="redis",
        non_interactive=True,
        dry_run=True,
    )

    assert result == 0
    assert not target.exists()
    assert "Files that would be created" in stream.getvalue()
    assert "no files were written" in stream.getvalue()


def test_non_interactive_quick_start_creates_project(tmp_path: Path) -> None:
    target = tmp_path / "weather-agent"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))

    result = command.execute(
        path=str(target),
        agent_name="Weather Agent",
        template="quick-start",
        non_interactive=True,
    )

    assert result == 0
    assert (target / "10xgraph.json").exists()
    assert (target / "graph" / "agent.py").exists()


def test_non_interactive_rejects_production_options_on_quick_start(tmp_path: Path) -> None:
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    result = command.execute(
        path=str(tmp_path / "invalid"),
        template="quick-start",
        auth="jwt",
        non_interactive=True,
    )
    assert result != 0


def _dependencies(target: Path) -> list[str]:
    import tomllib

    return tomllib.loads((target / "pyproject.toml").read_text())["project"]["dependencies"]


def test_production_dependencies_follow_the_answers(tmp_path: Path) -> None:
    """JWT and Redis add their extras; the Dockerfile installs exactly this list."""
    target = tmp_path / "secure-agent"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    result = command.execute(
        path=str(target),
        agent_name="Secure Agent",
        template="production",
        auth="jwt",
        rate_limit="redis",
        provider="openai",
        non_interactive=True,
    )
    assert result == 0
    assert _dependencies(target) == ["10xgraph[openai,pg_checkpoint]", "10xgraph-api[jwt,redis]"]

    import json

    config = json.loads((target / "10xgraph.json").read_text())
    assert config["redis"] == "${REDIS_URL}"
    assert set(config["rate_limit"]) >= {"enabled", "requests", "window", "by"}
    assert "redis" not in config["rate_limit"]

    agent_py = (target / "graph" / "agent.py").read_text()
    assert 'model="gpt-4o-mini"' in agent_py
    assert 'provider="openai"' in agent_py
    assert 'OPENAI_API_KEY=""' in (target / ".env.example").read_text()
    assert '"OPENAI_API_KEY"' in (target / "tests" / "conftest.py").read_text()
    assert "$core_requirement" not in (target / "pyproject.toml").read_text()
    assert "$api_requirement" not in (target / "pyproject.toml").read_text()


def test_production_without_jwt_installs_redis_but_not_jwt(tmp_path: Path) -> None:
    target = tmp_path / "open-agent"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    result = command.execute(
        path=str(target),
        agent_name="Open Agent",
        template="production",
        auth="custom",
        rate_limit="memory",
        provider="anthropic",
        non_interactive=True,
    )
    assert result == 0
    assert _dependencies(target) == ["10xgraph[anthropic,pg_checkpoint]", "10xgraph-api[redis]"]
    assert 'model="claude-sonnet-5-5"' in (target / "graph" / "agent.py").read_text()


def test_quick_start_defaults_to_google(tmp_path: Path) -> None:
    target = tmp_path / "quick-agent"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    assert command.execute(path=str(target), agent_name="Quick", non_interactive=True) == 0
    agent_py = (target / "graph" / "agent.py").read_text()
    assert 'model="gemini-2.5-flash"' in agent_py
    assert 'provider="google"' in agent_py
    assert 'GOOGLE_API_KEY=""' in (target / ".env.example").read_text()


def test_non_interactive_rejects_unknown_provider(tmp_path: Path) -> None:
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    result = command.execute(path=str(tmp_path / "x"), provider="mistral", non_interactive=True)
    assert result != 0


def test_websocket_off_by_default_and_not_installed(tmp_path: Path) -> None:
    import json

    target = tmp_path / "no-ws"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    result = command.execute(
        path=str(target), template="production", provider="openai", non_interactive=True
    )
    assert result == 0
    assert _dependencies(target) == ["10xgraph[openai,pg_checkpoint]", "10xgraph-api[jwt,redis]"]
    config = json.loads((target / "10xgraph.json").read_text())
    assert config["websocket"] == {"enabled": False}


def test_websocket_on_adds_the_extra_and_enables_the_routes(tmp_path: Path) -> None:
    import json

    target = tmp_path / "ws"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    result = command.execute(
        path=str(target),
        template="production",
        auth="jwt",
        provider="anthropic",
        websocket=True,
        non_interactive=True,
    )
    assert result == 0
    assert _dependencies(target) == [
        "10xgraph[anthropic,pg_checkpoint]",
        "10xgraph-api[jwt,redis,websocket]",
    ]
    config = json.loads((target / "10xgraph.json").read_text())
    assert config["websocket"] == {"enabled": True}


def test_init_scaffolds_a_gitignore_that_keeps_env_out(tmp_path: Path) -> None:
    target = tmp_path / "agent"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))

    result = command.execute(path=str(target), template="production", non_interactive=True)

    assert result == 0
    lines = (target / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", "!.env.example", "uploads/", "eval_reports/"} <= set(lines)
    assert lines.index("!.env.example") > lines.index(".env.*")


def test_init_adds_secret_patterns_to_an_existing_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    command = InitCommand(OutputFormatter(stream=io.StringIO()))

    result = command.execute(path=str(tmp_path), template="quick-start", non_interactive=True)

    assert result == 0
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert content.startswith("node_modules/\n")
    assert ".env\n" in content


@pytest.mark.parametrize(
    ("template", "auth", "rate_limit"),
    [
        ("quick-start", None, "none"),
        ("production", "jwt", "none"),
        ("production", "jwt", "memory"),
        ("production", "jwt", "redis"),
        ("production", "custom", "redis"),
        ("production", "none", "redis"),
    ],
)
def test_generated_config_loads_through_graph_config(
    tmp_path: Path, monkeypatch, template: str, auth: str | None, rate_limit: str
) -> None:
    """Every init recipe writes a 10xgraph.json the server parses without a config error."""
    target = tmp_path / "agent"
    result = InitCommand(OutputFormatter(stream=io.StringIO())).execute(
        path=str(target),
        agent_name="Agent",
        template=template,
        auth=auth,
        rate_limit=rate_limit,
        non_interactive=True,
    )
    assert result == 0
    monkeypatch.chdir(target)  # the config names ".env": never load one from elsewhere
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")

    config = GraphConfig(str(target / "10xgraph.json"))
    assert config.graph_path == "graph.agent:app"
    config.auth_config()
    config.websocket  # noqa: B018 - parsing is the check
    config.runs  # noqa: B018
    assert config.cache_store is None
    limit = config.rate_limit
    assert (limit is not None) == (rate_limit != "none")
    revocation = config.token_revocation
    assert (revocation is not None) == (rate_limit == "redis" and auth in {"jwt", "custom"})
    if rate_limit == "redis":
        assert config.redis_url == "redis://localhost:6379/0"
    else:
        assert config.redis_url is None

    store, _ = build_cache_store(config, SimpleNamespace(REDIS_URL=None))
    assert isinstance(store, RedisCacheStore if rate_limit == "redis" else InMemoryCacheStore)
    asyncio.run(store.aclose())
