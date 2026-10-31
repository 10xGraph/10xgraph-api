"""Production-readiness warnings in `10xgraph config`, `10xgraph build` and `10xgraph init`."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tenxgraph_api.cli.commands import config as config_module
from tenxgraph_api.cli.commands.build import BuildCommand
from tenxgraph_api.cli.commands.config import ConfigCommand
from tenxgraph_api.cli.commands.init import InitCommand
from tenxgraph_api.cli.config_editor.validation import (
    production_readiness_warnings,
    validate_config,
)
from tenxgraph_api.cli.core.output import OutputFormatter


REDIS_RATE_LIMIT = {
    "enabled": True,
    "by": "user",
}
READY = {
    "agent": "graph.react:app",
    "auth": "jwt",
    "redis": "redis://x",
    "rate_limit": REDIS_RATE_LIMIT,
    "token_revocation": True,
}
AUTH_OFF_TEXT = "Auth is off"


def _paths(issues: list[dict], level: str = "warning") -> list[str]:
    return [i["path"] for i in issues if i["level"] == level]


def test_validate_config_warns_when_auth_and_rate_limit_are_missing() -> None:
    paths = _paths(validate_config({"agent": "graph.react:app"}))
    assert "auth" in paths
    assert "rate_limit" in paths


def test_ready_config_has_no_readiness_warnings() -> None:
    assert production_readiness_warnings(READY) == []
    assert not [i for i in validate_config(READY) if i["level"] == "warning"]


@pytest.mark.parametrize("value", ["allow_all", "default", "none"])
def test_open_authorization_with_auth_warns(value: str) -> None:
    issues = validate_config({**READY, "authorization": value})
    assert _paths(issues) == ["authorization"]


@pytest.mark.parametrize("value", [None, "ownership", "auth.perm:Backend"])
def test_safe_authorization_does_not_warn(value: str | None) -> None:
    config = {**READY, "authorization": value}
    assert production_readiness_warnings(config) == []


def test_disabled_rate_limit_warns() -> None:
    config = {**READY, "rate_limit": {"enabled": False}}
    issues = production_readiness_warnings(config)
    assert _paths(issues) == ["rate_limit"]
    assert "a shared cache store" in issues[0]["message"]


def test_token_revocation_without_a_shared_cache_store_warns(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    config = {key: value for key, value in READY.items() if key != "redis"}
    issues = validate_config(config)
    assert _paths(issues) == ["token_revocation"]
    assert "10xgraph revoke" in issues[0]["message"]
    assert not _paths(validate_config({**config, "cache_store": "graph.cache:store"}))


@pytest.mark.parametrize("value", ["redis", "memory", "auth.revoke:Backend"])
def test_old_token_revocation_strings_are_errors_naming_the_cache_store(value: str) -> None:
    issues = validate_config({**READY, "token_revocation": value})
    errors = [i for i in issues if i["level"] == "error"]
    assert [i["path"] for i in errors] == ["token_revocation"]
    assert "cache_store" in errors[0]["message"]


def test_token_revocation_object_form_is_valid() -> None:
    value = {"enabled": True, "token_lifetime": 3600, "user_ttl": None}
    assert validate_config({**READY, "token_revocation": value}) == []


@pytest.mark.parametrize("value", [False, {"enabled": False}])
def test_token_revocation_turned_off_warns_with_auth(value: object) -> None:
    issues = validate_config({**READY, "token_revocation": value})
    assert _paths(issues) == ["token_revocation"]


def test_auth_without_token_revocation_warns() -> None:
    config = {key: value for key, value in READY.items() if key != "token_revocation"}
    assert _paths(production_readiness_warnings(config)) == ["token_revocation"]


def test_per_ip_rate_limit_with_auth_warns() -> None:
    config = {**READY, "rate_limit": {**REDIS_RATE_LIMIT, "by": "ip"}}
    assert _paths(production_readiness_warnings(config)) == ["rate_limit.by"]


def test_redoc_exclusion_with_the_wrong_path_warns() -> None:
    limit = {**REDIS_RATE_LIMIT, "exclude_paths": ["/ping", "/redoc"]}
    issues = production_readiness_warnings({**READY, "rate_limit": limit})
    assert _paths(issues) == ["rate_limit.exclude_paths"]


def test_audit_reports_the_security_posture(tmp_path: Path, monkeypatch) -> None:
    from tenxgraph_api.cli.commands.audit import AuditCommand

    monkeypatch.chdir(tmp_path)
    (tmp_path / "10xgraph.json").write_text(
        json.dumps({"agent": "graph.react:app", "auth": None}), encoding="utf-8"
    )
    weak = AuditCommand._security_check()
    assert weak.status == "warn"
    assert "auth" in weak.detail
    (tmp_path / "10xgraph.json").write_text(json.dumps(READY), encoding="utf-8")
    assert AuditCommand._security_check().status == "pass"


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def _build(tmp_path: Path, monkeypatch, config: dict | str | None) -> tuple[int, str]:
    monkeypatch.chdir(tmp_path)
    if isinstance(config, dict):
        (tmp_path / "10xgraph.json").write_text(json.dumps(config), encoding="utf-8")
    elif isinstance(config, str):
        (tmp_path / "10xgraph.json").write_text(config, encoding="utf-8")
    stream = io.StringIO()
    code = BuildCommand(OutputFormatter(stream=stream)).execute(force=True)
    assert (tmp_path / "Dockerfile").exists()
    return code, stream.getvalue()


def test_build_warns_when_auth_is_off(tmp_path: Path, monkeypatch) -> None:
    code, text = _build(tmp_path, monkeypatch, {"agent": "graph.react:app", "auth": None})
    assert code == 0
    assert AUTH_OFF_TEXT in text


def test_build_is_quiet_for_a_ready_config(tmp_path: Path, monkeypatch) -> None:
    code, text = _build(tmp_path, monkeypatch, READY)
    assert code == 0
    assert AUTH_OFF_TEXT not in text


@pytest.mark.parametrize("config", [None, "{not json"])
def test_build_skips_the_check_without_a_usable_config(
    tmp_path: Path, monkeypatch, config: str | None
) -> None:
    code, text = _build(tmp_path, monkeypatch, config)
    assert code == 0
    assert AUTH_OFF_TEXT not in text


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_production_init_next_steps_point_to_config_and_build(tmp_path: Path) -> None:
    stream = io.StringIO()
    result = InitCommand(OutputFormatter(stream=stream)).execute(
        path=str(tmp_path / "svc"),
        agent_name="Svc",
        template="production",
        non_interactive=True,
    )
    assert result == 0
    text = stream.getvalue()
    assert "10xgraph config" in text
    assert "10xgraph build" in text


def test_quick_start_init_mentions_config_but_not_build(tmp_path: Path) -> None:
    stream = io.StringIO()
    result = InitCommand(OutputFormatter(stream=stream)).execute(
        path=str(tmp_path / "svc"),
        agent_name="Svc",
        template="quick-start",
        non_interactive=True,
    )
    assert result == 0
    text = stream.getvalue()
    assert "10xgraph config" in text
    assert "10xgraph build" not in text


# ---------------------------------------------------------------------------
# config command
# ---------------------------------------------------------------------------


class _StubServer:
    url = "http://127.0.0.1:1"

    def __init__(self, path: Path, port: int = 0, seed_path: Path | None = None) -> None:
        self.store = type("Store", (), {"seeded": False})()

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


def _run_config(tmp_path: Path, monkeypatch, content: str | None) -> str:
    monkeypatch.setattr(config_module, "ConfigEditorServer", _StubServer)
    path = tmp_path / "10xgraph.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    stream = io.StringIO()
    code = ConfigCommand(OutputFormatter(stream=stream)).execute(
        config=str(path), open_browser=False
    )
    assert code == 0
    return stream.getvalue()


def test_config_command_prints_readiness_warnings(tmp_path: Path, monkeypatch) -> None:
    text = _run_config(tmp_path, monkeypatch, json.dumps({"agent": "graph.react:app"}))
    assert AUTH_OFF_TEXT in text


@pytest.mark.parametrize("content", [None, "{not json"])
def test_config_command_skips_warnings_without_a_usable_file(
    tmp_path: Path, monkeypatch, content: str | None
) -> None:
    assert AUTH_OFF_TEXT not in _run_config(tmp_path, monkeypatch, content)
