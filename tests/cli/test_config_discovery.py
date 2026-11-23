"""Every command finds 10xgraph.json (or agentflow.json) in a parent folder."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import tenxgraph_api.cli.commands.config as config_module
from tenxgraph_api.cli.commands import revoke, worker
from tenxgraph_api.cli.commands.audit import AuditCommand
from tenxgraph_api.cli.commands.build import BuildCommand
from tenxgraph_api.cli.commands.config import ConfigCommand
from tenxgraph_api.cli.core.config import resolve_default_config
from tenxgraph_api.cli.core.output import OutputFormatter


CONFIG = {"agent": "graph.react:app", "worker": {"broker": {"type": "redis_stream"}}}


@pytest.fixture
def sub(tmp_path: Path, monkeypatch) -> Path:
    folder = tmp_path / "sub" / "deeper"
    folder.mkdir(parents=True)
    monkeypatch.chdir(folder)
    return folder


def _write(project: Path, name: str = "10xgraph.json") -> Path:
    path = project / name
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return path


def test_resolver_finds_parent(tmp_path: Path, sub: Path) -> None:
    path = _write(tmp_path)
    assert resolve_default_config("10xgraph.json") == path.resolve()


def test_resolver_prefers_nearest_then_new_name(tmp_path: Path, sub: Path) -> None:
    _write(tmp_path)
    near = _write(tmp_path / "sub", "agentflow.json")
    assert resolve_default_config("10xgraph.json") == near.resolve()
    new = _write(tmp_path / "sub")
    assert resolve_default_config("10xgraph.json") == new.resolve()


def test_resolver_legacy_name_in_parent(tmp_path: Path, sub: Path) -> None:
    path = _write(tmp_path, "agentflow.json")
    assert resolve_default_config("10xgraph.json") == path.resolve()


def test_resolver_explicit_path_and_custom_name(tmp_path: Path, sub: Path) -> None:
    other = tmp_path / "elsewhere.json"
    other.write_text("{}", encoding="utf-8")
    assert resolve_default_config(str(other)) == other
    assert resolve_default_config("elsewhere.json") == other.resolve()


def test_resolver_missing_returns_cwd_name(sub: Path) -> None:
    assert resolve_default_config("10xgraph.json") == sub.resolve() / "10xgraph.json"


@pytest.mark.parametrize("name", ["10xgraph.json", "agentflow.json"])
def test_audit_finds_parent_config(tmp_path: Path, sub: Path, name: str) -> None:
    _write(tmp_path, name)
    diagnostic = AuditCommand._config_check()
    assert diagnostic.status == "pass"
    assert str(tmp_path.resolve() / name) in diagnostic.detail


def test_audit_explicit_config(tmp_path: Path, sub: Path) -> None:
    path = _write(tmp_path, "custom.json")
    assert AuditCommand._config_check(str(path)).status == "pass"


def test_build_writes_next_to_parent_config(tmp_path: Path, sub: Path) -> None:
    _write(tmp_path)
    code = BuildCommand(OutputFormatter(stream=io.StringIO())).execute(
        non_interactive=True, docker_compose=True, worker=True
    )
    assert code == 0
    assert (tmp_path / "Dockerfile").exists()
    assert "worker" in (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    assert not (sub / "Dockerfile").exists()


class _StubServer:
    url = "http://127.0.0.1:1"
    paths: list = []

    def __init__(self, path: Path, port: int = 0, seed_path: Path | None = None) -> None:
        self.paths.append((path, seed_path))
        self.store = type("Store", (), {"seeded": False})()

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


@pytest.mark.parametrize(
    ("name", "expect_seed"), [("10xgraph.json", False), ("agentflow.json", True)]
)
def test_config_command_uses_parent_file(
    tmp_path: Path, sub: Path, monkeypatch, name: str, expect_seed: bool
) -> None:
    monkeypatch.setattr(config_module, "ConfigEditorServer", _StubServer)
    _StubServer.paths.clear()
    found = _write(tmp_path, name)
    code = ConfigCommand(OutputFormatter(stream=io.StringIO())).execute(open_browser=False)
    assert code == 0
    path, seed = _StubServer.paths[0]
    assert path == (tmp_path / "10xgraph.json").resolve()
    assert seed == (found.resolve() if expect_seed else None)


@pytest.mark.parametrize("name", ["10xgraph.json", "agentflow.json"])
def test_worker_and_revoke_find_parent(tmp_path: Path, sub: Path, name: str) -> None:
    _write(tmp_path, name)
    assert worker.resolve_default_config("10xgraph.json") == (tmp_path / name).resolve()
    assert revoke.resolve_default_config("10xgraph.json") == (tmp_path / name).resolve()
