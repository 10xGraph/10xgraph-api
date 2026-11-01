"""Production template defaults: rate limit key (G-168), token revocation (G-167), docs path
(G-120) and the Kubernetes manifest env (G-95)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from tenxgraph_api.cli.commands.init import InitCommand
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
        config = _cmd()._build_config(_context(auth), True)
        assert config["cache_store"]["rate_limit"]["by"] == "user"
    config = _cmd()._build_config(_context("none"), True)
    assert config["cache_store"]["rate_limit"]["by"] == "ip"


def test_production_cache_store_follows_the_answers() -> None:
    config = _cmd()._build_config(_context("jwt"), True)
    assert config["cache_store"]["type"] == "redis"
    assert "url" not in config["cache_store"]
    assert config["cache_store"]["token_revocation"] is True

    memory = _cmd()._build_config(_context("jwt", "memory"), True)["cache_store"]
    assert memory["type"] == "memory"
    assert "url" not in memory
    assert memory["rate_limit"]["enabled"] is True
    assert memory["token_revocation"] is True

    assert "token_revocation" not in _cmd()._build_config(_context("none"), True)["cache_store"]
    off = _cmd()._build_config(_context("jwt", rate_limit=False, token_revocation=False), True)
    assert off["cache_store"] == {"type": "redis"}
    for store in ("redis", "memory"):
        config = _cmd()._build_config(_context("jwt", store), True)
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
    assert prod.id_generator == "uuid"
    assert prod.token_revocation is not None
    assert prod.thread_name_generator == "graph.thread_name_generator:MyNameGenerator"

    dev = GraphConfig(str(templates / "dev" / "10xgraph.json"))
    assert dev.cache_store.type == "memory" and not dev.cache_store.type_set
    assert dev.rate_limit is None and dev.token_revocation is None
    assert dev.thread_name_generator == "random"


def test_generated_exclude_paths_use_the_real_redoc_path() -> None:
    config = _cmd()._build_config(_context("jwt"), True)
    assert "/redocs" in config["cache_store"]["rate_limit"]["exclude_paths"]


def test_k8s_env_does_not_shadow_the_secret() -> None:
    yaml = __import__("pytest").importorskip("yaml")
    deployment, _ = yaml.safe_load_all(generate_k8s_manifest_content("svc", 8000))
    env = {e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "ORIGINS" not in env
    assert "ALLOWED_HOST" not in env


def _recording_cache(monkeypatch):
    """Route ``RedisCacheStore.from_url`` to an in-memory store that records its URL."""
    from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore, RedisCacheStore

    calls: list[tuple] = []

    class _Store(RedisCacheStore):
        """Stands in for a shared store; keeps its data in memory."""

        def __init__(self):
            self.inner = InMemoryCacheStore()

        async def now(self):
            return await self.inner.now()

        async def get(self, key):
            return await self.inner.get(key)

        async def set(self, key, value, ttl, mode="always"):
            return await self.inner.set(key, value, ttl, mode)

        async def aclose(self):
            calls.append(("closed",))

    store = _Store()

    def from_url(url, **kwargs):
        calls.append(("url", url))
        return store

    monkeypatch.setattr(RedisCacheStore, "from_url", staticmethod(from_url))
    return store, calls


def test_revoke_command_writes_to_the_cache_store(monkeypatch, tmp_path) -> None:
    import asyncio

    from tenxgraph_api.cli.commands.revoke import RevokeCommand

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REDIS_URL", raising=False)
    store, calls = _recording_cache(monkeypatch)
    cmd = RevokeCommand(output=OutputFormatter(stream=io.StringIO(), quiet=True))
    assert cmd.execute(user="u1", before=100.0, redis_url="redis://x") == 0
    assert cmd.execute(jti="j1", redis_url="redis://x") == 0
    assert calls.count(("url", "redis://x")) == 2
    assert calls.count(("closed",)) == 2
    assert asyncio.run(store.get("revoked:user:u1")) == b"100.000000"
    assert asyncio.run(store.get("revoked:jti:j1")) == b"1"
    assert cmd.execute(redis_url="redis://x") != 0  # neither --user nor --jti
    assert cmd.execute(user="u", jti="j", redis_url="redis://x") != 0


def test_revoke_command_uses_the_server_selection(monkeypatch, tmp_path) -> None:
    from tenxgraph_api.cli.commands.revoke import RevokeCommand

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REDIS_URL", raising=False)
    _, calls = _recording_cache(monkeypatch)
    monkeypatch.setenv("TG_TEST_REVOKE_URL", "redis://config")
    (tmp_path / "10xgraph.json").write_text(
        json.dumps(
            {
                "agent": "g:app",
                "cache_store": {
                    "type": "redis",
                    "url": "${TG_TEST_REVOKE_URL}",
                    "token_revocation": {"user_ttl": 60},
                },
            }
        )
    )
    cmd = RevokeCommand(output=OutputFormatter(stream=io.StringIO(), quiet=True))
    assert cmd.execute(user="u1") == 0
    assert ("url", "redis://config") in calls
    assert cmd._cache_store(None, "10xgraph.json")[1] == 60

    # --redis-url wins over the config.
    assert cmd.execute(user="u2", redis_url="redis://flag") == 0
    assert ("url", "redis://flag") in calls


def test_revoke_command_refuses_an_in_memory_cache_store(monkeypatch, tmp_path) -> None:
    import pytest

    from tenxgraph_api.cli.commands.revoke import RevokeCommand
    from tenxgraph_api.cli.exceptions import ValidationError

    monkeypatch.chdir(tmp_path)
    # REDIS_URL alone never selects Redis: the store comes only from cache_store.
    monkeypatch.setenv("REDIS_URL", "redis://env-host:6379/0")
    _, calls = _recording_cache(monkeypatch)
    stream = io.StringIO()
    cmd = RevokeCommand(output=OutputFormatter(stream=stream))
    for store in (None, {"type": "memory", "token_revocation": True}):
        data = {"agent": "g:app"} if store is None else {"agent": "g:app", "cache_store": store}
        (tmp_path / "10xgraph.json").write_text(json.dumps(data))
        assert cmd.execute(user="u1") != 0
        with pytest.raises(ValidationError, match="in-memory") as info:
            cmd._cache_store(None, "10xgraph.json")
        assert '"cache_store": {"type": "redis"' in str(info.value)
        assert "--redis-url" in str(info.value)
    assert calls == []


def test_revoke_command_rejects_a_top_level_token_revocation(monkeypatch, tmp_path) -> None:
    import pytest

    from tenxgraph_api.cli.commands.revoke import RevokeCommand

    monkeypatch.chdir(tmp_path)
    (tmp_path / "10xgraph.json").write_text(
        json.dumps({"agent": "g:app", "token_revocation": "redis"})
    )
    cmd = RevokeCommand(output=OutputFormatter(stream=io.StringIO(), quiet=True))
    with pytest.raises(ValueError, match="`token_revocation` goes inside `cache_store`"):
        cmd._cache_store("redis://x", "10xgraph.json")
