"""D-17: the legacy agentflow.json fallback must work for str and Path config names."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

import tenxgraph_api.cli.commands.api as api_mod
from tenxgraph_api.cli.commands.api import APICommand
from tenxgraph_api.cli.core.config import ConfigManager, config_names, resolve_default_config
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.cli.exceptions import ConfigurationError


@pytest.mark.parametrize("value", ["10xgraph.json", Path("10xgraph.json")])
def test_config_names_default_tries_both(value) -> None:
    assert config_names(value) == ("10xgraph.json", "agentflow.json")


@pytest.mark.parametrize("value", ["custom.json", Path("custom.json")])
def test_config_names_custom_is_used_as_given(value) -> None:
    assert config_names(value) == ("custom.json",)


def test_resolve_default_config_accepts_path(tmp_path: Path) -> None:
    (tmp_path / "agentflow.json").write_text("{}", encoding="utf-8")
    assert resolve_default_config(Path("10xgraph.json"), tmp_path) == tmp_path / "agentflow.json"


@pytest.mark.parametrize("value", ["10xgraph.json", Path("10xgraph.json")])
def test_find_config_file_falls_back_to_legacy(tmp_path: Path, monkeypatch, value) -> None:
    (tmp_path / "agentflow.json").write_text('{"agent": "g:app"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    found = ConfigManager().find_config_file(value)
    assert found.resolve() == (tmp_path / "agentflow.json").resolve()


def test_find_config_file_prefers_new_name(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "agentflow.json").write_text("{}", encoding="utf-8")
    (tmp_path / "10xgraph.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    found = ConfigManager().find_config_file(Path("10xgraph.json"))
    assert found.name == "10xgraph.json"


def test_explicit_custom_name_is_not_redirected(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "agentflow.json").write_text("{}", encoding="utf-8")
    (tmp_path / "custom.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert ConfigManager().find_config_file(Path("custom.json")).name == "custom.json"
    with pytest.raises(ConfigurationError):
        ConfigManager().find_config_file(Path("missing.json"))


def test_dev_command_resolves_legacy_config(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "agentflow.json").write_text('{"agent": "graph:app"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("GRAPH_PATH", "")
    monkeypatch.setattr(api_mod, "load_dotenv", lambda *a, **k: None)
    launched: list = []
    monkeypatch.setattr(api_mod.uvicorn, "run", lambda *a, **k: launched.append(a))

    out = OutputFormatter(stream=io.StringIO(), quiet=True)
    code = APICommand(output=out).execute(config="10xgraph.json", reload=False)

    assert code == 0
    assert launched
    import os

    assert Path(os.environ["GRAPH_PATH"]).name == "agentflow.json"
