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


def _context(auth: str, rate_limit: str = "redis") -> dict:
    return _cmd()._non_interactive_context(
        path=".",
        agent_name="demo",
        template="production",
        auth=auth,
        rate_limit=rate_limit,
    )


def test_production_rate_limit_is_per_user_when_auth_is_on() -> None:
    for auth in ("jwt", "custom"):
        config = _cmd()._build_config(_context(auth), True)
        assert config["rate_limit"]["by"] == "user"
    config = _cmd()._build_config(_context("none"), True)
    assert config["rate_limit"]["by"] == "ip"


def test_production_config_enables_token_revocation_with_redis() -> None:
    config = _cmd()._build_config(_context("jwt"), True)
    assert config["token_revocation"] == "redis"
    assert "token_revocation" not in _cmd()._build_config(_context("jwt", "memory"), True)
    assert "token_revocation" not in _cmd()._build_config(_context("none"), True)


def test_static_production_template_matches() -> None:
    path = (
        Path(__file__).parents[2] / "tenxgraph_api" / "cli" / "templates" / "prod" / "10xgraph.json"
    )
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["token_revocation"] == "redis"
    assert "/redocs" in config["rate_limit"]["exclude_paths"]
    assert "/redoc" not in config["rate_limit"]["exclude_paths"]


def test_generated_exclude_paths_use_the_real_redoc_path() -> None:
    config = _cmd()._build_config(_context("jwt"), True)
    assert "/redocs" in config["rate_limit"]["exclude_paths"]


def test_k8s_env_does_not_shadow_the_secret() -> None:
    yaml = __import__("pytest").importorskip("yaml")
    deployment, _ = yaml.safe_load_all(generate_k8s_manifest_content("svc", 8000))
    env = {e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "ORIGINS" not in env
    assert "ALLOWED_HOST" not in env


def test_revoke_command_writes_to_redis(monkeypatch) -> None:
    from tenxgraph_api.cli.commands.revoke import RevokeCommand
    from tenxgraph_api.src.app.core.auth import revocation

    calls: list[tuple] = []

    class _Backend:
        def __init__(self, url):
            calls.append(("url", url))

        async def revoke_user(self, user, before=None):
            calls.append(("user", user, before))

        async def revoke_token(self, jti, expires_at=None):
            calls.append(("jti", jti, expires_at))

        async def aclose(self):
            calls.append(("closed",))

    monkeypatch.setattr(revocation, "RedisTokenRevocation", _Backend)
    cmd = RevokeCommand(output=OutputFormatter(stream=io.StringIO(), quiet=True))
    assert cmd.execute(user="u1", redis_url="redis://x") == 0
    assert cmd.execute(jti="j1", expires_at=5.0, redis_url="redis://x") == 0
    assert ("user", "u1", None) in calls and ("jti", "j1", 5.0) in calls
    assert calls.count(("closed",)) == 2
    assert cmd.execute(redis_url="redis://x") != 0  # neither --user nor --jti
    assert cmd.execute(user="u", jti="j", redis_url="redis://x") != 0
