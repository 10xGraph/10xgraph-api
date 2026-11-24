"""Production template defaults: rate limit key (G-168), token revocation (G-167), docs path
(G-120) and the Kubernetes manifest env (G-95)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from tenxgraph_api.cli.commands.init import InitCommand
from tenxgraph_api.cli.commands.init_context import build_config
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.cli.templates.defaults import generate_k8s_manifest_content


def _cmd() -> InitCommand:
    return InitCommand(output=OutputFormatter(stream=io.StringIO(), quiet=True))


def _context(auth: str, cache_store: str = "redis", **flags) -> dict:
    return _cmd()._non_interactive_context(
        path=".",
        agent_name="demo",
        template="production",
        auth=auth,
        cache_store=cache_store,
        **flags,
    )


def test_production_rate_limit_is_per_user_when_auth_is_on() -> None:
    for auth in ("jwt", "custom"):
        config = build_config(_context(auth), True)
        assert config["cache_store"]["rate_limit"]["by"] == "user"
    config = build_config(_context("none"), True)
    assert config["cache_store"]["rate_limit"]["by"] == "ip"


def test_production_cache_store_follows_the_answers() -> None:
    config = build_config(_context("jwt"), True)
    assert config["cache_store"]["type"] == "redis"
    assert "url" not in config["cache_store"]
    assert config["cache_store"]["token_revocation"] is True

    memory = build_config(_context("jwt", "memory"), True)["cache_store"]
    assert memory["type"] == "memory"
    assert "url" not in memory
    assert memory["rate_limit"]["enabled"] is True
    assert memory["token_revocation"] is True

    assert "token_revocation" not in build_config(_context("none"), True)["cache_store"]
    off = build_config(_context("jwt", rate_limit=False, token_revocation=False), True)
    assert off["cache_store"] == {"type": "redis"}
    for store in ("redis", "memory"):
        config = build_config(_context("jwt", store), True)
        assert not {"redis", "rate_limit", "token_revocation"} & set(config)


def test_static_production_template_matches() -> None:
    path = (
        Path(__file__).parents[2] / "tenxgraph_api" / "cli" / "templates" / "prod" / "10xgraph.json"
    )
    config = json.loads(path.read_text(encoding="utf-8"))
    store = config["cache_store"]
    assert store["type"] == "redis"
    assert "url" not in store
    assert store["token_revocation"] is True
    assert "/redocs" in store["rate_limit"]["exclude_paths"]
    assert "/redoc" not in store["rate_limit"]["exclude_paths"]
    assert not {"redis", "rate_limit", "token_revocation"} & set(config)


def test_static_templates_load_through_graph_config(monkeypatch) -> None:
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

    templates = Path(__file__).parents[2] / "tenxgraph_api" / "cli" / "templates"
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.chdir(templates)  # the configs name ".env": never load one from elsewhere
    prod = GraphConfig(str(templates / "prod" / "10xgraph.json"))
    assert prod.cache_store.type == "redis"
    assert prod.cache_store.expanded_url() == "redis://localhost:6379/0"
    assert prod.rate_limit is not None and prod.rate_limit.fail_open is False
    assert prod.id_generator is None
    assert prod.token_revocation is not None
    assert prod.thread_name_generator == "first_message"

    dev = GraphConfig(str(templates / "dev" / "10xgraph.json"))
    assert dev.cache_store.type == "memory" and not dev.cache_store.type_set
    assert dev.rate_limit is None and dev.token_revocation is None
    assert dev.thread_name_generator == "none"


def test_generated_exclude_paths_use_the_real_redoc_path() -> None:
    config = build_config(_context("jwt"), True)
    assert "/redocs" in config["cache_store"]["rate_limit"]["exclude_paths"]


def test_k8s_env_does_not_shadow_the_secret() -> None:
    yaml = __import__("pytest").importorskip("yaml")
    deployment, _ = yaml.safe_load_all(generate_k8s_manifest_content("svc", 8000))
    env = {e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "ORIGINS" not in env
    assert "ALLOWED_HOST" not in env


def test_production_env_sets_json_logs_in_every_target() -> None:
    from tenxgraph_api.cli.templates.defaults import (
        generate_docker_compose_content,
        generate_dockerfile_content,
    )

    assert "- LOG_FORMAT=json" in generate_docker_compose_content("svc", 8000)
    assert "ENV LOG_FORMAT=json" in generate_dockerfile_content(
        "3.12", 8000, "requirements.txt", True
    )
    yaml = __import__("pytest").importorskip("yaml")
    deployment, _ = yaml.safe_load_all(generate_k8s_manifest_content("svc", 8000))
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "LOG_FORMAT", "value": "json"} in env
