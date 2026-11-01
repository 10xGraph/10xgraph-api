"""Non-interactive and dry-run project initialization."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

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
        provider="openai",
        non_interactive=True,
    )
    assert result == 0
    assert _dependencies(target) == ["10xgraph[openai,pg_checkpoint]", "10xgraph-api[jwt,redis]"]

    import json

    config = json.loads((target / "10xgraph.json").read_text())
    assert config["cache_store"]["type"] == "redis"
    assert "url" not in config["cache_store"]
    assert set(config["cache_store"]["rate_limit"]) >= {"enabled", "requests", "window", "by"}
    assert config["cache_store"]["token_revocation"] is True
    assert not {"redis", "rate_limit", "token_revocation"} & set(config)

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
        cache_store="memory",
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
    ("template", "auth", "store", "rate_limit", "revocation"),
    [
        ("quick-start", None, None, None, None),
        ("production", "jwt", "memory", False, False),
        ("production", "jwt", "memory", True, True),
        ("production", "jwt", "redis", True, True),
        ("production", "jwt", "redis", False, False),
        ("production", "custom", "redis", True, True),
        ("production", "none", "redis", True, True),
    ],
)
def test_generated_config_loads_through_graph_config(
    tmp_path: Path,
    monkeypatch,
    template: str,
    auth: str | None,
    store: str | None,
    rate_limit: bool | None,
    revocation: bool | None,
) -> None:
    """Every init recipe writes a 10xgraph.json the server parses without a config error."""
    target = tmp_path / "agent"
    result = InitCommand(OutputFormatter(stream=io.StringIO())).execute(
        path=str(target),
        agent_name="Agent",
        template=template,
        auth=auth,
        cache_store=store,
        rate_limit=rate_limit,
        token_revocation=revocation,
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
    settings = config.cache_store
    assert settings.type == (store or "memory")
    assert settings.type_set == (store is not None)
    assert (config.rate_limit is not None) == bool(rate_limit)
    assert (config.token_revocation is not None) == bool(revocation and auth != "none")
    if store == "redis":
        assert settings.url is None
        assert settings.expanded_url() == "redis://localhost:6379/0"

    built, source = build_cache_store(config)
    assert isinstance(built, RedisCacheStore if store == "redis" else InMemoryCacheStore)
    assert (
        source
        == {
            "redis": "REDIS_URL",
            "memory": "cache_store.type",
            None: "cache_store not set",
        }[store]
    )
    asyncio.run(built.aclose())


def _written(tmp_path: Path, **kwargs) -> dict:
    import json

    target = tmp_path / "agent"
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    result = command.execute(
        path=str(target), agent_name="Agent", template="production", non_interactive=True, **kwargs
    )
    assert result == 0
    return json.loads((target / "10xgraph.json").read_text())


def test_cache_store_choices(tmp_path: Path) -> None:
    assert _written(tmp_path)["cache_store"]["type"] == "redis"
    assert _written(tmp_path / "m", cache_store="memory")["cache_store"]["type"] == "memory"
    assert "url" not in _written(tmp_path / "r", cache_store="redis")["cache_store"]


def test_invalid_cache_store_lists_the_choices(tmp_path: Path) -> None:
    stream = io.StringIO()
    result = InitCommand(OutputFormatter(stream=stream)).execute(
        path=str(tmp_path / "x"),
        template="production",
        cache_store="valkey",
        non_interactive=True,
    )
    assert result != 0
    assert "memory or redis" in stream.getvalue()


def test_rate_limit_off_and_token_revocation_off(tmp_path: Path) -> None:
    store = _written(tmp_path / "a", rate_limit=False, token_revocation=False)["cache_store"]
    assert store == {"type": "redis"}
    store = _written(tmp_path / "b", token_revocation=False)["cache_store"]
    assert "token_revocation" not in store and store["rate_limit"]["enabled"] is True


def test_token_revocation_is_ignored_without_auth(tmp_path: Path) -> None:
    store = _written(tmp_path, auth="none", token_revocation=True)["cache_store"]
    assert "token_revocation" not in store


def test_quick_start_ignores_cache_flags(tmp_path: Path) -> None:
    import json

    target = tmp_path / "q"
    result = InitCommand(OutputFormatter(stream=io.StringIO())).execute(
        path=str(target), cache_store="redis", rate_limit=True, non_interactive=True
    )
    assert result == 0
    assert "cache_store" not in json.loads((target / "10xgraph.json").read_text())


def test_next_steps_point_to_config_for_every_template(tmp_path: Path) -> None:
    for template in ("quick-start", "production"):
        stream = io.StringIO()
        InitCommand(OutputFormatter(stream=stream)).execute(
            path=str(tmp_path / template), template=template, non_interactive=True
        )
        text = " ".join(stream.getvalue().split())
        assert "Run 10xgraph config for full control" in text
